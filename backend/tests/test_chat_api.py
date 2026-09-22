from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tara_agent.api.app import create_app
from tara_agent.config import Settings
from tara_agent.data.preprocess import preprocess

from .test_agent import RepresentativeModel


def _settings(dataset_dir: Path, processed_dir: Path) -> Settings:
    return Settings(
        environment="test",
        dataset_dir=dataset_dir,
        processed_data_dir=processed_dir,
        cors_origins=[],
        _env_file=None,
    )


@pytest.fixture
def chat_app(preprocessable_dataset_dir: Path, tmp_path: Path) -> FastAPI:
    processed_dir = tmp_path / "processed"
    preprocess(
        preprocessable_dataset_dir,
        processed_dir,
        enforce_expected_shape=False,
    )
    return create_app(
        _settings(preprocessable_dataset_dir, processed_dir),
        agent_model=RepresentativeModel(),
    )


@pytest.mark.anyio
async def test_chat_returns_answer_trace_and_chart(chat_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=chat_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/chat",
            json={"question": "找一个 Tara 样本"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reasoning"] == "先检查工具结果。"
    assert payload["tool"]["name"] == "find_samples"
    assert [step["stage"] for step in payload["steps"]] == [
        "route",
        "understand",
        "execute",
        "answer",
    ]
    assert payload["charts"][0]["kind"] == "sample_map"
    assert payload["sources"] == ["context_general.tsv", "context_stat.tsv"]


@pytest.mark.anyio
async def test_chat_stream_returns_sse_trace(chat_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=chat_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"question": "V4 Dinoflagellata 的相对丰度"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: step") == 4
    assert response.text.count("event: reasoning_delta") == 1
    assert response.text.count("event: answer_delta") == 2
    assert "event: complete" in response.text
    assert '"taxon_abundance"' in response.text
    assert response.text.index("event: answer_delta") < response.text.index("event: complete")


@pytest.mark.anyio
async def test_chat_routes_capability_question_without_analysis_tool(
    chat_app: FastAPI,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=chat_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/chat",
            json={"question": "这个系统能做什么？"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"]["kind"] == "direct_answer"
    assert "tool" not in payload
    assert payload["result"] == {}
    assert [step["stage"] for step in payload["steps"]] == ["route", "respond"]


@pytest.mark.anyio
async def test_chat_requires_ready_agent(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path / "missing", tmp_path / "processed"))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/chat",
            json={"question": "找样本"},
        )

    assert response.status_code == 503


@pytest.mark.anyio
async def test_chat_rejects_blank_or_oversized_questions(chat_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=chat_app),
        base_url="http://test",
    ) as client:
        blank = await client.post("/api/v1/chat", json={"question": "  "})
        oversized = await client.post("/api/v1/chat", json={"question": "x" * 2_001})

    assert blank.status_code == 422
    assert oversized.status_code == 422
