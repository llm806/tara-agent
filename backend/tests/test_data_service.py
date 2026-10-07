from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tara_agent.agent.gateway import MCPToolGateway
from tara_agent.agent.models import CORE_TOOLS, ToolName
from tara_agent.config import Settings
from tara_agent.data.preprocess import preprocess
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.data_service.app import create_app
from tara_agent.mcp import create_server

TOKEN = "test-data-service-token-0123456789abcdef"


@pytest.fixture
def service_settings(preprocessable_dataset_dir: Path, tmp_path: Path) -> Settings:
    processed = tmp_path / "processed"
    preprocess(preprocessable_dataset_dir, processed, enforce_expected_shape=False)
    return Settings(
        dataset_dir=preprocessable_dataset_dir,
        processed_data_dir=processed,
        _env_file=None,
    )


@pytest.fixture
def service_client(service_settings: Settings):
    with TestClient(create_app(service_settings, token=TOKEN)) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client


def request_body(client: TestClient, tool: str, arguments: dict) -> dict:
    status = client.get("/health").json()
    return {
        "tool_name": tool,
        "arguments": arguments,
        "parent_id": str(uuid4()),
        "expected_code_sha256": status["code_sha256"],
        "expected_generation": status["manifest"]["generation"],
    }


def test_service_requires_authentication(service_client: TestClient) -> None:
    for path in ("/health", "/tools", "/suggestions"):
        assert (
            service_client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        )
    service_client.headers.pop("Authorization")
    assert service_client.post("/call", json={}).status_code == 401


def test_service_requires_long_token(service_settings: Settings) -> None:
    with pytest.raises(ValueError, match="32"):
        create_app(service_settings, token="short")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("find_samples", {"query": {"limit": 1}}),
        ("get_sample_info", {"sample_id": "TARA_TEST_001"}),
        ("find_taxa", {"query": {"marker": "v4", "taxon": "Dinoflagellata"}}),
        ("taxon_abundance", {"query": {"marker": "v4", "taxon": "Dinoflagellata"}}),
        (
            "taxon_abundance",
            {
                "query": {
                    "marker": "v9",
                    "taxon": "Dinoflagellata",
                    "group_by": "station",
                    "aggregation": "max",
                    "limit": 1,
                }
            },
        ),
        (
            "taxon_abundance",
            {
                "query": {
                    "marker": "v9",
                    "taxon": "Dinoflagellata",
                    "sample_ids": ["TARA_TEST_001", "TARA_TEST_002"],
                    "taxonomic_rank": "genus",
                    "limit": 1,
                }
            },
        ),
        ("diversity_analysis", {"query": {"marker": "v4"}}),
        (
            "environment_association",
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Dinoflagellata",
                    "environment_variable": "temperature",
                }
            },
        ),
    ],
)
async def test_http_matches_in_process_and_preserves_trace(
    service_client: TestClient,
    service_settings: Settings,
    tool: str,
    arguments: dict,
) -> None:
    gateway = MCPToolGateway(
        create_server(ProcessedDataReader(service_settings.processed_data_dir))
    )
    expected = await gateway.call(ToolName(tool), arguments)
    body = request_body(service_client, tool, arguments)
    response = service_client.post("/call", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["error"] is None
    assert payload["result"] == expected
    events = payload["observations"]
    assert events
    known = {body["parent_id"]}
    for event in events:
        assert event["parent_id"] in known
        if event["phase"] == "started":
            known.add(event["observation_id"])
    assert any(event["span_kind"] == "data" for event in events)
    assert any(event.get("details", {}).get("data_sources") for event in events)


def test_version_and_schema_mismatches_are_rejected(service_client: TestClient) -> None:
    body = request_body(service_client, "find_samples", {"query": {"limit": 1}})
    for key, value in (("expected_code_sha256", "0" * 64), ("expected_generation", "other")):
        assert service_client.post("/call", json={**body, key: value}).status_code == 409
    assert (
        service_client.post("/call", json={**body, "tool_name": "execute_sql"}).status_code == 422
    )
    invalid = {**body, "arguments": {"query": {"limit": 0}}}
    payload = service_client.post("/call", json=invalid).json()
    assert payload["error"]
    assert payload["result"] is None


def test_not_found_returns_error_and_closed_trace(service_client: TestClient) -> None:
    body = request_body(service_client, "get_sample_info", {"sample_id": "MISSING"})
    payload = service_client.post("/call", json=body).json()
    assert payload["result"] is None
    assert payload["error"]
    assert payload["observations"][-1]["phase"] == "failed"


def test_health_detects_removed_data(
    service_client: TestClient, service_settings: Settings
) -> None:
    (service_settings.processed_data_dir / "manifest.json").unlink()
    assert service_client.get("/health").status_code == 503


def test_tools_and_suggestions_are_available(service_client: TestClient) -> None:
    assert {item["name"] for item in service_client.get("/tools").json()} == CORE_TOOLS
    payload = service_client.get("/suggestions?limit=4").json()
    assert len(payload["items"]) == 4
