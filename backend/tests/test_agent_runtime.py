from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest

from tara_agent.agent.models import (
    AgentResponse,
    AgentStreamEvent,
    RouteDecision,
    ToolTrace,
)
from tara_agent.agent.runtime import PersistentAgentRun
from tara_agent.agent.workflow import WorkflowTaskEvent
from tara_agent.observability.execution import TraceObservationEvent
from tara_agent.persistence.repositories import SpanRecord, StartedRun


class RuntimeRepository:
    def __init__(self) -> None:
        self.spans: dict[object, SpanRecord] = {}
        self.completed = False
        self.failed = False

    async def create_span(self, record: SpanRecord) -> None:
        self.spans[record.id] = record

    async def update_span(self, record: SpanRecord) -> None:
        self.spans[record.id] = record

    async def complete_run(self, run: StartedRun, **values: Any) -> None:
        self.completed = True

    async def fail_run(self, run: StartedRun, **values: Any) -> None:
        self.failed = True


class DynamicWorkflowAgent:
    workflow_name = "dynamic_workflow"
    workflow_title = "动态工作流"
    shared_resource_snapshot: list[dict[str, str]] = []

    async def stream_execution(self, question: str, context=None):
        yield WorkflowTaskEvent(
            task_id="task-1",
            node_name="branch_a",
            node_title="分支 A",
            phase="started",
            input_data={"question": question},
        )
        yield TraceObservationEvent(
            observation_id="operation-1",
            parent_id="task-1",
            name="分支 A 模型调用",
            span_kind="llm",
            phase="started",
            occurred_at=datetime.now(UTC),
            details={"input_data": {"question": question}},
        )
        yield WorkflowTaskEvent(
            task_id="task-2",
            node_name="branch_b",
            node_title="分支 B",
            phase="started",
            input_data={"question": question},
        )
        yield TraceObservationEvent(
            observation_id="operation-2",
            parent_id="task-2",
            name="分支 B 工具调用",
            span_kind="tool",
            phase="started",
            occurred_at=datetime.now(UTC),
            details={"tool_name": "find_samples"},
        )
        yield TraceObservationEvent(
            observation_id="operation-2",
            parent_id="task-2",
            name="分支 B 工具调用",
            span_kind="tool",
            phase="completed",
            occurred_at=datetime.now(UTC),
            details={"output_data": {"value": 2}},
        )
        yield WorkflowTaskEvent(
            task_id="task-2",
            node_name="branch_b",
            node_title="分支 B",
            phase="completed",
            output_data={"value": 2},
        )
        yield TraceObservationEvent(
            observation_id="operation-1",
            parent_id="task-1",
            name="分支 A 模型调用",
            span_kind="llm",
            phase="completed",
            occurred_at=datetime.now(UTC),
            details={"output_data": {"value": 1}},
        )
        yield WorkflowTaskEvent(
            task_id="task-1",
            node_name="branch_a",
            node_title="分支 A",
            phase="completed",
            output_data={"value": 1},
        )
        yield AgentStreamEvent(
            event="complete",
            response=AgentResponse(
                question=question,
                reasoning="",
                answer="完成",
                model="test-model",
                tool=ToolTrace(
                    name="find_samples",
                    arguments={"query": {"limit": 1}},
                    summary="测试",
                ),
                steps=[],
                result={"metadata": {"provenance": {}}},
                charts=[],
                warnings=[],
                sources=[],
            ),
        )


class FailingWorkflowAgent:
    workflow_name = "failing_workflow"
    workflow_title = "失败工作流"
    shared_resource_snapshot: list[dict[str, str]] = []

    async def stream_execution(self, question: str, context=None):
        yield WorkflowTaskEvent(
            task_id="task-1",
            node_name="understand",
            node_title="理解问题",
            phase="started",
            input_data={"question": question},
        )
        raise ValueError("原始执行错误")


