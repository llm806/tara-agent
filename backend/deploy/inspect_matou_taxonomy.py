"""流式探查 MATOU taxonomy；可选导出基因清单与对应 Pfam，原始数据只读。"""

import argparse
import csv
import gzip
import hashlib
import json
import sys
import time
import zlib
from pathlib import Path
from typing import TextIO

HEADER = ["geneID", "taxId", "taxName", "taxRank", "taxLineage"]
PFAM_HEADER = [
    "HmmerMatchID",
    "geneID",
    "frame",
    "geneLength",
    "geneFrom",
    "geneTo",
    "pfamAcc",
    "pfamLength",
    "pfamFrom",
    "pfamTo",
    "domainBitScore",
    "i-Evalue",
    "domainAccuracy",
]
SCRIPT_VERSION = "2"


def positive_gene_id(value: str, line_number: int) -> int:
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise ValueError(f"第 {line_number} 行的 geneID 不是正整数")
    return int(value)


def source_fingerprint(path: Path) -> tuple:
    state = path.stat()
    return state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino


def source_description(path: Path, before: tuple) -> dict:
    with path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if source_fingerprint(path) != before:
        raise ValueError(f"处理期间源文件发生变化，结果无效：{path}")
    return {"path": str(path), "size_bytes": before[0], "sha256": checksum}


