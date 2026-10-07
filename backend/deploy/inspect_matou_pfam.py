"""核验已提取 Pfam；可导出去重候选映射与阈值敏感性计数，不计算功能丰度。"""

import argparse
import csv
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

HEADER = [
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
BINS = [Decimal(s) for s in ("0", "1e-10", "1e-5", "1e-3", "1")]
LABELS = ["zero", "(0,1e-10]", "(1e-10,1e-5]", "(1e-5,1e-3]", "(1e-3,1]", ">1"]


def fingerprint(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino


def describe(path):
    before = fingerprint(path)
    with path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if fingerprint(path) != before:
        raise ValueError(f"文件发生变化：{path}")
    return {"path": str(path), "size_bytes": before[0], "sha256": checksum}


def inspect(
    prepared: Path,
    max_hits: int = 10_000_000,
    *,
    output_dir: Path | None = None,
    sensitivity_evalues: tuple[str, ...] = ("1e-5", "1e-3"),
) -> dict:
    if max_hits <= 0:
        raise ValueError("命中数量保护上限必须为正数")
    if not 1 <= len(sensitivity_evalues) <= 8:
        raise ValueError("敏感性条件数量必须为 1 到 8")
    try:
        thresholds = [Decimal(s) for s in sensitivity_evalues]
    except InvalidOperation as error:
        raise ValueError("敏感性 E-value 不是数值") from error
    if any(not t.is_finite() or t < 0 for t in thresholds) or len(set(thresholds)) != len(
        thresholds
    ):
        raise ValueError("敏感性 E-value 必须有限、非负且不重复")
    prepared = prepared.resolve(strict=True)
    manifest_path, path = prepared / "report.json", prepared / "pfam_hits.tsv"
    if manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("准备报告超过大小保护上限")
    states = {p: fingerprint(p) for p in (manifest_path, path, Path(__file__))}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed" or manifest.get("script_version") != "2":
        raise ValueError("需要准备脚本 v2 的完整成功报告")
    expected = manifest["outputs"]["pfam_hits"]
    if states[path][0] != expected["size_bytes"]:
        raise ValueError("Pfam 文件大小与准备报告不符")
    if output_dir is not None:
        output_dir = output_dir.resolve()
        original = manifest["pfam"].get("source", {}).get("path")
        if (
            output_dir.is_relative_to(prepared)
            or prepared.is_relative_to(output_dir)
            or (original and output_dir.is_relative_to(Path(original).resolve().parent))
        ):
            raise ValueError("输出目录须与原始目录和已有准备目录分开")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(exist_ok=False)
    started = time.monotonic()
    families: dict[str, dict[int, Decimal]] = defaultdict(dict)
    gene_family_counts: Counter[int] = Counter()
    frames: Counter[str] = Counter()
    evalue_bins = dict.fromkeys(LABELS, 0)
    ranges = {field: [None, None] for field in ("domainBitScore", "i-Evalue", "domainAccuracy")}
    count = pairs = 0
    repeated_examples = []
    print(f"开始检查已提取的 Pfam：{path}", file=sys.stderr, flush=True)
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t", strict=True)
        if reader.fieldnames != HEADER:
            raise ValueError("Pfam 表头不是已确认的十三列")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"第 {reader.line_num} 行不是十三列")
            if count >= max_hits:
                raise ValueError("命中数量超过保护上限")
            text = row["geneID"]
            if not text.isascii() or not text.isdigit() or int(text) <= 0:
                raise ValueError(f"第 {reader.line_num} 行 geneID 不是正整数")
            gene = int(text)
            pfam = row["pfamAcc"]
            if not pfam or pfam != pfam.strip() or not row["frame"]:
                raise ValueError(f"第 {reader.line_num} 行 Pfam 或 frame 为空或无效")
            for field in ranges:
                try:
                    value = Decimal(row[field])
                except InvalidOperation as error:
                    raise ValueError(f"第 {reader.line_num} 行 {field} 不是数值") from error
                if not value.is_finite() or (field == "i-Evalue" and value < 0):
                    raise ValueError(f"第 {reader.line_num} 行 {field} 无效")
                low, high = ranges[field]
                ranges[field] = [
                    value if low is None else min(low, value),
                    value if high is None else max(high, value),
                ]
                if field == "i-Evalue":
                    evalue = value
                    index = next((i for i, limit in enumerate(BINS) if value <= limit), len(BINS))
                    evalue_bins[LABELS[index]] += 1
            seen = families[pfam]
            if gene in seen:
                seen[gene] = min(seen[gene], evalue)
                if len(repeated_examples) < 5:
                    repeated_examples.append({"geneID": gene, "pfamAcc": pfam})
            else:
                seen[gene] = evalue
                gene_family_counts[gene] += 1
                pairs += 1
            frames[row["frame"]] += 1
            count += 1
            if count % 1_000_000 == 0:
                print(
                    f"已检查 {count:,} 条，耗时 {time.monotonic() - started:.0f} 秒",
                    file=sys.stderr,
                    flush=True,
                )
    if count == 0 or count != manifest["pfam"]["selected_hit_records"]:
        raise ValueError("Pfam 命中数量与准备报告不符或为空")
    if len(gene_family_counts) != manifest["pfam"]["selected_genes_with_pfam"]:
        raise ValueError("Pfam 基因数量与准备报告不符")
    source_info = describe(path)
    if source_info["sha256"] != expected["sha256"]:
        raise ValueError("Pfam SHA-256 与准备报告不符")
    manifest_info, script_info = describe(manifest_path), describe(Path(__file__).resolve())
    if any(fingerprint(p) != state for p, state in states.items()):
        raise ValueError("检查期间输入或脚本发生变化")
    report = {
        "status": "completed",
        "script_version": "2",
        "scope": "prepared_pfam_hits",
        "script": script_info,
        "preparation_report": manifest_info,
        "source": source_info,
        "selection": manifest["selection"],
        "total_hit_records": count,
        "unique_gene_pfam_pairs": pairs,
        "same_gene_pfam_extra_hit_records": count - pairs,
        "genes_with_pfam": len(gene_family_counts),
        "genes_with_multiple_distinct_pfams": sum(n > 1 for n in gene_family_counts.values()),
        "unique_pfam_accessions": len(families),
        "repeated_pair_examples": repeated_examples,
        "frame_hit_counts": dict(frames),
        "i_Evalue_hit_bins": evalue_bins,
        "quality_ranges": {k: {"min": str(v[0]), "max": str(v[1])} for k, v in ranges.items()},
        "parameters": {"max_hits": max_hits},
        "validation": {"prepared_output_digest_and_counts_match": True, "inputs_unchanged": True},
        "limitations": [
            "同一 gene–Pfam 多次命中可能是重复结构域或不同翻译框，不等于错误记录。",
            "质量分箱按原始命中计数，仅用于探查，不是采用的筛选阈值或显著性结论。",
            "未检查结构域重叠、翻译框冲突或生物学功能正确性；原始命中不修改。",
            "此报告不计算 MetaG/MetaT 功能丰度，不核验定量单位或样本配对。",
        ],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    if output_dir is not None:
        report["mapping_method"] = {
            "pair": "unique_geneID_pfamAcc",
            "min_iEvalue": "minimum_over_all_original_hits_and_frames",
            "quality_policy": "all_original_pairs_preserved_no_threshold_selected",
        }
        report["parameters"]["sensitivity_evalues"] = list(sensitivity_evalues)
        sensitivity = []
        for limit, label in zip(thresholds, sensitivity_evalues, strict=True):
            genes: set[int] = set()
            eligible_pairs = eligible_families = 0
            for hits in families.values():
                retained = 0
                for gene, evalue in hits.items():
                    if evalue <= limit:
                        genes.add(gene)
                        retained += 1
                eligible_pairs += retained
                eligible_families += retained > 0
            sensitivity.append(
                {
                    "condition": "min_iEvalue <= threshold",
                    "threshold": label,
                    "unique_gene_pfam_pairs": eligible_pairs,
                    "genes_with_pfam": len(genes),
                    "unique_pfam_accessions": eligible_families,
                }
            )
        report["threshold_sensitivity_counts"] = sensitivity
        report["limitations"].extend(
            [
                "候选映射不保留结构域坐标或翻译框，不能用来解析蛋白结构域排列。",
                "敏感性条件不是 Pfam 家族的官方 gathering 阈值，不表示已验证功能归属。",
            ]
        )
        partial = output_dir / "gene_pfam.tsv.partial"
        print("正在写出去重候选映射……", file=sys.stderr, flush=True)
        with partial.open("x", encoding="utf-8", newline="") as target:
            target.write("geneID\tpfamAcc\tmin_iEvalue\n")
            # 固定顺序方便复现；同一基因的不同 Pfam 都保留，不强制分摊贡献。
            for pfam in sorted(families):
                for gene in sorted(families[pfam]):
                    target.write(f"{gene}\t{pfam}\t{families[pfam][gene]}\n")
        info = describe(partial)
        final = partial.with_suffix("")
        info["path"] = str(final)
        report["outputs"] = {"gene_pfam": info}
        if any(fingerprint(p) != state for p, state in states.items()):
            raise ValueError("写出映射期间输入或脚本发生变化；不发布结果")
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        partial.rename(final)
        report_partial = output_dir / "report.json.partial"
        report_partial.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report_partial.rename(output_dir / "report.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--max-hits", type=int, default=10_000_000)
    parser.add_argument("--output-dir", type=Path, help="导出去重候选映射及报告的新目录")
    parser.add_argument(
        "--sensitivity-evalue", action="append", help="仅比较覆盖，不采用为筛选规则"
    )
    args = parser.parse_args()
    try:
        report = inspect(
            args.prepared_dir,
            args.max_hits,
            output_dir=args.output_dir,
            sensitivity_evalues=tuple(args.sensitivity_evalue or ("1e-5", "1e-3")),
        )
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
        print(f"Pfam 检查失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "检查已停止；输入文件未改动，无完整 report.json 的输出目录不可使用。", file=sys.stderr
        )
        return 130
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
