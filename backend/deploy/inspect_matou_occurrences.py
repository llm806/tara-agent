"""只读试读 MATOU 定量表的有界前缀；不推断单位、配对或全表质量。"""

import argparse
import gzip
import hashlib
import json
import sys
import time
import zlib
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_ROWS = 1_000_000
MAX_LINE_BYTES = 16_384


def fingerprint(path: Path) -> tuple:
    state = path.stat()
    return state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino


def inspect_prefix(path: Path, max_rows: int = 100_000) -> dict:
    if not 1 <= max_rows <= MAX_ROWS:
        raise ValueError(f"试读行数必须在 1 到 {MAX_ROWS} 之间")
    path = path.resolve(strict=True)
    before = fingerprint(path)
    started = time.monotonic()
    samples: Counter[str] = Counter()
    pairs: set[tuple[int, str]] = set()
    duplicates = zeros = decreases = count = 0
    previous_sample = previous_gene = None
    minimum = maximum = None
    examples = []
    checksum = hashlib.sha256()
    reached_eof = False

    with gzip.open(path, "rb") as source:
        header = source.readline(MAX_LINE_BYTES + 1)
        if header.decode("utf-8-sig").rstrip("\r\n") != "geneid\tsamplename\tvalue":
            raise ValueError("定量表头必须为 geneid、samplename、value 三列")
        checksum.update(header)
        for _ in range(max_rows):
            raw = source.readline(MAX_LINE_BYTES + 1)
            if not raw:
                reached_eof = True
                break
            line_number = count + 2
            if len(raw) > MAX_LINE_BYTES:
                raise ValueError(f"第 {line_number} 行超过试读长度限制")
            fields = raw.decode("utf-8").rstrip("\r\n").split("\t")
            if len(fields) != 3:
                raise ValueError(f"第 {line_number} 行不是三列")
            gene, sample, text_value = fields
            if not gene.isascii() or not gene.isdigit() or int(gene) <= 0:
                raise ValueError(f"第 {line_number} 行的 geneid 不是正整数")
            if not sample or sample != sample.strip():
                raise ValueError(f"第 {line_number} 行的样本编号为空或含首尾空白")
            try:
                value = Decimal(text_value)
            except InvalidOperation as error:
                raise ValueError(f"第 {line_number} 行的 value 不是数值") from error
            if not value.is_finite() or value < 0:
                raise ValueError(f"第 {line_number} 行的 value 非有限或为负")
            gene_id = int(gene)
            pair = gene_id, sample
            duplicates += pair in pairs
            pairs.add(pair)
            samples[sample] += 1
            zeros += value == 0
            if sample == previous_sample and gene_id < previous_gene:
                decreases += 1
            previous_sample, previous_gene = sample, gene_id
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
            if len(examples) < 5:
                examples.append(dict(zip(("geneid", "samplename", "value"), fields, strict=True)))
            checksum.update(raw)
            count += 1

    if fingerprint(path) != before:
        raise ValueError("试读期间源文件发生变化，报告无效")
    if count == 0:
        raise ValueError("定量表没有数据行")
    elapsed = time.monotonic() - started
    return {
        "path": str(path),
        "source_size_bytes": before[0],
        "rows_read": count,
        "row_limit": max_rows,
        "reached_eof": reached_eof,
        "prefix_sha256": checksum.hexdigest(),
        "observed_sample_count": len(samples),
        "observed_samples_first_10": dict(list(samples.items())[:10]),
        "duplicate_gene_sample_rows_in_prefix": duplicates,
        "gene_id_decreases_within_adjacent_same_sample": decreases,
        "zero_values_in_prefix": zeros,
        "minimum_value_in_prefix": str(minimum),
        "maximum_value_in_prefix": str(maximum),
        "first_rows": examples,
        "elapsed_seconds": round(elapsed, 3),
        "rows_per_second": round(count / elapsed, 1) if elapsed > 0 else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--max-rows", type=int, default=100_000)
    args = parser.parse_args()
    try:
        reports = []
        for path in args.input:
            print(f"开始试读：{path}；最多 {args.max_rows} 行", file=sys.stderr, flush=True)
            reports.append(inspect_prefix(path, args.max_rows))
    except (OSError, EOFError, ValueError, UnicodeError, zlib.error) as error:
        print(f"试读失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("试读已停止，原始文件没有改动。", file=sys.stderr)
        return 130
    report = {
        "status": "completed",
        "script_version": "1",
        "scope": "bounded_file_prefix",
        "files": reports,
        "limitations": [
            "前缀不是随机抽样，不代表完整样本清单或全表分布。",
            "到达行数上限即停止，不保证读完样本或验证完整 Gzip。",
            "prefix_sha256 仅标识实际读取的解压前缀，不是源文件摘要。",
            "速度仅描述本次前缀试读，不用于承诺全表耗时。",
            "不推断单位、缺失记录是否为零、全表唯一性或 DNA/RNA 配对。",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
