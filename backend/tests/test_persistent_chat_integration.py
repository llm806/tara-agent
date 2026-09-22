import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from tara_agent.agent.models import ModelStreamDelta, RouteDecision
from tara_agent.agent.provider import AgentModelError
from tara_agent.api.app import create_app
from tara_agent.config import PROJECT_ROOT, Settings
from tara_agent.data.preprocess import preprocess
from tara_agent.persistence import Database, User

from .test_agent import RepresentativeModel

pytestmark = pytest.mark.integration


class FailingModel:
    name = "failing-model"
    provider = "test"
    trace_parameters: dict = {}

    async def route(self, question, context, tools):
        return RouteDecision(
            kind="analysis",
            rationale="进入测试分析流程。",
            capability_requirements=["sample_query"],
        )

    async def plan(self, question, context, tools):
        raise AgentModelError("测试模型规划失败")

    async def stream_answer(self, question, plan, result):
        if False:
            yield ModelStreamDelta(kind="answer", content="")


def _settings(dataset_dir: Path, processed_dir: Path) -> Settings:
    return Settings(
        environment="test",
        dataset_dir=dataset_dir,
        processed_data_dir=processed_dir,
        cors_origins=[],
        _env_file=PROJECT_ROOT / ".env",
    )


def _sse_events(body: str) -> list[dict]:
    events = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line.removeprefix("data:").lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        )
        if data:
            events.append(json.loads(data))
    return events


async def _register(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "集成测试用户",
            "password": "integration-test-password",
        },
    )
    assert response.status_code == 201
    return response.json()["user"]["id"]


