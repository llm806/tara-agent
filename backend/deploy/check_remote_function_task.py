"""真实服务器复杂功能任务的算术、版本、范围和同树 Trace 验收；不调用模型。"""

import argparse
import asyncio
import json
import math
from pathlib import Path
from uuid import uuid4

from tara_agent.agent.models import ToolName
from tara_agent.analysis.function_atlas_models import AtlasResult, ComparisonResult
from tara_agent.config import Settings
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.observability.execution import bind_execution_trace


async def verify(gateway, *, pfam_accessions=("PF00504", "PF03382", "PF00313")):
    status = await gateway.get_status()
    if status.matou is None or status.matou.study_manifest_sha256 is None:
        raise ValueError("真实 MATOU 服务需要先准备 study_manifest.json 并重启")
    events = []
    parent = str(uuid4())
    with bind_execution_trace(parent_id=parent, writer=events.append):
        raw = await gateway.call(
            ToolName.FUNCTION_ATLAS, {"query": {"assay": "MetaT", "top_n": 100}}
        )
        atlas = AtlasResult.model_validate(raw)
        if atlas.sample_count != status.matou.sample_counts["MetaT"] or not atlas.ranks:
            raise ValueError("全样本功能谱范围或排名为空")
        for rank in atlas.ranks:
            points = [p for p in atlas.observations if p.pfam_accession == rank.pfam_accession]
            computed = math.fsum(
                p.observed_fraction for p in points if p.observed_fraction is not None
            )
            computed /= atlas.informative_samples
            if not math.isclose(
                computed, rank.mean_observed_contribution, rel_tol=1e-12, abs_tol=1e-15
            ):
                raise ValueError("总体排名与逐样本观测的算术不一致")
        comparison = ComparisonResult.model_validate(
            await gateway.call(
                ToolName.COMPARE_FUNCTION_SIGNALS,
                {"query": {"pfam_accessions": list(pfam_accessions)}},
            )
        )
        if comparison.matched_sampling_keys < 3:
            raise ValueError("合格采样编码少于3，不能验收代表性比较任务")
        if not any(s.complete_correspondences >= 3 for s in comparison.summaries):
            raise ValueError("目标家族没有至少3个完整观测对应，不能将空结果算作任务验收通过")
        for point in comparison.points:
            if point.fraction_difference is not None and not math.isclose(
                point.metat_fraction - point.metag_fraction,
                point.fraction_difference,
                abs_tol=1e-15,
            ):
                raise ValueError("目标功能差值算术不一致")
        for result in (atlas, comparison):
            filters = result.metadata.provenance.filters
            if (
                filters["manifest_sha256"] != status.matou.manifest_sha256
                or filters["study_manifest_sha256"] != status.matou.study_manifest_sha256
            ):
                raise ValueError("结果来源与服务版本不一致")
    known, opened = {parent}, set()
    for event in events:
        if event["parent_id"] not in known:
            raise ValueError("科学服务或数据访问没有真实父节点")
        node = event["observation_id"]
        if event["phase"] == "started":
            known.add(node)
            opened.add(node)
        else:
            opened.remove(node)
    if opened or not events:
        raise ValueError("执行树为空或存在未关闭节点")
    return {
        "status": "passed",
        "scope": "real_tools_no_model_or_database_acceptance",
        "matou": status.matou.model_dump(),
        "sample_count": atlas.sample_count,
        "top_family_count": len(atlas.ranks),
        "matched_sampling_codes": comparison.matched_sampling_keys,
        "summaries": [s.model_dump() for s in comparison.summaries],
        "observation_events": len(events),
        "method_version": atlas.method_version,
        "limitations": [
            "候选功能，不是家族 gathering 阈值验证",
            "编码对应不证明同一提取物",
            "未验收真实模型、数据库或序列提取",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        parser.error("不能覆盖已有验收报告")
    report = asyncio.run(verify(RemoteToolGateway(Settings())))
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text)
    print(text)


if __name__ == "__main__":
    main()