class DirectResponseAgent:
    workflow_name = "direct_response"
    workflow_title = "直接回答"
    shared_resource_snapshot: list[dict[str, str]] = []

    async def stream_execution(self, question: str, context=None):
        yield AgentStreamEvent(
            event="complete",
            response=AgentResponse(
                question=question,
                reasoning="",
                answer="当前支持样本和分类群查询。",
                model="test-model",
                route=RouteDecision(
                    kind="direct_answer",
                    rationale="系统能力咨询。",
                    response="当前支持样本和分类群查询。",
                ),
                steps=[],
                result={},
                charts=[],
                warnings=[],
                sources=[],
            ),
        )


class CleanupFailingRepository(RuntimeRepository):
    async def update_span(self, record: SpanRecord) -> None:
        raise RuntimeError("节点状态写入失败")


@pytest.mark.anyio
async def test_persistent_run_records_dynamic_langgraph_tasks() -> None:
    repository = RuntimeRepository()
    started_at = datetime.now(UTC)
    run = StartedRun(
        session_id=uuid4(),
        trace_id=uuid4(),
        user_message_id=uuid4(),
        assistant_message_id=uuid4(),
        started_at=started_at,
    )
    execution = PersistentAgentRun(
        agent=cast(Any, DynamicWorkflowAgent()),
        repository=cast(Any, repository),
        run=run,
        question="测试动态节点",
    )

    events = [event async for event in execution.stream()]
    spans = sorted(repository.spans.values(), key=lambda item: item.sequence_no)

    assert [event.event for event in events] == ["run_started", "complete"]
    assert [span.span_kind for span in spans] == [
        "workflow",
        "node",
        "llm",
        "node",
        "tool",
    ]
    assert all(span.status == "completed" for span in spans)
    assert spans[1].parent_span_id == spans[0].id
    assert spans[2].parent_span_id == spans[1].id
    assert spans[3].parent_span_id == spans[0].id
    assert spans[4].parent_span_id == spans[3].id
    assert [span.name for span in spans[1:]] == [
        "分支 A",
        "分支 A 模型调用",
        "分支 B",
        "分支 B 工具调用",
    ]
    assert all(
        span.input_data == {"question": "测试动态节点"}
        for span in (spans[1], spans[3])
    )
    assert [span.output_data for span in (spans[1], spans[3])] == [
        {"value": 1},
        {"value": 2},
    ]
    assert spans[1].attributes == {
        "langgraph_task_id": "task-1",
        "langgraph_node": "branch_a",
        "langgraph_namespace": [],
    }
    assert spans[3].attributes["langgraph_task_id"] == "task-2"
    assert spans[4].tool_name == "find_samples"
    assert repository.completed is True
    assert repository.failed is False


@pytest.mark.anyio
async def test_persistent_run_preserves_original_error_when_span_cleanup_fails() -> None:
    repository = CleanupFailingRepository()
    run = StartedRun(
        session_id=uuid4(),
        trace_id=uuid4(),
        user_message_id=uuid4(),
        assistant_message_id=uuid4(),
        started_at=datetime.now(UTC),
    )
    execution = PersistentAgentRun(
        agent=cast(Any, FailingWorkflowAgent()),
        repository=cast(Any, repository),
        run=run,
        question="触发失败",
    )

    with pytest.raises(ValueError, match="原始执行错误") as raised:
        _ = [event async for event in execution.stream()]

    assert repository.failed is True
    assert any("节点状态写入失败" in note for note in raised.value.__notes__)


@pytest.mark.anyio
async def test_persistent_run_completes_without_a_tool_call() -> None:
    repository = RuntimeRepository()
    run = StartedRun(
        session_id=uuid4(),
        trace_id=uuid4(),
        user_message_id=uuid4(),
        assistant_message_id=uuid4(),
        started_at=datetime.now(UTC),
    )
    execution = PersistentAgentRun(
        agent=cast(Any, DirectResponseAgent()),
        repository=cast(Any, repository),
        run=run,
        question="这个系统能做什么？",
    )

    events = [event async for event in execution.stream()]

    assert events[-1].response is not None
    assert events[-1].response.tool is None
    assert repository.completed is True
    assert repository.failed is False