@pytest.mark.anyio
async def test_chat_persists_session_messages_and_trace_tree(
    preprocessable_dataset_dir: Path,
    tmp_path: Path,
) -> None:
    if os.environ.get("TARA_RUN_DATABASE_TESTS") != "1":
        pytest.skip("设置 TARA_RUN_DATABASE_TESTS=1 后执行本地 PostgreSQL 集成测试")

    processed_dir = tmp_path / "processed"
    preprocess(
        preprocessable_dataset_dir,
        processed_dir,
        enforce_expected_shape=False,
    )
    database = Database(_settings(preprocessable_dataset_dir, processed_dir))
    model = RepresentativeModel()
    app: FastAPI = create_app(
        _settings(preprocessable_dataset_dir, processed_dir),
        agent_model=model,
        database=database,
    )
    session_id: str | None = None
    user_id: str | None = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            user_id = await _register(client, "chat-integration@example.com")
            first = await client.post(
                "/api/v1/chat/stream",
                json={"question": "找一个 Tara 样本"},
            )
            assert first.status_code == 200
            events = _sse_events(first.text)
            assert events[0]["event"] == "run_started"
            session_id = events[0]["session_id"]
            first_trace_id = events[0]["trace_id"]
            assert events[-1]["response"]["session_id"] == session_id
            assert events[-1]["response"]["trace_id"] == first_trace_id

            second = await client.post(
                "/api/v1/chat",
                json={
                    "question": "查看 TARA_TEST_001",
                    "session_id": session_id,
                },
            )
            assert second.status_code == 200
            second_payload = second.json()
            assert second_payload["session_id"] == session_id
            assert second_payload["trace_id"] != first_trace_id
            assert [message.content for message in model.route_context.messages] == [
                "找一个 Tara 样本",
                events[-1]["response"]["answer"],
            ]

            conversation = await client.get(f"/api/v1/sessions/{session_id}")
            assert conversation.status_code == 200
            messages = conversation.json()["messages"]
            assert [item["sequence_no"] for item in messages] == [0, 1, 2, 3]
            assert [item["role"] for item in messages] == [
                "user",
                "assistant",
                "user",
                "assistant",
            ]
            assert all(item["status"] == "completed" for item in messages)
            assert messages[0]["agent_response"] is None
            live_response = events[-1]["response"]
            restored_response = messages[1]["agent_response"]
            for key in (
                "question",
                "reasoning",
                "answer",
                "model",
                "route",
                "steps",
                "result",
                "charts",
                "warnings",
                "sources",
                "session_id",
                "trace_id",
                "message_id",
            ):
                assert restored_response[key] == live_response[key]
            assert restored_response["tool"]["name"] == live_response["tool"]["name"]
            assert restored_response["tool"]["arguments"] == live_response["tool"]["arguments"]
            assert messages[3]["agent_response"] == second_payload

            traces = await client.get(
                f"/api/v1/sessions/{session_id}/traces",
            )
            assert traces.status_code == 200
            assert traces.json()["page"]["total"] == 2

            trace = await client.get(
                f"/api/v1/sessions/{session_id}/traces/{first_trace_id}"
            )
            assert trace.status_code == 200
            trace_payload = trace.json()
            assert trace_payload["status"] == "completed"
            assert [item["sequence_no"] for item in trace_payload["spans"]] == list(
                range(11)
            )
            assert [item["span_kind"] for item in trace_payload["spans"]] == [
                "workflow",
                "node",
                "llm",
                "node",
                "llm",
                "node",
                "tool",
                "service",
                "data",
                "node",
                "llm",
            ]
            root_span = trace_payload["spans"][0]
            assert root_span["parent_span_id"] is None
            resource_ids = {
                item["resource_id"] for item in root_span["attributes"]["resources"]
            }
            assert {
                "workflow.tara_agent_request",
                "prompt.request_router",
                "prompt.tool_planner",
                "prompt.analysis_answer",
                "capability.tara_agent",
            } <= resource_ids
            node_spans = [
                item for item in trace_payload["spans"] if item["span_kind"] == "node"
            ]
            assert all(item["parent_span_id"] == root_span["id"] for item in node_spans)
            assert [item["attributes"]["langgraph_node"] for item in node_spans] == [
                "route",
                "understand",
                "execute",
                "answer",
            ]
            assert all(item["attributes"]["langgraph_task_id"] for item in node_spans)
            assert all(item["attributes"]["langgraph_namespace"] == [] for item in node_spans)
            llm_spans = [
                item for item in trace_payload["spans"] if item["span_kind"] == "llm"
            ]
            assert [item["parent_span_id"] for item in llm_spans] == [
                node_spans[0]["id"],
                node_spans[1]["id"],
                node_spans[3]["id"],
            ]
            assert [item["total_tokens"] for item in llm_spans] == [70, 120, 240]
            tool_span = next(
                item for item in trace_payload["spans"] if item["span_kind"] == "tool"
            )
            service_span = next(
                item
                for item in trace_payload["spans"]
                if item["span_kind"] == "service"
            )
            data_span = next(
                item for item in trace_payload["spans"] if item["span_kind"] == "data"
            )
            assert tool_span["parent_span_id"] == node_spans[2]["id"]
            assert service_span["parent_span_id"] == tool_span["id"]
            assert data_span["parent_span_id"] == service_span["id"]
            assert tool_span["tool_name"] == "find_samples"
            assert tool_span["attributes"]["resources"][0]["resource_id"] == (
                "tool.find_samples"
            )
            assert (
                tool_span["output_data"]["metadata"]["provenance"]
                ["sample_count"]
                == 2
            )
            assert data_span["output_data"] == {"row_count": 2, "column_count": 39}
            assert trace_payload["sample_count"] == 2

            second_trace = await client.get(
                f"/api/v1/sessions/{session_id}/traces/{second_payload['trace_id']}"
            )
            second_root = second_trace.json()["spans"][0]
            context_messages = second_root["input_data"]["conversation_context"][
                "messages"
            ]
            assert [item["content"] for item in context_messages] == [
                "找一个 Tara 样本",
                events[-1]["response"]["answer"],
            ]

            follow_up = await client.post(
                "/api/v1/chat",
                json={
                    "question": "只保留表层样本",
                    "session_id": session_id,
                },
            )
            assert follow_up.status_code == 200
            follow_up_payload = follow_up.json()
            assert follow_up_payload["route"]["analysis_reference_ids"] == [
                second_payload["trace_id"]
            ]
            assert len(model.plan_context.analysis_references) == 1
            assert str(model.plan_context.analysis_references[0].trace_id) == (
                second_payload["trace_id"]
            )

            follow_up_trace = await client.get(
                f"/api/v1/sessions/{session_id}/traces/"
                f"{follow_up_payload['trace_id']}"
            )
            follow_up_detail = follow_up_trace.json()
            assert "continued_from_trace_id" not in follow_up_detail
            planner_span = next(
                item
                for item in follow_up_detail["spans"]
                if item["span_kind"] == "llm"
                and item["name"] == "选择分析工具"
            )
            assert planner_span["artifact_refs"] == []
            assert planner_span["attributes"]["analysis_reference_ids"] == [
                second_payload["trace_id"]
            ]
            planning_references = planner_span["input_data"][
                "conversation_context"
            ]["analysis_references"]
            assert len(planning_references) == 1
            assert planning_references[0]["trace_id"] == second_payload["trace_id"]

            clarification = await client.post(
                "/api/v1/chat",
                json={
                    "question": "按极地状态比较多样性",
                    "session_id": session_id,
                },
            )
            assert clarification.status_code == 200
            clarification_payload = clarification.json()
            assert clarification_payload["route"]["kind"] == "clarify"

            clarified_analysis = await client.post(
                "/api/v1/chat",
                json={"question": "V9", "session_id": session_id},
            )
            assert clarified_analysis.status_code == 200
            clarified_payload = clarified_analysis.json()
            assert clarified_payload["route"]["analysis_reference_ids"] == []
            assert [message.content for message in model.plan_context.messages][-2:] == [
                "按极地状态比较多样性",
                clarification_payload["answer"],
            ]
            assert model.plan_context.analysis_references == []

            clarified_trace = await client.get(
                f"/api/v1/sessions/{session_id}/traces/"
                f"{clarified_payload['trace_id']}"
            )
            clarified_detail = clarified_trace.json()
            assert "continued_from_trace_id" not in clarified_detail
            clarified_planner_span = next(
                item
                for item in clarified_detail["spans"]
                if item["span_kind"] == "llm"
                and item["name"] == "选择分析工具"
            )
            assert clarified_planner_span["artifact_refs"] == []
            assert "analysis_reference_ids" not in clarified_planner_span["attributes"]
            clarified_context = clarified_planner_span["input_data"][
                "conversation_context"
            ]
            assert len(clarified_context["messages"]) <= 8
            assert [item["content"] for item in clarified_context["messages"]][-2:] == [
                "按极地状态比较多样性",
                clarification_payload["answer"],
            ]
            assert clarified_context["analysis_references"] == []
    finally:
        if user_id is not None:
            async with database.session() as session:
                await session.execute(delete(User).where(User.id == user_id))
        await database.dispose()