def inspect_taxonomy(
    path: Path,
    taxon: str,
    *,
    gene_ids: set[int] | None = None,
    gene_output: TextIO | None = None,
    max_selected_genes: int = 10_000_000,
) -> dict:
    path = path.resolve(strict=True)
    target = taxon.strip().casefold()
    if not target:
        raise ValueError("分类名称不能为空")
    before = source_fingerprint(path)
    started = time.monotonic()
    total = matched = self_matches = lineage_matches = duplicates = 0
    examples = []
    print(f"开始扫描：{path}；分类条件：{taxon}", file=sys.stderr, flush=True)

    # 读取到 EOF 才能验证整个 gzip；前几行不能证明压缩包完整。
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, delimiter="\t", strict=True)
        if next(reader, None) != HEADER:
            raise ValueError("taxonomy 表头与已确认的 MATOU v1.5 五列表头不一致")
        for row in reader:
            total += 1
            if len(row) != len(HEADER):
                raise ValueError(f"第 {reader.line_num} 行不是五列")
            gene_id = positive_gene_id(row[0], reader.line_num)
            own = row[2].strip().casefold() == target
            ancestor = any(part.strip().casefold() == target for part in row[4].split(";"))
            self_matches += own
            lineage_matches += ancestor
            if own or ancestor:
                matched += 1
                if gene_ids is not None:
                    if gene_id in gene_ids:
                        duplicates += 1
                    else:
                        if len(gene_ids) >= max_selected_genes:
                            raise ValueError("目标基因数量超过内存保护上限；停止，不发布结果")
                        gene_ids.add(gene_id)
                        if gene_output is not None:
                            gene_output.write(f"{gene_id}\n")
                if len(examples) < 10:
                    examples.append(dict(zip(HEADER, row, strict=True)))
            if total % 1_000_000 == 0:
                print(
                    f"已扫描 {total:,} 条，匹配 {matched:,} 条，"
                    f"耗时 {time.monotonic() - started:.0f} 秒",
                    file=sys.stderr,
                    flush=True,
                )

    if total == 0:
        raise ValueError("taxonomy 只有表头，没有注释记录")
    print("扫描完成，计算源文件 SHA-256……", file=sys.stderr, flush=True)
    source_info = source_description(path, before)

    # 统计的是注释记录，尚未去重，不能把匹配条数直接称为基因数。
    return {
        "status": "completed",
        "script_version": SCRIPT_VERSION,
        "source": source_info,
        "selection": {
            "taxon": taxon.strip(),
            "method": "exact_case_insensitive_taxName_or_semicolon_lineage_token",
        },
        "total_records": total,
        "matched_records": matched,
        "taxName_matches": self_matches,
        "taxLineage_matches": lineage_matches,
        "selected_unique_geneIDs": len(gene_ids) if gene_ids is not None else None,
        "selected_duplicate_records": duplicates if gene_ids is not None else None,
        "matched_examples": examples,
        "validation": {
            "full_gzip_read": True,
            "header_column_count_and_positive_geneIDs": True,
            "geneID_uniqueness": "selected_records_only" if gene_ids is not None else "not_checked",
            "biological_assignment_accuracy": "not_assessed",
        },
        "warnings": [
            "匹配条数是注释记录数，不是经过去重的基因数。",
            "taxName 与 taxLineage 两类匹配可能重叠，不应相加。",
            "该条件仅按已有注释匹配；未注释或分类错误的基因不会被纠正。",
            "Pfam、定量单位和样本配对尚未在此步骤核验。",
        ],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def prepare_pfam(
    taxonomy: Path,
    pfam: Path,
    taxon: str,
    output_dir: Path,
    expected_taxonomy_sha256: str,
    max_selected_genes: int = 10_000_000,
) -> dict:
    taxonomy, pfam = taxonomy.resolve(strict=True), pfam.resolve(strict=True)
    output_dir = output_dir.resolve()
    if taxonomy == pfam:
        raise ValueError("taxonomy 与 Pfam 必须是不同文件")
    if not taxon.strip() or max_selected_genes <= 0:
        raise ValueError("分类名称不能为空，目标基因上限必须为正数")
    if any(output_dir.is_relative_to(path.parent) for path in (taxonomy, pfam)):
        raise ValueError("产物目录不得位于原始数据目录内")
    if len(expected_taxonomy_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in expected_taxonomy_sha256.lower()
    ):
        raise ValueError("已核验的 taxonomy SHA-256 必须为 64 位十六进制值")
    started = time.monotonic()
    script_path = Path(__file__).resolve(strict=True)
    before = {path: source_fingerprint(path) for path in (taxonomy, pfam, script_path)}
    # 新目录独占一次运行；失败保留 partial 供定位，不覆盖旧结果。
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)
    genes_partial = output_dir / "gene_ids.tsv.partial"
    pfam_partial = output_dir / "pfam_hits.tsv.partial"
    gene_ids: set[int] = set()
    with genes_partial.open("x", encoding="utf-8", newline="") as target:
        target.write("geneID\n")
        taxonomy_report = inspect_taxonomy(
            taxonomy,
            taxon,
            gene_ids=gene_ids,
            gene_output=target,
            max_selected_genes=max_selected_genes,
        )
    if taxonomy_report["source"]["sha256"] != expected_taxonomy_sha256.lower():
        raise ValueError("taxonomy SHA-256 与此前核验结果不同；停止，不发布结果")
    if taxonomy_report["selected_duplicate_records"]:
        raise ValueError(
            f"目标分类记录中有 {taxonomy_report['selected_duplicate_records']:,} 条重复 geneID；"
            "需先核查重复注释，不发布结果"
        )
    if not gene_ids:
        raise ValueError("没有匹配基因；停止，不发布空结果")
    annotated: set[int] = set()
    total = matched = 0
    print(f"开始扫描 Pfam：{pfam}", file=sys.stderr, flush=True)
    with (
        gzip.open(pfam, "rt", encoding="utf-8-sig", newline="") as source,
        pfam_partial.open("x", encoding="utf-8", newline="") as target,
    ):
        reader = csv.reader(source, delimiter="\t", strict=True)
        if next(reader, None) != PFAM_HEADER:
            raise ValueError("Pfam 表头与已确认的 MATOU v1.5 十三列表头不一致")
        writer = csv.writer(target, delimiter="\t", lineterminator="\n")
        writer.writerow(PFAM_HEADER)
        for row in reader:
            total += 1
            if len(row) != len(PFAM_HEADER):
                raise ValueError(f"Pfam 第 {reader.line_num} 行不是十三列")
            gene_id = positive_gene_id(row[1], reader.line_num)
            if gene_id in gene_ids:
                # 保留全部命中及质量字段；此处不决定去重、阈值或功能聚合方法。
                writer.writerow(row)
                matched += 1
                annotated.add(gene_id)
            if total % 1_000_000 == 0:
                print(
                    f"Pfam 已扫描 {total:,} 条，保留 {matched:,} 条，"
                    f"涉及 {len(annotated):,} 个基因",
                    file=sys.stderr,
                    flush=True,
                )
    if total == 0:
        raise ValueError("Pfam 只有表头，没有注释记录")
    print("Pfam 扫描完成，计算源文件与产物 SHA-256……", file=sys.stderr, flush=True)
    pfam_source = source_description(pfam, before[pfam])
    for path in before:
        if source_fingerprint(path) != before[path]:
            raise ValueError(f"处理期间源文件发生变化，不发布结果：{path}")
    outputs = {}
    for key, partial in (("gene_ids", genes_partial), ("pfam_hits", pfam_partial)):
        final_path = partial.with_suffix("")
        info = source_description(partial, source_fingerprint(partial))
        info["path"] = str(final_path)
        outputs[key] = info
        partial.rename(final_path)
    report = {
        "status": "completed",
        "script_version": SCRIPT_VERSION,
        "script_sha256": source_description(script_path, before[script_path])["sha256"],
        "selection": taxonomy_report["selection"],
        "taxonomy": taxonomy_report,
        "pfam": {
            "source": pfam_source,
            "total_hit_records": total,
            "selected_hit_records": matched,
            "selected_genes_with_pfam": len(annotated),
            "selected_genes_without_pfam": len(gene_ids) - len(annotated),
            "gene_annotation_coverage": len(annotated) / len(gene_ids),
        },
        "outputs": outputs,
        "parameters": {"max_selected_genes": max_selected_genes},
        "validation": {
            "both_full_gzip_reads": True,
            "expected_taxonomy_sha256": True,
            "selected_taxonomy_geneIDs_unique": True,
            "pfam_header_column_count_and_positive_geneIDs": True,
            "source_files_unchanged_during_run": True,
        },
        "warnings": [
            "仅检查匹配分类记录内 geneID 唯一性，未核验全 taxonomy 中重复或冲突分类。",
            "Pfam 原始命中全部保留；重复 gene–Pfam、翻译框、重叠及质量阈值尚未处理。",
            "覆盖率按基因是否至少有一条原始 Pfam 命中计算，不代表经质量筛选的功能或信号覆盖率。",
            "分类准确性、FASTA 标识、MetaG/MetaT 定量单位、零值及样本映射仍未核验。",
            "当前仅为数据准备，不能解释为功能丰度或 RNA/DNA 比值。",
        ],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    # 完整 report 是完成标记；缺少它的目录不得供后续分析使用。
    report_partial = output_dir / "report.json.partial"
    report_partial.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_partial.rename(output_dir / "report.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="taxonomy.tsv.gz 的路径")
    parser.add_argument("--taxon", required=True, help="当前分类名称或完整祖先名称")
    parser.add_argument("--pfam", type=Path, help="提供此参数时提取对应 Pfam 原始命中")
    parser.add_argument("--output-dir", type=Path, help="新的产物目录，必须位于原始目录之外")
    parser.add_argument("--expected-taxonomy-sha256", help="此前完整探查确认的 taxonomy 摘要")
    parser.add_argument(
        "--max-selected-genes", type=int, default=10_000_000, help="目标基因数量保护上限"
    )
    args = parser.parse_args()
    if args.pfam is not None:
        if args.output_dir is None or args.expected_taxonomy_sha256 is None:
            parser.error("--pfam 必须同时提供 --output-dir 和 --expected-taxonomy-sha256")
    elif args.output_dir is not None or args.expected_taxonomy_sha256 is not None:
        parser.error("导出参数必须与 --pfam 一起使用")
    try:
        if args.pfam is None:
            report = inspect_taxonomy(args.input, args.taxon)
        else:
            report = prepare_pfam(
                args.input,
                args.pfam,
                args.taxon,
                args.output_dir,
                args.expected_taxonomy_sha256,
                args.max_selected_genes,
            )
    except (OSError, EOFError, ValueError, csv.Error, zlib.error) as error:
        print(f"探查失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("探查已中止；原始文件没有改动。", file=sys.stderr)
        return 130
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
