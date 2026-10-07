"""验收真实服务的目标 Pfam 核酸序列、geneID 查询、分页及来源绑定。"""

import argparse
import asyncio
import json
from pathlib import Path

from tara_agent.agent.models import ToolName
from tara_agent.analysis.function_atlas_models import SequenceResult
from tara_agent.config import Settings
from tara_agent.data.matou_artifacts import FileRecord
from tara_agent.data_service.client import RemoteToolGateway


async def verify(gateway, *, pfams=("PF00504", "PF03382", "PF00313")):
    status = await gateway.get_status()
    if status.matou is None or status.matou.study_manifest_sha256 is None:
        raise ValueError("服务缺少 MATOU 研究清单")
    checks, fasta_hashes = [], set()
    for family in pfams:
        result = SequenceResult.model_validate(
            await gateway.call(
                ToolName.RETRIEVE_GENE_SEQUENCES,
                {"query": {"pfam_accession": family, "limit": 2}},
            )
        )
        if result.total < 1 or not result.sequences:
            raise ValueError(f"{family} 没有真实序列，不能验收为可用")
        filters = result.metadata.provenance.filters
        evidence = filters["sequence_evidence"]
        source = FileRecord.model_validate(evidence["source"])
        if (
            filters["manifest_sha256"] != status.matou.manifest_sha256
            or filters["study_manifest_sha256"] != status.matou.study_manifest_sha256
            or evidence["study_manifest_sha256"] != status.matou.study_manifest_sha256
            or family not in evidence["selection"]["pfam_accessions"]
            or evidence["selection"]["header_mapping"] != "exact_MATOU-v1.5.geneID"
        ):
            raise ValueError("序列结果范围或来源与服务版本不一致")
        fasta_hashes.add(source.sha256)
        first = result.sequences[0]
        lookup = SequenceResult.model_validate(
            await gateway.call(
                ToolName.RETRIEVE_GENE_SEQUENCES, {"query": {"gene_ids": [first.geneID]}}
            )
        )
        if lookup.total != 1 or lookup.sequences != [first] or lookup.unavailable_gene_ids:
            raise ValueError("按 geneID 与 Pfam 查询的序列不一致")
        if result.total > 1:
            page = SequenceResult.model_validate(
                await gateway.call(
                    ToolName.RETRIEVE_GENE_SEQUENCES,
                    {"query": {"pfam_accession": family, "limit": 1, "offset": 1}},
                )
            )
            if page.total != result.total or page.sequences != result.sequences[1:2]:
                raise ValueError("序列分页结果不一致")
        checks.append({"pfam_accession": family, "total": result.total, "gene_id": first.geneID})
    if not checks or len(fasta_hashes) != 1:
        raise ValueError("验收范围为空或混用了多个 FASTA 来源")
    return {
        "status": "passed",
        "scope": "real_sequence_tools_no_model_or_browser_acceptance",
        "checks": checks,
        "fasta_sha256": next(iter(fasta_hashes)),
        "study_manifest_sha256": status.matou.study_manifest_sha256,
        "limitations": ["仅验收已准备目标 Pfam 的核酸序列，不证明功能或蛋白活性"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        parser.error("不能覆盖已有验收报告")
    report = asyncio.run(verify(RemoteToolGateway(Settings())))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text)
    print(text)


if __name__ == "__main__":
    main()