@pytest.mark.anyio
async def test_failed_model_call_is_persisted(
    preprocessable_dataset_dir: Path,
    tmp_path: Path,
) -> None:
    if os.environ.get("TARA_RUN_DATABASE_TESTS") != "1":
        pytest.skip("设置 TARA_RUN_DATABASE_TESTS=1 后执行本地 PostgreSQL 集成测试")

    processed_dir = tmp_path / "processed"
    preprocess(
        preprocessable_dataset_dir,
        processed_dir,
        enforce_expected_shape=False,
    )
    settings = _settings(preprocessable_dataset_dir, processed_dir)
    database = Database(settings)
    app = create_app(settings, agent_model=FailingModel(), database=database)
    title = "持久化失败链路测试"
    session_id: str | None = None
    user_id: str | None = None

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            user_id = await _register(client, "failed-integration@example.com")
            response = await client.post("/api/v1/chat", json={"question": title})
            assert response.status_code == 502

            sessions = await client.get("/api/v1/sessions")
            session = next(item for item in sessions.json()["items"] if item["title"] == title)
            session_id = session["id"]

            conversation = await client.get(f"/api/v1/sessions/{session_id}")
            assert [item["status"] for item in conversation.json()["messages"]] == [
                "completed",
                "failed",
            ]

            traces = await client.get(
                f"/api/v1/sessions/{session_id}/traces",
            )
            trace = traces.json()["items"][0]
            assert trace["status"] == "failed"
            assert trace["question"] == title
            assert trace["error_code"] == "AgentModelError"

            detail = await client.get(
                f"/api/v1/sessions/{session_id}/traces/{trace['id']}"
            )
            spans = detail.json()["spans"]
            assert len(spans) == 5
            assert [item["status"] for item in spans] == [
                "failed",
                "completed",
                "completed",
                "failed",
                "failed",
            ]
            assert [item["span_kind"] for item in spans] == [
                "workflow",
                "node",
                "llm",
                "node",
                "llm",
            ]
            assert spans[1]["attributes"]["langgraph_node"] == "route"
            assert spans[3]["attributes"]["langgraph_node"] == "understand"
            assert spans[1]["parent_span_id"] == spans[0]["id"]
            assert spans[2]["parent_span_id"] == spans[1]["id"]
            assert spans[3]["parent_span_id"] == spans[0]["id"]
            assert spans[4]["parent_span_id"] == spans[3]["id"]
            assert spans[4]["error_code"] == "AgentModelError"
    finally:
        if user_id is not None:
            async with database.session() as session:
                await session.execute(delete(User).where(User.id == user_id))
        await database.dispose()
