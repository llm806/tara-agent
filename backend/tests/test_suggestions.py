from __future__ import annotations

import random
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tara_agent.agent.gateway import MCPToolGateway
from tara_agent.agent.suggestions import QuestionSuggestionService
from tara_agent.api.app import create_app
from tara_agent.config import Settings
from tara_agent.data.preprocess import preprocess
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.mcp import create_server


def _settings(dataset_dir: Path, processed_dir: Path) -> Settings:
    return Settings(
        environment="test",
        dataset_dir=dataset_dir,
        processed_data_dir=processed_dir,
        cors_origins=[],
        _env_file=None,
    )


def _service(dataset_dir: Path, processed_dir: Path) -> QuestionSuggestionService:
    preprocess(dataset_dir, processed_dir, enforce_expected_shape=False)
    reader = ProcessedDataReader(processed_dir)
    gateway = MCPToolGateway(create_server(reader))
    return QuestionSuggestionService.from_reader(
        gateway,
        reader,
        random_source=random.Random(7),
    )


@pytest.mark.anyio
async def test_suggestions_use_actual_processed_values(
    preprocessable_dataset_dir: Path,
    tmp_path: Path,
) -> None:
    service = _service(preprocessable_dataset_dir, tmp_path / "processed")

    questions = [item.question for item in service.candidates]

    assert any("TARA_TEST_001" in question for question in questions)
    assert any("Test Ocean" in question for question in questions)
    assert any("0.8-5" in question for question in questions)
    assert any("Dinoflagellata" in question for question in questions)
    assert all("Bacillariophyta" not in question for question in questions)
    assert all("Eukaryota" not in question for question in questions)
    assert len({item.id for item in service.candidates}) == len(service.candidates)
    assert {item.category for item in service.candidates} == {
        "样本筛选",
        "样本详情",
        "分类群检索",
        "丰度分布",
        "多样性比较",
    }


@pytest.mark.anyio
async def test_suggestions_replace_current_batch(
    preprocessable_dataset_dir: Path,
    tmp_path: Path,
) -> None:
    service = _service(preprocessable_dataset_dir, tmp_path / "processed")

    first = await service.get_batch(limit=4)
    second = await service.get_batch(
        limit=4,
        exclude_ids={item.id for item in first},
    )
    third = await service.get_batch(
        limit=4,
        exclude_ids={item.id for item in [*first, *second]},
    )

    assert len(first) == 4
    assert len({item.category for item in first}) == 4
    assert {item.id for item in first}.isdisjoint(item.id for item in second)
    assert {item.id for item in [*first, *second]}.isdisjoint(
        item.id for item in third
    )


@pytest.mark.anyio
async def test_question_suggestion_api_returns_supported_questions(
    preprocessable_dataset_dir: Path,
    tmp_path: Path,
) -> None:
    processed_dir = tmp_path / "processed"
    preprocess(preprocessable_dataset_dir, processed_dir, enforce_expected_shape=False)
    app = create_app(_settings(preprocessable_dataset_dir, processed_dir))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/question-suggestions?limit=4")
        first_items = response.json()["items"]
        replacement = await client.get(
            "/api/v1/question-suggestions",
            params=[
                ("limit", "4"),
                *(("exclude_id", item["id"]) for item in first_items),
            ],
        )

    assert response.status_code == 200
    assert replacement.status_code == 200
    assert len(first_items) == 4
    assert all(set(item) == {"id", "category", "question"} for item in first_items)
    replacement_ids = {item["id"] for item in replacement.json()["items"]}
    assert replacement_ids.isdisjoint(item["id"] for item in first_items)


@pytest.mark.anyio
async def test_question_suggestion_api_requires_processed_data(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path / "missing", tmp_path / "processed"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/question-suggestions")

    assert response.status_code == 503
