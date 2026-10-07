"""复用已核验的类群基因清单，完整流式筛选 MetaG/MetaT；不做功能计算或配对。"""

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sys
import time
import zlib
from decimal import Decimal, InvalidOperation
from pathlib import Path

HEADER = b"geneid\tsamplename\tvalue"
MAX_LINE_BYTES = 16_384
SCRIPT_VERSION = "1"


def fingerprint(path: Path) -> tuple:
    state = path.stat()
    return state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino


def describe(path: Path) -> dict:
    before = fingerprint(path)
    with path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    if fingerprint(path) != before:
        raise ValueError(f"文件发生变化：{path}")
    return {"path": str(path), "size_bytes": before[0], "sha256": checksum}


def check_space(directory: Path, reserve_bytes: int) -> None:
    if shutil.disk_usage(directory).free < reserve_bytes:
        raise ValueError("输出磁盘剩余空间低于保留额度；停止，不发布结果")


def read_line(source) -> bytes:
    raw = source.readline(MAX_LINE_BYTES + 1)
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError("数据行超过长度保护上限")
    return raw


def gene_id(text: str) -> int:
    if not text.isascii() or not text.isdigit():
        raise ValueError("geneID 不是 ASCII 正整数")
    result = int(text)
    if result <= 0:
        raise ValueError("geneID 不是正整数")
    return result


def load_genes(prepared: Path, max_genes: int) -> tuple[set[int], dict, dict]:
    report_path = prepared / "report.json"
    if report_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("准备报告超过大小保护上限")
    before = fingerprint(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("status") != "completed"
        or report.get("script_version") != "2"
        or report.get("validation", {}).get("selected_taxonomy_geneIDs_unique") is not True
        or not report.get("selection", {}).get("taxon")
    ):
        raise ValueError("需要 taxonomy/Pfam 准备脚本 v2 的完整成功报告")
    path = (prepared / "gene_ids.tsv").resolve(strict=True)
    state = fingerprint(path)
    checksum = hashlib.sha256()
    genes: set[int] = set()
    with path.open("rb") as source:
        header = read_line(source)
        checksum.update(header)
        if header.rstrip(b"\r\n") != b"geneID":
            raise ValueError("基因清单表头必须为 geneID")
        while raw := read_line(source):
            checksum.update(raw)
            identifier = gene_id(raw.decode("ascii").rstrip("\r\n"))
            if identifier in genes:
                raise ValueError("基因清单存在重复 geneID")
            if len(genes) >= max_genes:
                raise ValueError("基因清单超过数量保护上限")
            genes.add(identifier)
    actual = {"path": str(path), "size_bytes": state[0], "sha256": checksum.hexdigest()}
    expected = report["outputs"]["gene_ids"]
    if (
        not genes
        or len(genes) != report["taxonomy"]["selected_unique_geneIDs"]
        or actual["size_bytes"] != expected["size_bytes"]
        or actual["sha256"] != expected["sha256"]
    ):
        raise ValueError("基因清单数量、大小或 SHA-256 与准备报告不符")
    if fingerprint(path) != state or fingerprint(report_path) != before:
        raise ValueError("准备文件发生变化")
    return genes, report, actual


class HashingReader(io.RawIOBase):
    """在同一次顺序读取中计算压缩源摘要，避免额外扫描几十 GB 的原始文件。"""

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.checksum = hashlib.sha256()
        self.bytes_read = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.source.readinto(buffer)
        if count:
            self.checksum.update(memoryview(buffer)[:count])
            self.bytes_read += count
        return count


