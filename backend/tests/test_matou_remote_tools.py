"""从 MCP 到远程网关、产品聊天的 MATOU 链路，不需要本机真实科学数据。"""

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from tara_agent.agent.gateway import AgentToolError, MCPToolGateway
from tara_agent.agent.models import CORE_TOOLS, MATOU_TOOLS, RouteDecision, ToolName, ToolPlan
from tara_agent.api.app import create_app as create_product_app
from tara_agent.config import Settings
from tara_agent.data.matou_prepare import PreparationLimits, prepare_function_data
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.data_service.app import create_app
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.mcp import create_server
from tara_agent.observability.execution import bind_execution_trace

from .test_agent import RepresentativeModel
from .test_data_service import service_settings as service_settings
from .test_matou_function_data import dataset as dataset

TOKEN = "matou-remote-test-token-0123456789abcdef"


@pytest.fixture
def matou_setup(dataset, service_settings, tmp_path):
    prepare_function_data(*dataset, PreparationLimits(min_free_gib=0))
    settings = service_settings.model_copy(update={"matou_data_dir": dataset[3]})
    service = create_app(settings, token=TOKEN)
    token = tmp_path / "token"
    token.write_text(TOKEN, encoding="utf-8")
    local = Settings(
        environment="test",
        dataset_dir=tmp_path / "no-local-raw",
        processed_data_dir=tmp_path / "no-local-core",
        matou_data_dir=tmp_path / "no-local-matou",
        data_service_url="http://127.0.0.1:8011",
        data_service_token_file=token,
        cors_origins=[],
        _env_file=None,
    )
    return settings, local, service


def body(client, tool, arguments):
    health = client.get("/health").json()
    return {
        "tool_name": tool,
        "arguments": arguments,
        "parent_id": str(uuid4()),
        "expected_code_sha256": health["code_sha256"],
        "expected_generation": health["manifest"]["generation"],
        "expected_matou_manifest_sha256": health["matou"]["manifest_sha256"],
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,query",
    [
        (ToolName.FIND_FUNCTION_SAMPLES, {"assay": "MetaG", "limit": 1, "offset": 1}),
        (ToolName.FIND_FUNCTION_SAMPLES, {"assay": "MetaT", "sample_name_contains": "T-A"}),
        (ToolName.FUNCTION_PROFILE, {"assay": "MetaG", "sample_name": "G-A", "top_n": 1}),
        (
            ToolName.FUNCTION_PROFILE,
            {
                "assay": "MetaT",
                "sample_name": "T-A",
                "pfam_accessions": ["PF00002"],
                "max_i_evalue": 1e-5,
            },
        ),
    ],
)
async def test_remote_equals_mcp_with_actual_parent_tree_and_no_local_data(
    matou_setup, tool, query
):
    settings, local, app = matou_setup
    gateway = MCPToolGateway(
        create_server(
            ProcessedDataReader(settings.processed_data_dir),
            matou_reader=MatouDataReader(settings.matou_data_dir),
        )
    )
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=app))
    assert {t.name for t in await remote.list_tools()} == CORE_TOOLS | MATOU_TOOLS
    assert await remote.call(tool, {"query": query}) == await gateway.call(tool, {"query": query})
    events = []
    parent = str(uuid4())
    with bind_execution_trace(parent_id=parent, writer=events.append):
        result = await remote.call(tool, {"query": query})
    assert events and events[0]["parent_id"] == parent
    assert {e["span_kind"] for e in events} >= {"service", "data"}
    known = {parent}
    opened = set()
    for event in events:
        assert event["parent_id"] in known
        if event["phase"] == "started":
            known.add(event["observation_id"])
            opened.add(event["observation_id"])
        else:
            opened.remove(event["observation_id"])
    assert not opened
    if tool is ToolName.FUNCTION_PROFILE:
        assert result["taxon_value_sum"] == 1
        assert result["context"]["taxon"] == "Example taxon"
        assert result["observations"][0]["fraction_of_observed_taxon_signal"] == pytest.approx(
            0.3 if "max_i_evalue" in query else 0.9
        )
        assert result["value_interpretation"] == "provided_values_export_unit_unconfirmed"
    else:
        assert len(result["items"]) == 1
        assert result["metadata"]["warnings"][0]["code"] == "sample_mapping_unconfirmed"
    assert not local.dataset_dir.exists()
    assert not local.processed_data_dir.exists()
    assert not local.matou_data_dir.exists()


def test_mcp_schemas_are_read_only_and_health_has_version(matou_setup):
    _, _, app = matou_setup
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        status = client.get("/health").json()["matou"]
        assert status["taxon"] == "Example taxon"
        assert status["sample_counts"] == {"MetaG": 2, "MetaT": 1}
        tools = {t["name"]: t for t in client.get("/tools").json()}
        for name in MATOU_TOOLS:
            assert tools[name]["input_schema"]["properties"]["query"]
        for invalid in (None, "0" * 64):
            request = body(
                client, "function_profile", {"query": {"assay": "MetaG", "sample_name": "G-A"}}
            )
            request["expected_matou_manifest_sha256"] = invalid
            assert client.post("/call", json=request).status_code == 409
        request["tool_name"] = "execute_sql"
        assert client.post("/call", json=request).status_code == 422


@pytest.mark.parametrize(
    "query",
    [
        {"assay": "MetaG", "sample_name": "T-A"},
        {"assay": "MetaG", "sample_name": "G-A", "taxon": "Other"},
        {"assay": "MetaG", "sample_name": "G-A", "pfam_accessions": ["PF00002", "PF00002"]},
        {"assay": "MetaG", "sample_name": "G-A", "top_n": 0},
        {"assay": "MetaG", "sample_name": "G-A", "max_i_evalue": -1},
        {"assay": "MetaG", "sample_name": "G-A", "python": "print(1)"},
        {"assay": "MetaG"},
    ],
)
def test_invalid_scope_and_parameters_never_return_a_result(matou_setup, query):
    _, _, app = matou_setup
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        result = client.post(
            "/call", json=body(client, "function_profile", {"query": query})
        ).json()
        assert result["result"] is None
        assert result["error"]


