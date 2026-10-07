"""从本地验收 MATOU 远程工具、来源、算术一致性和 Trace，不读取本机科学数据。"""

import argparse
import asyncio
import json
import math
from pathlib import Path
from uuid import uuid4

from tara_agent.agent.models import MATOU_TOOLS, ToolName
from tara_agent.analysis.function_query_models import FunctionSamplesResult
from tara_agent.analysis.function_signal_models import FunctionSignalResult
from tara_agent.config import Settings
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.observability.execution import bind_execution_trace


async def verify(gateway: RemoteToolGateway, requested: dict[str, str | None]) -> dict:
    status = await gateway.get_status()
    if status.matou is None or not {t.name for t in await gateway.list_tools()} >= MATOU_TOOLS:
        raise ValueError("远程服务未开放 MATOU 工具")
    checks = []
    for assay, sample_name in requested.items():
        listing = None
        selected = None
        for offset in range(0, 20000, 200):
            listing = FunctionSamplesResult.model_validate(
                await gateway.call(
                    ToolName.FIND_FUNCTION_SAMPLES,
                    {"query": {"assay": assay, "offset": offset, "limit": 200}},
                )
            )
            selected = next(
                (
                    s
                    for s in listing.items
                    if (
                        s.sample_name == sample_name if sample_name else s.observed_gene_records > 0
                    )
                ),
                None,
            )
            if selected is not None or offset + 200 >= listing.total:
                break
        if selected is None:
            raise ValueError(f"{assay} 找不到指定样本或非空验收样本")
        events = []
        parent = str(uuid4())
        with bind_execution_trace(parent_id=parent, writer=events.append):
            result = FunctionSignalResult.model_validate(
                await gateway.call(
                    ToolName.FUNCTION_PROFILE,
                    {"query": {"assay": assay, "sample_name": selected.sample_name, "top_n": 5}},
                )
            )
        if (
            result.context.assay != assay
            or result.context.sample_name != selected.sample_name
            or result.context.taxon != status.matou.taxon
            or result.context.dataset_version != status.matou.dataset_version
            or result.observed_gene_count != selected.observed_gene_records
            or result.metadata.provenance.filters.get("manifest_sha256")
            != status.matou.manifest_sha256
        ):
            raise ValueError("结果范围、数量或来源与清单不符")
        if not events or events[0]["parent_id"] != parent:
            raise ValueError("结果缺少正确的 Trace 父节点")
        if not {"service", "data"} <= {e["span_kind"] for e in events}:
            raise ValueError("缺少服务或数据访问节点")
        for item in result.observations:
            fraction = item.fraction_of_observed_taxon_signal
            if fraction is not None and (
                item.value_sum is None
                or result.taxon_value_sum <= 0
                or not math.isclose(
                    fraction, item.value_sum / result.taxon_value_sum, rel_tol=1e-12
                )
            ):
                raise ValueError("候选功能份额与分子分母不一致")
        checks.append(
            {
                "assay": assay,
                "sample_name": selected.sample_name,
                "observed_genes": result.observed_gene_count,
                "taxon_value_sum": result.taxon_value_sum,
                "annotated_signal_fraction": result.annotated_signal_fraction,
                "candidate_families": [i.model_dump() for i in result.observations],
                "trace_events": len(events),
            }
        )
    return {
        "status": "completed",
        "scope": "two_remote_samples_contract_and_arithmetic",
        "matou": status.matou.model_dump(),
        "analysis_code_sha256": status.code_sha256,
        "checks": checks,
        "limitations": ["本次验收不证明定量单位、功能正确性、跨样本可比性或 DNA/RNA 配对。"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-g-sample")
    parser.add_argument("--meta-t-sample")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # 未指定时仅为接口验收选择各实验首个非空样本，不替用户制定科研样本范围。
    result = asyncio.run(
        verify(
            RemoteToolGateway(Settings()),
            {
                "MetaG": args.meta_g_sample,
                "MetaT": args.meta_t_sample,
            },
        )
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    print(payload, end="")


if __name__ == "__main__":
    main()
