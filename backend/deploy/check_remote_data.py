"""验证远程真实数据、六个工具及来源链路，不需要模型或产品数据库。"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from tara_agent.agent.models import CORE_TOOLS, ToolName
from tara_agent.config import Settings
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.observability.execution import bind_execution_trace


async def main() -> None:
    gateway = RemoteToolGateway(Settings())
    status = await gateway.get_status()
    coverage = status.manifest.coverage
    assert (coverage.context_samples, coverage.v4_samples, coverage.v9_samples) == (
        1434,
        1011,
        1069,
    )
    assert {tool.name for tool in await gateway.list_tools()} >= CORE_TOOLS
    sample_ids = status.manifest.artifacts["v4_abundance"].repeated_columns[:1]
    assert sample_ids
    requests = [
        (ToolName.FIND_SAMPLES, {"query": {"limit": 1}}, "items"),
        (ToolName.GET_SAMPLE_INFO, {"sample_id": sample_ids[0]}, "sample"),
        (
            ToolName.FIND_TAXA,
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Bacillariophyta",
                    "asv_limit": 1,
                    "sample_limit": 1,
                }
            },
            "asvs",
        ),
        (
            ToolName.TAXON_ABUNDANCE,
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Bacillariophyta",
                    "sample_ids": sample_ids,
                    "limit": 1,
                }
            },
            "observations",
        ),
        (
            ToolName.DIVERSITY_ANALYSIS,
            {
                "query": {
                    "marker": "v4",
                    "sample_ids": sample_ids,
                    "limit": 1,
                }
            },
            "observations",
        ),
        (
            ToolName.ENVIRONMENT_ASSOCIATION,
            {
                "query": {
                    "marker": "v4",
                    "taxon": "Bacillariophyta",
                    "sample_ids": sample_ids,
                    "environment_variable": "temperature",
                    "point_limit": 1,
                }
            },
            "points",
        ),
    ]
    v9_samples = status.manifest.artifacts["v9_abundance"].repeated_columns[:2]
    requests.extend(
        [
            (
                ToolName.TAXON_ABUNDANCE,
                {
                    "query": {
                        "marker": "v9",
                        "taxon": "Bacillariophyta",
                        "group_by": "station",
                        "aggregation": "mean",
                        "limit": 10,
                    }
                },
                "station_leaders",
            ),
            (
                ToolName.TAXON_ABUNDANCE,
                {
                    "query": {
                        "marker": "v9",
                        "taxon": "Bacillariophyta",
                        "sample_ids": v9_samples,
                        "taxonomic_rank": "genus",
                        "limit": 10,
                    }
                },
                "composition_summaries",
            ),
        ]
    )
    for name, arguments, result_key in requests:
        events = []
        parent = str(uuid4())
        with bind_execution_trace(parent_id=parent, writer=events.append):
            result = await gateway.call(name, arguments)
        assert result_key in result
        if result_key == "station_leaders":
            assert result[result_key] and result["group_page"]["total"] > 0
            assert any(row["mean_rank"] == 1 for row in result[result_key])
            assert any(row["max_rank"] == 1 for row in result[result_key])
        if result_key == "composition_summaries":
            assert {row["sample_id"] for row in result[result_key]} == set(v9_samples)
            for row in result["composition"]:
                summary = next(s for s in result[result_key] if s["sample_id"] == row["sample_id"])
                assert (
                    abs(
                        row["fraction_within_selected_taxon"]
                        - row["read_count"] / summary["selected_taxon_read_count"]
                    )
                    < 1e-12
                )
        if name is ToolName.FIND_TAXA:
            assert result["asv_page"]["total"] == 5173
        if name is ToolName.FIND_SAMPLES:
            assert result["page"]["total"] == 1434
        assert events and events[0]["parent_id"] == parent
        assert any(event["span_kind"] == "data" for event in events)
        print(f"PASS: {name.value}")
    suggestions = await gateway.get_batch(limit=4)
    assert len(suggestions) == 4
    print(
        json.dumps(
            {
                "status": "ok",
                "generation": status.manifest.generation,
                "analysis_code_sha256": status.code_sha256,
                "remote_tools_verified": len(requests),
                "suggestions_verified": len(suggestions),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