def test_changed_manifest_makes_health_and_bound_call_unavailable(matou_setup):
    settings, _, app = matou_setup
    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        request = body(client, "find_function_samples", {"query": {"assay": "MetaG"}})
        path = settings.matou_data_dir / "manifest.json"
        path.write_text(path.read_text() + " ")
        assert client.get("/health").status_code == 503
        assert client.post("/call", json=request).status_code == 503


@pytest.mark.anyio
async def test_corrupt_artifact_returns_failure_tree_without_fallback(matou_setup):
    settings, local, app = matou_setup
    path = settings.matou_data_dir / "gene_pfam.parquet"
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=app))
    events = []
    with (
        bind_execution_trace(parent_id=str(uuid4()), writer=events.append),
        pytest.raises(AgentToolError),
    ):
        await remote.call(
            ToolName.FUNCTION_PROFILE, {"query": {"assay": "MetaG", "sample_name": "G-A"}}
        )
    assert events[-1]["phase"] == "failed"
    assert not local.matou_data_dir.exists()


class FunctionModel(RepresentativeModel):
    async def route(self, question, context, tools):
        assert {t.name for t in tools} >= MATOU_TOOLS
        return RouteDecision(
            kind="analysis",
            rationale="查询已准备的单样本候选功能。",
            capability_requirements=["function_profile"],
        )

    async def plan(self, question, context, tools):
        return ToolPlan(
            tool_name="function_profile",
            arguments={"query": {"assay": "MetaG", "sample_name": "G-A", "top_n": 2}},
            rationale="明确样本的候选功能谱。",
        )


@pytest.mark.anyio
async def test_local_product_chat_uses_remote_matou_and_presents_chart(matou_setup):
    _, local, server = matou_setup
    app = create_product_app(
        local, agent_model=FunctionModel(), data_service_transport=httpx.ASGITransport(app=server)
    )
    assert app.state.data_reader is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post("/api/v1/chat", json={"question": "MetaG G-A 的前两个候选 Pfam"})
    assert result.status_code == 200
    response = result.json()
    assert response["tool"]["name"] == "function_profile"
    assert response["charts"][0]["x"] == ["PF00002", "PF00001"]
    assert response["sources"] and response["warnings"]
    assert not local.matou_data_dir.exists()


@pytest.mark.anyio
async def test_remote_manifest_change_is_pinned_before_call(matou_setup):
    _, local, server = matou_setup
    with TestClient(server, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        status = client.get("/health").json()

    def respond(request):
        assert request.url.path == "/health"
        return httpx.Response(200, json=status)

    remote = RemoteToolGateway(local, transport=httpx.MockTransport(respond))
    await remote.get_status()
    status["matou"]["manifest_sha256"] = "0" * 64
    with pytest.raises(AgentToolError, match="清单版本已变化"):
        await remote.call(
            ToolName.FUNCTION_PROFILE, {"query": {"assay": "MetaG", "sample_name": "G-A"}}
        )


@pytest.mark.anyio
async def test_unconfigured_service_keeps_core_tools_without_local_matou(
    service_settings, tmp_path
):
    app = create_app(service_settings, token=TOKEN)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    local = Settings(
        data_service_url="http://127.0.0.1:8011", data_service_token_file=token, _env_file=None
    )
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=app))
    assert {t.name for t in await remote.list_tools()} == CORE_TOOLS
    with pytest.raises(AgentToolError, match="尚未启用"):
        await remote.call(
            ToolName.FUNCTION_PROFILE, {"query": {"assay": "MetaG", "sample_name": "G-A"}}
        )


def test_local_product_can_optionally_load_validated_matou(matou_setup):
    settings, _, _ = matou_setup
    app = create_product_app(settings, agent_model=FunctionModel())
    assert app.state.data_reader is not None
    invalid = settings.model_copy(update={"matou_data_dir": settings.matou_data_dir / "missing"})
    with pytest.raises(FileNotFoundError):
        create_product_app(invalid, agent_model=FunctionModel())


@pytest.mark.anyio
async def test_real_acceptance_entry_runs_against_both_assays(matou_setup):
    import runpy
    from pathlib import Path

    _, local, app = matou_setup
    verify = runpy.run_path(str(Path(__file__).parents[1] / "deploy/check_remote_matou.py"))[
        "verify"
    ]
    result = await verify(
        RemoteToolGateway(local, transport=httpx.ASGITransport(app=app)),
        {"MetaG": None, "MetaT": "T-A"},
    )
    assert result["status"] == "completed"
    assert [(c["assay"], c["sample_name"]) for c in result["checks"]] == [
        ("MetaG", "G-A"),
        ("MetaT", "T-A"),
    ]
    assert result["limitations"]


@pytest.mark.anyio
async def test_function_mcp_has_typed_output_and_read_only_annotations(matou_setup):
    from mcp import Client

    settings, _, _ = matou_setup
    server = create_server(
        ProcessedDataReader(settings.processed_data_dir),
        matou_reader=MatouDataReader(settings.matou_data_dir),
    )
    async with Client(server) as client:
        listed = await client.list_tools()
    tools = {t.name: t for t in listed.tools}
    for tool in MATOU_TOOLS:
        assert tools[tool].output_schema
        assert tools[tool].annotations.read_only_hint
        assert tools[tool].annotations.open_world_hint is False
