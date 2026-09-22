import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from tara_agent.api.app import create_app
from tara_agent.config import Settings
from tara_agent.data.preprocess import preprocess


class ReadyModel:
    name = "test-model"


class UnavailableDatabase:
    async def ping(self) -> None:
        raise OSError("database unavailable")

    async def dispose(self) -> None:
        pass


def _settings(dataset_dir: Path, tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        dataset_dir=dataset_dir,
        processed_data_dir=tmp_path / "processed",
        cors_origins=[],
        _env_file=None,
    )


async def _get(app: FastAPI, path: str) -> Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


def test_health_reports_valid_processed_data(
    preprocessable_dataset_dir: Path, tmp_path: Path
) -> None:
    settings = _settings(preprocessable_dataset_dir, tmp_path)
    preprocess(
        preprocessable_dataset_dir,
        settings.processed_data_dir,
        enforce_expected_shape=False,
    )
    app = create_app(settings, agent_model=ReadyModel())

    response = asyncio.run(_get(app, "/api/v1/health"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data_ready"] is True
    assert payload["agent_ready"] is True
    assert len(payload["datasets"]) == 4


def test_readiness_requires_agent(
    preprocessable_dataset_dir: Path, tmp_path: Path
) -> None:
    settings = _settings(preprocessable_dataset_dir, tmp_path)
    preprocess(
        preprocessable_dataset_dir,
        settings.processed_data_dir,
        enforce_expected_shape=False,
    )
    app = create_app(settings)

    response = asyncio.run(_get(app, "/api/v1/ready"))

    assert response.status_code == 503
    assert response.json()["agent_ready"] is False


def test_readiness_checks_database_connection(
    preprocessable_dataset_dir: Path, tmp_path: Path
) -> None:
    settings = _settings(preprocessable_dataset_dir, tmp_path).model_copy(
        update={"environment": "development"}
    )
    preprocess(
        preprocessable_dataset_dir,
        settings.processed_data_dir,
        enforce_expected_shape=False,
    )
    app = create_app(
        settings,
        agent_model=ReadyModel(),
        database=UnavailableDatabase(),
    )

    response = asyncio.run(_get(app, "/api/v1/ready"))

    assert response.status_code == 503
    assert response.json()["database_ready"] is False


def test_readiness_returns_503_when_sources_are_missing(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing"
    app = create_app(_settings(missing_dir, tmp_path))

    response = asyncio.run(_get(app, "/api/v1/ready"))

    assert response.status_code == 503
    assert response.json()["data_ready"] is False


def test_api_does_not_treat_raw_sources_as_ready(
    valid_dataset_dir: Path, tmp_path: Path
) -> None:
    app = create_app(_settings(valid_dataset_dir, tmp_path))

    response = asyncio.run(_get(app, "/api/v1/ready"))

    assert response.status_code == 503
    assert response.json()["datasets"] == []