def scan(
    path: Path,
    assay: str,
    genes: set[int],
    output: Path,
    max_samples: int,
    reserve_bytes: int,
    progress_seconds: float,
) -> tuple[dict, list[dict]]:
    before = fingerprint(path)
    started = last_progress = time.monotonic()
    total = selected = zeros = 0
    minimum = maximum = None
    samples: dict[str, dict] = {}
    current_sample = None
    previous_gene = 0
    print(f"开始完整扫描 {assay}：{path}", file=sys.stderr, flush=True)
    with path.open("rb") as compressed:
        hashing = HashingReader(compressed)
        with (
            io.BufferedReader(hashing, buffer_size=1024 * 1024) as buffered,
            gzip.GzipFile(fileobj=buffered, mode="rb") as source,
            output.open("xb") as target_file,
            gzip.GzipFile(
                fileobj=target_file, mode="wb", filename="", compresslevel=1, mtime=0
            ) as target,
        ):
            raw_header = read_line(source)
            if raw_header.decode("utf-8-sig").rstrip("\r\n") != HEADER.decode():
                raise ValueError(f"{assay} 表头不是 geneid、samplename、value 三列")
            target.write(raw_header)
            while raw := read_line(source):
                fields = raw.decode("utf-8").rstrip("\r\n").split("\t")
                if len(fields) != 3:
                    raise ValueError(f"{assay} 第 {total + 2} 行不是三列")
                identifier = gene_id(fields[0])
                sample, value_text = fields[1:]
                if not sample or sample != sample.strip() or any(ord(c) < 32 for c in sample):
                    raise ValueError(f"{assay} 第 {total + 2} 行样本编号无效")
                try:
                    value = Decimal(value_text)
                except InvalidOperation as error:
                    raise ValueError(f"{assay} 第 {total + 2} 行 value 不是数值") from error
                if not value.is_finite() or value < 0:
                    raise ValueError(f"{assay} 第 {total + 2} 行 value 非有限或为负")

                # 全表验证此顺序，才能用常量级状态证明键唯一；不假设前缀代表全表。
                # 无序时停止，不自动排序、去重或累加科学定量值。
                if sample != current_sample:
                    if sample in samples:
                        raise ValueError(f"{assay} 样本 {sample} 分散在多个数据块；停止核查")
                    if len(samples) >= max_samples:
                        raise ValueError("样本数量超过保护上限")
                    stats = {
                        "assay": assay,
                        "samplename": sample,
                        "total_records": 0,
                        "selected_records": 0,
                        "zero_values": 0,
                    }
                    samples[sample] = stats
                    current_sample, previous_gene = sample, 0
                if identifier <= previous_gene:
                    raise ValueError(
                        f"{assay} 第 {total + 2} 行存在重复键或 geneID 非递增；停止核查"
                    )
                previous_gene = identifier
                total += 1
                stats["total_records"] += 1
                stats["zero_values"] += value == 0
                zeros += value == 0
                minimum = value if minimum is None or value < minimum else minimum
                maximum = value if maximum is None or value > maximum else maximum
                if identifier in genes:
                    # 原始字节与数值字符串直接保留；包括未获 Pfam 注释的目标基因。
                    target.write(raw)
                    selected += 1
                    stats["selected_records"] += 1
                if total % 100_000 == 0:
                    now = time.monotonic()
                    if now - last_progress >= progress_seconds:
                        check_space(output.parent, reserve_bytes)
                        print(
                            f"{assay} 已扫描 {total:,} 条，保留 {selected:,} 条，"
                            f"样本 {len(samples)} 个，耗时 {now - started:.0f} 秒",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_progress = now
        if total == 0:
            raise ValueError(f"{assay} 没有数据行")
        if hashing.bytes_read != before[0] or fingerprint(path) != before:
            raise ValueError(f"源文件发生变化或未完整读取：{path}")
        source_info = {
            "path": str(path),
            "size_bytes": before[0],
            "sha256": hashing.checksum.hexdigest(),
        }
    check_space(output.parent, reserve_bytes)
    print(f"{assay} 扫描完成：{total:,} 条，保留 {selected:,} 条", file=sys.stderr, flush=True)
    return {
        "assay": assay,
        "source": source_info,
        "total_records": total,
        "selected_records": selected,
        "sample_count": len(samples),
        "samples_with_selected_records": sum(s["selected_records"] > 0 for s in samples.values()),
        "sample_examples": list(samples)[:10],
        "zero_values": zeros,
        "minimum_value": str(minimum),
        "maximum_value": str(maximum),
        "validation": {
            "full_gzip_read": True,
            "schema_and_finite_nonnegative_values": True,
            "contiguous_sample_blocks_and_strictly_increasing_geneIDs": True,
            "gene_sample_keys_unique": True,
        },
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }, list(samples.values())


def extract(
    prepared: Path,
    meta_g: Path,
    meta_t: Path,
    output: Path,
    *,
    max_genes: int = 10_000_000,
    max_samples: int = 10_000,
    min_free_gib: float = 10,
    progress_seconds: float = 30,
) -> dict:
    if max_genes <= 0 or max_samples <= 0 or not 0 < progress_seconds < float("inf"):
        raise ValueError("基因、样本上限和进度间隔必须为正数")
    if not 0 <= min_free_gib < float("inf"):
        raise ValueError("磁盘保留额度必须为有限非负数")
    prepared = prepared.resolve(strict=True)
    meta_g, meta_t = meta_g.resolve(strict=True), meta_t.resolve(strict=True)
    output = output.resolve()
    if meta_g == meta_t:
        raise ValueError("MetaG、MetaT 必须是不同文件")
    if (
        any(output.is_relative_to(p.parent) for p in (meta_g, meta_t))
        or output.is_relative_to(prepared)
        or prepared.is_relative_to(output)
    ):
        raise ValueError("输出须与原始数据目录及已有准备目录分开")
    if output.exists():
        raise FileExistsError(f"输出目录已经存在，不覆盖旧运行：{output}")
    paths = (meta_g, meta_t, prepared / "report.json", prepared / "gene_ids.tsv", Path(__file__))
    states = {p: fingerprint(p) for p in paths}
    started = time.monotonic()
    print("正在核验已有基因清单及准备报告……", file=sys.stderr, flush=True)
    genes, preparation, genes_info = load_genes(prepared, max_genes)
    output.parent.mkdir(parents=True, exist_ok=True)
    reserve = int(min_free_gib * 1024**3)
    check_space(output.parent, reserve)
    output.mkdir(exist_ok=False)
    files, sample_rows, partials = [], [], {}
    for assay, source in (("MetaG", meta_g), ("MetaT", meta_t)):
        partial = output / f"{assay}.selected.occurrences.tsv.gz.partial"
        info, rows = scan(source, assay, genes, partial, max_samples, reserve, progress_seconds)
        files.append(info)
        sample_rows.extend(rows)
        partials[assay] = partial
    samples_path = output / "samples.tsv.partial"
    with samples_path.open("x", encoding="utf-8", newline="") as target:
        columns = ["assay", "samplename", "total_records", "selected_records", "zero_values"]
        target.write("\t".join(columns) + "\n")
        for row in sample_rows:
            target.write("\t".join(str(row[k]) for k in columns) + "\n")
    partials["samples"] = samples_path
    outputs = {}
    for key, partial in partials.items():
        outputs[key] = describe(partial)
        outputs[key]["path"] = str(partial.with_suffix(""))
    script_info = describe(Path(__file__).resolve(strict=True))
    preparation_info = describe(prepared / "report.json")
    if any(fingerprint(p) != state for p, state in states.items()):
        raise ValueError("运行期间输入或脚本发生变化；停止，不发布结果")
    check_space(output, reserve)
    report = {
        "status": "completed",
        "script_version": SCRIPT_VERSION,
        "script": script_info,
        "scope": "full_occurrence_files",
        "selection": preparation["selection"],
        "selected_unique_geneIDs": len(genes),
        "preparation_report": preparation_info,
        "gene_ids": genes_info,
        "files": files,
        "outputs": outputs,
        "parameters": {
            "max_selected_genes": max_genes,
            "max_samples": max_samples,
            "min_free_gib": min_free_gib,
            "progress_seconds": progress_seconds,
            "output_gzip_compresslevel": 1,
        },
        "validation": {"inputs_and_script_unchanged": True, "gene_list_digest_matches": True},
        "limitations": [
            "仅按准备清单中的 geneID 筛选，分类准确性未重新评估。",
            "保留原始数值；单位、缩放、归一化和缺失记录是否为零尚未核验。",
            "样本清单分别统计 MetaG/MetaT，不代表 DNA/RNA 已配对或已关联 context。",
            "未做 Pfam 质量筛选、功能聚合、相对丰度计算或 RNA/DNA 比值分析。",
        ],
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    # 产物关闭且全部检查通过后发布；report.json 最后出现，作为整次运行完成标记。
    for partial in partials.values():
        partial.rename(partial.with_suffix(""))
    report_partial = output / "report.json.partial"
    report_partial.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_partial.rename(output / "report.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--meta-g", type=Path, required=True)
    parser.add_argument("--meta-t", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-selected-genes", type=int, default=10_000_000)
    parser.add_argument("--max-samples", type=int, default=10_000)
    parser.add_argument("--min-free-gib", type=float, default=10)
    parser.add_argument("--progress-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        report = extract(
            args.prepared_dir,
            args.meta_g,
            args.meta_t,
            args.output_dir,
            max_genes=args.max_selected_genes,
            max_samples=args.max_samples,
            min_free_gib=args.min_free_gib,
            progress_seconds=args.progress_seconds,
        )
    except (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error) as error:
        print(
            f"筛选失败：{type(error).__name__}: {error}；无完整 report.json 的目录不可使用。",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("筛选已停止，原始文件未改动；未完成目录不可用于分析。", file=sys.stderr)
        return 130
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
