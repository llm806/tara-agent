from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from tara_agent.agent.gateway import AgentToolError, MCPToolGateway
from tara_agent.agent.models import ToolName
from tara_agent.api.app import create_app as create_product_app
from tara_agent.config import Settings
from tara_agent.data.preprocess import preprocess
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.data_service.app import create_app
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.mcp import create_server
from tara_agent.observability.execution import bind_execution_trace, replay_execution_observations

from .test_agent import RepresentativeModel

TOKEN = "remote-test-token-0123456789abcdef0123456789"


@pytest.fixture
def remote_setup(preprocessable_dataset_dir: Path, tmp_path: Path):
    processed = tmp_path / "server-processed"
    preprocess(preprocessable_dataset_dir, processed, enforce_expected_shape=False)
    server_settings = Settings(
        dataset_dir=preprocessable_dataset_dir,
        processed_data_dir=processed,
        _env_file=None,
    )
    server = create_app(server_settings, token=TOKEN)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN, encoding="utf-8")
    local_settings = Settings(
        environment="test",
        dataset_dir=tmp_path / "no-local-raw",
        processed_data_dir=tmp_path / "no-local-processed",
        data_service_url="http://127.0.0.1:8010",
        data_service_token_file=token_file,
        cors_origins=[],
        _env_file=None,
    )
    return server_settings, local_settings, httpx.ASGITransport(app=server)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("find_samples", {"query": {"limit": 1}}),
        ("get_sample_info", {"sample_id": "TARA_TEST_001"}),
        ("find_taxa", {"query": {"marker": "v4", "taxon": "Dinoflagellata"}}),
        ("taxon_abundance", {"query": {"marker": "v4", "taxon": "Dinoflagellata"}}),
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
async def test_remote_gateway_matches_local_and_replays_same_tree(remote_setup, tool, arguments):
    server_settings, local_settings, transport = remote_setup
    remote = RemoteToolGateway(local_settings, transport=transport)
    local = MCPToolGateway(create_server(ProcessedDataReader(server_settings.processed_data_dir)))
    expected = await local.call(ToolName(tool), arguments)
    events = []
    parent = str(uuid4())
    with bind_execution_trace(parent_id=parent, writer=events.append):
        assert await remote.call(ToolName(tool), arguments) == expected
    assert events[0]["parent_id"] == parent
    assert {event["span_kind"] for event in events} >= {"service", "data"}
    assert not local_settings.dataset_dir.exists()
    assert not local_settings.processed_data_dir.exists()


@pytest.mark.anyio
async def test_product_health_chat_and_suggestions_without_local_data(remote_setup):
    _, settings, transport = remote_setup
    app = create_product_app(
        settings,
        agent_model=RepresentativeModel(),
        data_service_transport=transport,
    )
    assert app.state.data_reader is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ready = await client.get("/api/v1/ready")
        assert ready.status_code == 200
        assert ready.json()["data_ready"] is True
        assert len(ready.json()["datasets"]) == 4
        response = await client.post("/api/v1/chat", json={"question": "找一个 Tara 样本"})
        assert response.status_code == 200
        assert response.json()["result"]["page"]["total"] == 2
        suggestions = await app.state.question_suggestions.get_batch(limit=4)
        assert len(suggestions) == 4


@pytest.mark.anyio
async def test_wrong_token_and_code_are_rejected(remote_setup):
    _, settings, transport = remote_setup
    remote = RemoteToolGateway(settings, transport=transport)
    remote.token = "incorrect"
    with pytest.raises(AgentToolError, match="认证"):
        await remote.list_tools()
    remote.token = TOKEN
    remote.code_sha256 = "0" * 64
    with pytest.raises(AgentToolError, match="不一致"):
        await remote.call(ToolName.FIND_SAMPLES, {"query": {}})


@pytest.mark.anyio
async def test_outage_is_degraded_without_fallback(remote_setup):
    _, settings, _ = remote_setup

    def unavailable(request):
        raise httpx.ConnectError("unavailable", request=request)

    app = create_product_app(
        settings,
        agent_model=RepresentativeModel(),
        data_service_transport=httpx.MockTransport(unavailable),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/ready")).status_code == 503
    assert app.state.data_reader is None
    with pytest.raises(AgentToolError, match="SSH"):
        await app.state.question_suggestions.get_batch(limit=4)


@pytest.mark.anyio
async def test_remote_error_keeps_failed_observations(remote_setup):
    _, settings, transport = remote_setup
    remote = RemoteToolGateway(settings, transport=transport)
    events = []
    with (
        bind_execution_trace(parent_id=str(uuid4()), writer=events.append),
        pytest.raises(AgentToolError),
    ):
        await remote.call(ToolName.GET_SAMPLE_INFO, {"sample_id": "NOT_FOUND"})
    assert events[-1]["phase"] == "failed"


def test_replay_rejects_foreign_tree_before_writing():
    from datetime import UTC, datetime

    from tara_agent.observability.execution import TraceObservationEvent

    events = []
    event = TraceObservationEvent(
        observation_id=str(uuid4()),
        parent_id=str(uuid4()),
        name="foreign",
        span_kind="data",
        phase="started",
        occurred_at=datetime.now(UTC),
    )
    with (
        bind_execution_trace(parent_id=str(uuid4()), writer=events.append),
        pytest.raises(ValueError, match="执行树"),
    ):
        replay_execution_observations([event])
    assert events == []


@pytest.mark.parametrize(
    "url", ["http://10.24.116.59:8010", "https://u:p@test", "http://127.0.0.1/?token=x"]
)
def test_remote_url_prevents_cleartext_credentials(url):
    with pytest.raises(ValueError):
        Settings(data_service_url=url, _env_file=None)
