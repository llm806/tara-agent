"""核验每个 assay 的首个完整样本、原值一致性及 Pfam 信号覆盖，不推断单位。"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
import time
import zlib
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path

LINE_LIMIT = 16_384
SMALL_FILE_LIMIT = 4 * 1024 * 1024
OCCURRENCE_HEADER = b"geneid\tsamplename\tvalue"
PFAM_HEADER = (
    b"HmmerMatchID\tgeneID\tframe\tgeneLength\tgeneFrom\tgeneTo\tpfamAcc\tpfamLength"
    b"\tpfamFrom\tpfamTo\tdomainBitScore\ti-Evalue\tdomainAccuracy"
)
POLICIES = {
    "all_original_pairs": None,
    "min_iEvalue_le_1e-5": Decimal("1e-5"),
    "min_iEvalue_le_1e-3": Decimal("1e-3"),
}


def fingerprint(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino


def small_file(path):
    before = fingerprint(path)
    if before[0] > SMALL_FILE_LIMIT:
        raise ValueError(f"报告或样本清单超过大小上限：{path}")
    data = path.read_bytes()
    if fingerprint(path) != before:
        raise ValueError(f"读取时文件发生变化：{path}")
    return data, {
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def positive_id(text):
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError("geneID 必须为 ASCII 正整数")
    return int(text)


def nonnegative(text):
    try:
        value = Decimal(text)
    except DecimalException as error:
        raise ValueError("数值格式无效") from error
    if not value.is_finite() or value < 0:
        raise ValueError("数值必须有限且非负")
    return value


def checked_line(source):
    line = source.readline(LINE_LIMIT + 1)
    if len(line) > LINE_LIMIT:
        raise ValueError("数据行超过长度上限")
    return line


def check_budget(deadline):
    if time.monotonic() >= deadline:
        raise ValueError("达到时间上限；不发布未完成样本的统计")


def first_block(path, sample, expected_count, row_limit, deadline, *, keep=False, wanted=None):
    """只存目标类群的小样本；原表求和不保留全部基因。边界行纳入前缀摘要。"""
    state = fingerprint(path)
    checksum = hashlib.sha256()
    values = {}
    count = zeros = found = previous_gene = 0
    total = Decimal(0)
    minimum = maximum = None
    next_sample = None
    started = last_progress = time.monotonic()
    with gzip.open(path, "rb") as source:
        header = checked_line(source)
        if header.decode("utf-8-sig").rstrip("\r\n") != OCCURRENCE_HEADER.decode():
            raise ValueError(f"定量表头不符：{path}")
        checksum.update(header)
        while True:
            if count % 10_000 == 0:
                check_budget(deadline)
            line = checked_line(source)
            if not line:
                break
            checksum.update(line)
            fields = line.decode("utf-8").rstrip("\r\n").split("\t")
            if len(fields) != 3:
                raise ValueError("定量记录不是三列")
            gene = positive_id(fields[0])
            name = fields[1]
            if not name or name != name.strip() or any(ord(c) < 32 for c in name):
                raise ValueError("样本编号无效")
            value = nonnegative(fields[2])
            if name != sample:
                next_sample = name
                break
            if count >= row_limit:
                raise ValueError("达到行数上限，首个样本尚未完整；不发布统计")
            if gene <= previous_gene:
                raise ValueError("样本内 geneID 重复或不是严格递增")
            previous_gene = gene
            count += 1
            total += value
            zeros += value == 0
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
            if keep:
                values[gene] = value
            if wanted is not None and gene in wanted:
                if value != wanted[gene]:
                    raise ValueError(f"筛选产物与原值不一致：{sample} / geneID {gene}")
                found += 1
            if count % 10_000 == 0 and time.monotonic() - last_progress >= 30:
                last_progress = time.monotonic()
                print(f"{sample} 已核对 {count:,} 行", file=sys.stderr, flush=True)
    if count != expected_count:
        raise ValueError(f"{sample} 完整样本行数 {count} 与样本清单 {expected_count} 不符")
    if wanted is not None and found != len(wanted):
        raise ValueError("筛选产物中的部分 geneID 在原始首个样本中不存在")
    if fingerprint(path) != state:
        raise ValueError(f"检查期间文件发生变化：{path}")
    check_budget(deadline)
    return {
        "path": str(path),
        "source_size_bytes": state[0],
        "samplename": sample,
        "records": count,
        "value_sum": str(total),
        "zero_values": zeros,
        "minimum_value": str(minimum) if minimum is not None else None,
        "maximum_value": str(maximum) if maximum is not None else None,
        "complete_sample_block": True,
        "next_sample_observed": next_sample,
        "stopped_at": "sample_boundary" if next_sample is not None else "file_eof",
        "decompressed_prefix_sha256_including_boundary_row": checksum.hexdigest(),
        "whole_file_digest_checked": False,
        "selected_gene_values_matched": found if wanted is not None else None,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }, values


def pfam_pairs(path, genes, expected, expected_count, deadline, max_hits):
    before = fingerprint(path)
    checksum = hashlib.sha256()
    pairs = {}
    count = 0
    with path.open("rb") as source:
        header = checked_line(source)
        checksum.update(header)
        if header.rstrip(b"\r\n") != PFAM_HEADER:
            raise ValueError("Pfam 表头不是已核验的十三列")
        while line := checked_line(source):
            if count % 10_000 == 0:
                check_budget(deadline)
            if count >= max_hits:
                raise ValueError("Pfam 命中超过保护上限")
            checksum.update(line)
            fields = line.decode("utf-8").rstrip("\r\n").split("\t")
            if len(fields) != 13:
                raise ValueError("Pfam 记录不是十三列")
            gene = positive_id(fields[1])
            count += 1
            if gene not in genes:
                continue
            if fields[2] not in {"0", "1", "2", "3", "4", "5"}:
                raise ValueError("观察到尚未支持的翻译框编号")
            family = fields[6]
            if not re.fullmatch(r"PF\d{5}", family):
                raise ValueError("Pfam accession 格式无效")
            evalue = nonnegative(fields[11])
            key = gene, family
            bit = 1 << int(fields[2])
            if key in pairs:
                old, frames, hits = pairs[key]
                pairs[key] = min(old, evalue), frames | bit, hits + 1
            else:
                pairs[key] = evalue, bit, 1
    if (
        before[0] != expected["size_bytes"]
        or checksum.hexdigest() != expected["sha256"]
        or count != expected_count
        or fingerprint(path) != before
    ):
        raise ValueError("Pfam 产物的大小、摘要、计数或稳定性与准备报告不符")
    check_budget(deadline)
    return pairs, {
        "path": str(path),
        "size_bytes": before[0],
        "sha256": checksum.hexdigest(),
        "full_prepared_hit_records_checked": count,
    }


def coverage(values, pairs):
    total = sum(values.values(), Decimal(0))
    stats = []
    for label, threshold in POLICIES.items():
        memberships = {}
        for (gene, _), (evalue, _, _) in pairs.items():
            if gene in values and (threshold is None or evalue <= threshold):
                memberships[gene] = memberships.get(gene, 0) + 1
        annotated = sum((values[g] for g in memberships), Decimal(0))
        weighted = sum((values[g] * n for g, n in memberships.items()), Decimal(0))
        stats.append(
            {
                "candidate_condition": label,
                "genes_with_pfam": len(memberships),
                "genes_without_pfam_under_condition": len(values) - len(memberships),
                "annotated_value_sum": str(annotated),
                "unannotated_value_sum": str(total - annotated),
                "annotated_fraction_of_selected_signal": str(annotated / total) if total else None,
                "genes_with_multiple_distinct_pfams": sum(n > 1 for n in memberships.values()),
                "distinct_pfam_membership_weighted_value_sum": str(weighted),
                "membership_weighted_fraction_of_selected_signal": str(weighted / total)
                if total
                else None,
            }
        )
    current = [v for (g, _), v in pairs.items() if g in values]
    return {
        "selected_observed_genes": len(values),
        "selected_value_sum": str(total),
        "unique_gene_pfam_pairs": len(current),
        "extra_hits_for_same_gene_pfam": sum(hits - 1 for _, _, hits in current),
        "gene_pfam_pairs_observed_in_multiple_frames": sum(
            mask.bit_count() > 1 for _, mask, _ in current
        ),
        "candidate_signal_coverage": stats,
    }


def inspect(
    raw_dir,
    occurrences_dir,
    prepared_dir,
    output_dir,
    *,
    max_raw_rows=20_000_000,
    max_selected_genes=500_000,
    max_hits=10_000_000,
    max_seconds=1800,
):
    if (
        not 1 <= max_raw_rows <= 200_000_000
        or not 1 <= max_selected_genes <= 1_000_000
        or not 1 <= max_hits <= 10_000_000
        or not 0 < max_seconds <= 7200
    ):
        raise ValueError("行数、基因数、命中数或时间保护参数超出允许范围")
    raw_dir, occurrences_dir, prepared_dir = (
        p.resolve(strict=True) for p in (raw_dir, occurrences_dir, prepared_dir)
    )
    output_dir = output_dir.resolve()
    if any(
        output_dir.is_relative_to(p) or p.is_relative_to(output_dir)
        for p in (raw_dir, occurrences_dir, prepared_dir)
    ):
        raise ValueError("输出必须与原始目录和已有准备目录分开")
    if output_dir.exists():
        raise FileExistsError("输出目录已存在；不覆盖旧检查")
    deadline = time.monotonic() + max_seconds
    paths = [
        occurrences_dir / "report.json",
        occurrences_dir / "samples.tsv",
        prepared_dir / "report.json",
        prepared_dir / "pfam_hits.tsv",
        Path(__file__).resolve(),
    ]
    states = {p: fingerprint(p) for p in paths}
    report_bytes, report_info = small_file(paths[0])
    sample_bytes, sample_info = small_file(paths[1])
    prepared_bytes, prepared_info = small_file(paths[2])
    manifest, preparation = json.loads(report_bytes), json.loads(prepared_bytes)
    if (
        manifest.get("status") != "completed"
        or manifest.get("script_version") != "1"
        or manifest.get("scope") != "full_occurrence_files"
        or preparation.get("status") != "completed"
        or preparation.get("script_version") != "2"
        or manifest["selection"] != preparation["selection"]
        or prepared_info["sha256"] != manifest["preparation_report"]["sha256"]
    ):
        raise ValueError("需要绑定同一分类准备结果的成功提取报告 v1 和准备报告 v2")
    expected_samples = manifest["outputs"]["samples"]
    if any(sample_info[k] != expected_samples[k] for k in ("sha256", "size_bytes")):
        raise ValueError("样本清单与提取报告不一致")
    reader = csv.DictReader(io.StringIO(sample_bytes.decode("utf-8")), delimiter="\t")
    if reader.fieldnames != [
        "assay",
        "samplename",
        "total_records",
        "selected_records",
        "zero_values",
    ]:
        raise ValueError("样本清单表头不符")
    samples = {}
    for row in reader:
        key = row["assay"], row["samplename"]
        if None in row or any(v is None for v in row.values()) or key in samples:
            raise ValueError("样本清单字段或键唯一性无效")
        if key[0] not in {"MetaG", "MetaT"}:
            raise ValueError("样本清单 assay 无效")
        counts = {k: int(row[k]) for k in ("total_records", "selected_records", "zero_values")}
        if (
            not 0 <= counts["selected_records"] <= counts["total_records"]
            or not 0 <= counts["zero_values"] <= counts["total_records"]
            or counts["total_records"] < 1
        ):
            raise ValueError("样本清单计数无效")
        samples[key] = counts
    file_info = {entry["assay"]: entry for entry in manifest["files"]}
    if len(manifest["files"]) != 2 or set(file_info) != {"MetaG", "MetaT"}:
        raise ValueError("提取报告需要唯一的 MetaG 和 MetaT 文件")
    for assay, info in file_info.items():
        group = [r for (a, _), r in samples.items() if a == assay]
        if (
            len(group) != info["sample_count"]
            or sum(r["total_records"] for r in group) != info["total_records"]
            or sum(r["selected_records"] for r in group) != info["selected_records"]
        ):
            raise ValueError("样本清单统计与全量提取报告不一致")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)
    results = []
    selected_values = {}
    genes = set()
    with localcontext() as context:
        context.prec = 50
        for assay in ("MetaG", "MetaT"):
            info = file_info[assay]
            sample = info["sample_examples"][0]
            counts = samples[assay, sample]
            raw = raw_dir / Path(info["source"]["path"]).name
            selected = occurrences_dir / f"{assay}.selected.occurrences.tsv.gz"
            for path, expected in ((raw, info["source"]), (selected, manifest["outputs"][assay])):
                states[path] = fingerprint(path)
                if states[path][0] != expected["size_bytes"]:
                    raise ValueError(f"文件大小与提取报告不符：{path}")
            print(f"开始检查 {assay} 的完整首个样本：{sample}", file=sys.stderr, flush=True)
            selected_report, values = first_block(
                selected,
                sample,
                counts["selected_records"],
                max_selected_genes,
                deadline,
                keep=True,
            )
            genes.update(values)
            if len(genes) > max_selected_genes:
                raise ValueError("两个样本的目标基因总数超过内存保护上限")
            raw_report, _ = first_block(
                raw, sample, counts["total_records"], max_raw_rows, deadline, wanted=values
            )
            if raw_report["zero_values"] != counts["zero_values"]:
                raise ValueError("样本零值计数与提取报告不符")
            selected_values[assay] = values
            results.append(
                {
                    "assay": assay,
                    "original": raw_report,
                    "selected": selected_report,
                    "selected_values_match_original": True,
                }
            )
        print("正在核验已准备的 Pfam 摘要与样本信号覆盖……", file=sys.stderr, flush=True)
        pairs, pfam_info = pfam_pairs(
            paths[3],
            genes,
            preparation["outputs"]["pfam_hits"],
            preparation["pfam"]["selected_hit_records"],
            deadline,
            max_hits,
        )
        for result in results:
            result["pfam_diagnostics"] = coverage(selected_values[result["assay"]], pairs)
    if any(fingerprint(p) != before for p, before in states.items()):
        raise ValueError("检查期间输入或脚本发生变化；报告无效")
    check_budget(deadline)
    report = {
        "status": "completed",
        "script_version": "1",
        "scope": "first_complete_sample_per_assay_and_prepared_pfam",
        "script": small_file(paths[4])[1],
        "selection": manifest["selection"],
        "extraction_report": report_info,
        "preparation_report": prepared_info,
        "samples": sample_info,
        "pfam": pfam_info,
        "files": results,
        "parameters": {
            "max_raw_rows_per_assay": max_raw_rows,
            "max_selected_genes": max_selected_genes,
            "max_hits": max_hits,
            "max_seconds": max_seconds,
            "decimal_sum_precision": 50,
        },
        "validation": {
            "inputs_and_script_unchanged": True,
            "prepared_pfam_full_digest_and_counts_match": True,
            "sample_counts_match_full_extraction_report": True,
        },
        "limitations": [
            "每个 assay 仅检查文件首个样本，非随机抽样；不代表全部样本的归一化。",
            "原始及筛选定量文件只核对大小、稳定性和读取前缀，不重算整文件摘要或完整 Gzip。",
            "数值总和不能单独确定 RPKM、额外缩放或未出现记录是否为零。",
            "原值一致只证明本次读取的筛选记录，没有重新验证所有类群基因是否提取完整。",
            "Pfam 覆盖按观察到的 geneID 计算；未出现基因不补零，不评价生物学功能正确性。",
            "敏感性条件不是家族官方 gathering 阈值；跨框最小 E-value 不解决翻译框冲突。",
            "多 Pfam 的基因可多次贡献 membership 信号，其相加比例可能超过 100%。",
            "不确定 DNA/RNA 配对、不计算 RNA/DNA 比值、不关联环境。",
        ],
    }
    partial = output_dir / "report.json.partial"
    with partial.open("x", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")
    partial.rename(output_dir / "report.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw-dir", "occurrences-dir", "prepared-dir", "output-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--max-raw-rows", type=int, default=20_000_000)
    parser.add_argument("--max-selected-genes", type=int, default=500_000)
    parser.add_argument("--max-hits", type=int, default=10_000_000)
    parser.add_argument("--max-seconds", type=float, default=1800)
    args = parser.parse_args()
    try:
        report = inspect(**vars(args))
    except KeyboardInterrupt:
        print("检查已停止；未完成的报告不可用于分析。", file=sys.stderr)
        return 130
    except (
        OSError,
        EOFError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        DecimalException,
        zlib.error,
    ) as error:
        print(f"检查失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
