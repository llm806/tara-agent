"""独立准备、列出样本或执行单样本候选分析，不需要模型和产品数据库。"""

import argparse
import json
import sys
import zlib
from pathlib import Path

import polars as pl

from tara_agent.analysis.function_atlas import MatouAtlasService
from tara_agent.analysis.function_atlas_models import (
    AtlasQuery,
    FunctionComparisonQuery,
    SequenceQuery,
)
from tara_agent.analysis.function_service import MatouFunctionService
from tara_agent.analysis.function_signal_models import FunctionSignalQuery
from tara_agent.analysis.function_study_prepare import prepare_study
from tara_agent.data.matou_prepare import PreparationLimits, prepare_function_data
from tara_agent.data.matou_reader import MatouDataReader


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    study = commands.add_parser("prepare-study", help="准备跨样本功能谱缓存和可选的目标核酸序列")
    study.add_argument("--data-dir", type=Path, required=True)
    study.add_argument("--max-i-evalue", type=float)
    study.add_argument("--fasta", type=Path)
    study.add_argument("--sequence-pfam", action="append", default=[])
    study.add_argument("--max-seconds", type=float, default=43200)
    study.add_argument("--min-free-gib", type=float, default=2)
    study.add_argument("--max-bases", type=int, default=100000000)
    for name in ("atlas", "compare", "sequences"):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", type=Path, required=True)
        if name == "atlas":
            command.add_argument("--assay", choices=("MetaG", "MetaT"), default="MetaT")
            command.add_argument("--sample", action="append")
            command.add_argument("--top-n", type=int, default=20)
        if name in ("atlas", "compare"):
            command.add_argument("--pfam", action="append", required=name == "compare")
            command.add_argument("--max-i-evalue", type=float)
        else:
            command.add_argument("--pfam")
            command.add_argument("--gene-id", type=int, action="append")
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--offset", type=int, default=0)
    prepare = commands.add_parser("prepare", help="完整转换已提取子集，新目录最后发布清单")
    for option in ("prepared-dir", "occurrences-dir", "mapping-dir", "output-dir"):
        prepare.add_argument(f"--{option}", type=Path, required=True)
    for option, default in PreparationLimits().model_dump().items():
        prepare.add_argument(f"--{option.replace('_', '-')}", type=type(default), default=default)
    for name in ("samples", "analyze"):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", type=Path, required=True)
        command.add_argument("--assay", choices=("MetaG", "MetaT"), required=True)
        if name == "analyze":
            command.add_argument("--sample", required=True)
            command.add_argument("--pfam", action="append")
            command.add_argument("--top-n", type=int, default=20)
            command.add_argument("--max-i-evalue", type=float)
    args = parser.parse_args()
    try:
        if args.command == "prepare-study":
            result = prepare_study(
                args.data_dir,
                max_i_evalue=args.max_i_evalue,
                fasta=args.fasta,
                sequence_pfams=args.sequence_pfam,
                max_seconds=args.max_seconds,
                min_free_gib=args.min_free_gib,
                max_bases=args.max_bases,
            ).model_dump()
        elif args.command in ("atlas", "compare", "sequences"):
            service = MatouAtlasService(MatouDataReader(args.data_dir))
            if args.command == "atlas":
                result = service.atlas(
                    AtlasQuery(
                        assay=args.assay,
                        sample_names=args.sample,
                        top_n=args.top_n,
                        pfam_accessions=args.pfam,
                        max_i_evalue=args.max_i_evalue,
                    )
                ).model_dump()
            elif args.command == "compare":
                result = service.compare(
                    FunctionComparisonQuery(
                        pfam_accessions=args.pfam, max_i_evalue=args.max_i_evalue
                    )
                ).model_dump()
            else:
                result = service.sequences(
                    SequenceQuery(
                        gene_ids=args.gene_id,
                        pfam_accession=args.pfam,
                        limit=args.limit,
                        offset=args.offset,
                    )
                ).model_dump()
        elif args.command == "prepare":
            limits = PreparationLimits(
                **{key: getattr(args, key) for key in PreparationLimits.model_fields}
            )
            manifest = prepare_function_data(
                args.prepared_dir, args.occurrences_dir, args.mapping_dir, args.output_dir, limits
            )
            result = {
                "status": manifest.status,
                "data_dir": str(args.output_dir.resolve()),
                "sample_count": len(manifest.samples),
                "pipeline_version": manifest.pipeline_version,
            }
        else:
            reader = MatouDataReader(args.data_dir)
            if args.command == "samples":
                result = {
                    "taxon": reader.manifest.selection["taxon"],
                    "assay": args.assay,
                    "samples": [
                        {"sample_name": s.sample_name, "observed_records": s.artifact.rows}
                        for s in reader.list_samples(args.assay)
                    ],
                }
            else:
                query = FunctionSignalQuery(
                    pfam_accessions=args.pfam, top_n=args.top_n, max_i_evalue=args.max_i_evalue
                )
                result = (
                    MatouFunctionService(reader)
                    .analyze_sample(args.assay, args.sample, query)
                    .model_dump()
                )
    except KeyboardInterrupt:
        print("已停止；没有完整 manifest.json 的转换目录不可使用。", file=sys.stderr)
        return 130
    except (
        OSError,
        EOFError,
        ValueError,
        KeyError,
        TypeError,
        zlib.error,
        pl.exceptions.PolarsError,
    ) as error:
        print(f"MATOU 操作失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
