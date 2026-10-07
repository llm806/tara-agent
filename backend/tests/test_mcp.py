from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer

from tara_agent.data.preprocess import preprocess
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.mcp import create_server


@pytest.fixture
def mcp_server(preprocessable_dataset_dir: Path, tmp_path: Path) -> MCPServer:
    processed_dir = tmp_path / "processed"
    preprocess(
        preprocessable_dataset_dir,
        processed_dir,
        enforce_expected_shape=False,
    )
    return create_server(ProcessedDataReader(processed_dir))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_server_exposes_core_read_only_tools(mcp_server: MCPServer) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {
        "find_samples",
        "get_sample_info",
        "find_taxa",
        "taxon_abundance",
        "diversity_analysis",
        "environment_association",
        "community_analysis",
    }
    assert all(tool.annotations.read_only_hint for tool in tools.values())
    assert all(tool.annotations.open_world_hint is False for tool in tools.values())
    assert all(tool.output_schema is not None for tool in tools.values())
    assert all(
        "examples" in next(iter(tool.input_schema["properties"].values()))
        for tool in tools.values()
        if tool.name != "community_analysis"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_key"),
    [
        ("find_samples", {"query": {"limit": 1}}, "items"),
        ("get_sample_info", {"sample_id": "TARA_TEST_001"}, "sample"),
        (
            "find_taxa",
            {"query": {"marker": "v4", "taxon": "Dinoflagellata"}},
            "asvs",
        ),
        (
            "taxon_abundance",
            {"query": {"marker": "v4", "taxon": "Dinoflagellata"}},
            "observations",
        ),
        ("diversity_analysis", {"query": {"marker": "v4"}}, "observations"),
        (
            "environment_association",
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Dinoflagellata",
                    "environment_variable": "temperature",
                }
            },
            "points",
        ),
    ],
)
async def test_tools_return_structured_results(
    mcp_server: MCPServer,
    tool_name: str,
    arguments: dict[str, Any],
    expected_key: str,
) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool(tool_name, arguments)

    assert result.is_error is False
    assert result.structured_content is not None
    assert expected_key in result.structured_content


@pytest.mark.anyio
async def test_recoverable_service_error_is_visible_to_model(mcp_server: MCPServer) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "get_sample_info",
            {"sample_id": "TARA_UNKNOWN"},
        )

    assert result.is_error is True
    assert result.structured_content is None
    assert "Unknown sample ID" in result.content[0].text


@pytest.mark.anyio
async def test_find_samples_normalizes_surface_alias(mcp_server: MCPServer) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_samples",
            {"query": {"depths": ["表层"], "limit": 10}},
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["page"]["total"] == 2
    filters = result.structured_content["metadata"]["provenance"]["filters"]
    assert filters["depths"] == ["SRF"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("find_samples", {"query": {"limit": 999}}),
        ("find_samples", {"query": {"depths": ["abyss"]}}),
        ("get_sample_info", {"sample_id": ["not", "a", "string"]}),
        ("find_taxa", {"query": {"marker": "v18", "taxon": "Eukaryota"}}),
        ("taxon_abundance", {"query": {"marker": "v4", "taxon": " "}}),
        ("diversity_analysis", {"query": {"marker": "v4", "sample_ids": []}}),
        (
            "environment_association",
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Eukaryota",
                    "environment_variable": "salinity",
                }
            },
        ),
    ],
)
async def test_invalid_input_is_rejected_by_generated_schema(
    mcp_server: MCPServer,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool(tool_name, arguments)

    assert result.is_error is True
    assert result.structured_content is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "find_taxa",
            {"query": {"marker": "v4", "taxon": "x" * 201}},
        ),
        (
            "taxon_abundance",
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Dinoflagellata",
                    "sample_ids": [f"sample-{index}" for index in range(501)],
                }
            },
        ),
        (
            "environment_association",
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Dinoflagellata",
                    "environment_variable": "password",
                }
            },
        ),
    ],
)
async def test_tools_reject_out_of_scope_inputs(
    mcp_server: MCPServer,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    async with Client(mcp_server, raise_exceptions=True) as client:
        result = await client.call_tool(tool_name, arguments)

    assert result.is_error is True
