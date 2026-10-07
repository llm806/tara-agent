"""编排独立功能谱预计算和目标序列准备；原始文件只读，成功清单最后发布。"""

import gzip
import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from uuid import uuid4

import polars as pl

from tara_agent.analysis.function_atlas import summarize_sample
from tara_agent.data.matou_artifacts import MatouArtifact, describe, fingerprint
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.matou_study import CODE_EVIDENCE, StudyManifest


def extract_sequences(path: Path, wanted: set[int], *, max_bases=100000000, max_seconds=43200):
    """核验官方标题与全文件 geneID 唯一性；不把相似名称当映射。"""
    start, before = time.monotonic(), fingerprint(path)
    current, header, parts, previous, bases, record_bases = None, None, [], 0, 0, 0
    rows = []
    opener = gzip.open if path.suffix == ".gz" else open

    def append():
        if current is not None and record_bases == 0:
            raise ValueError("FASTA 序列为空")
        if current in wanted:
            sequence = "".join(parts)
            if not sequence:
                raise ValueError("目标序列为空")
            rows.append({"geneID": current, "header": header, "sequence": sequence})

    with opener(path, "rt", encoding="ascii") as stream:
        for line_number, line in enumerate(stream, start=1):
            if time.monotonic() - start > max_seconds:
                raise ValueError("序列提取超过时间保护上限")
            text = line.strip()
            if text.startswith(">"):
                append()
                match = re.fullmatch(r">MATOU-v1\.5\.([1-9][0-9]*)", text)
                if match is None:
                    raise ValueError("FASTA 标题不符合已核验的 MATOU-v1.5.geneID 映射")
                current, header, parts = int(match[1]), text[1:], []
                record_bases = 0
                if current <= previous:
                    raise ValueError("FASTA geneID 无序或重复，不能证明映射唯一")
                previous = current
            elif text:
                if current is None:
                    raise ValueError(f"FASTA 序列出现在首个标题之前：第 {line_number} 行")
                # 官方文件包含小写碱基；统一字母大小写，不改碱基身份或丢弃非法字符。
                text = text.upper()
                if re.fullmatch(r"[ACGTRYSWKMBDHVN]+", text) is None:
                    invalid = "".join(sorted(set(text) - set("ACGTRYSWKMBDHVN")))[:20]
                    raise ValueError(
                        f"FASTA 核酸字符不符合契约：第 {line_number} 行、"
                        f"geneID={current}、非法字符={invalid!r}"
                    )
                record_bases += len(text)
                if current in wanted:
                    bases += len(text)
                    if bases > max_bases:
                        raise ValueError("目标核酸序列超过碱基资源上限，请缩小 gene/Pfam 范围")
                    parts.append(text)
        append()
    found = {r["geneID"] for r in rows}
    if found != wanted:
        raise ValueError(f"FASTA 缺少 {len(wanted - found)} 个目标 geneID，不能发布完整提取")
    source = describe(path)
    if fingerprint(path) != before:
        raise ValueError("序列提取期间原始文件发生变化")
    return pl.DataFrame(
        rows, schema={"geneID": pl.Int64, "header": pl.String, "sequence": pl.String}
    ), source


def prepare_study(
    directory: Path,
    *,
    max_i_evalue=None,
    fasta: Path | None = None,
    sequence_pfams=(),
    max_seconds=43200,
    min_free_gib=2,
    max_bases=100000000,
):
    reader = MatouDataReader(directory)
    if (reader.directory / "study_manifest.json").exists():
        raise ValueError("已有研究清单不可覆盖；请使用新的功能数据目录")
    if max_i_evalue is not None and (max_i_evalue < 0 or not math.isfinite(max_i_evalue)):
        raise ValueError("候选 E 条件必须有限且非负")
    if (
        not 0 < max_seconds <= 172800
        or not math.isfinite(min_free_gib)
        or min_free_gib < 0
        or not isinstance(max_bases, int)
        or max_bases <= 0
    ):
        raise ValueError("准备资源参数无效")
    stage = reader.directory / f"study-{uuid4().hex}"
    stage.mkdir()
    start = time.monotonic()

    def guard():
        if time.monotonic() - start > max_seconds:
            raise ValueError("功能谱准备超过时间保护上限")
        if shutil.disk_usage(stage).free < min_free_gib * 1024**3:
            raise ValueError("磁盘低于保留额度")

    mapping = reader.load_gene_pfam()
    if max_i_evalue is not None:
        mapping = mapping.filter(pl.col("min_iEvalue") <= max_i_evalue)
    totals, profiles, parts, profile_rows = [], [], [], 0
    profile_schema = {
        "assay": pl.String,
        "sample_name": pl.String,
        "pfam_accession": pl.String,
        "value_sum": pl.Float64,
    }

    def flush():
        if profiles:
            path = stage / f"part-{len(parts)}.parquet"
            pl.DataFrame(profiles, schema=profile_schema).write_parquet(path, compression="zstd")
            parts.append(path)
            profiles.clear()

    for sample in reader.manifest.samples:
        guard()
        result = summarize_sample(
            reader.load_sample(sample.assay, sample.sample_name), mapping, sample.sample_name
        )
        totals.append(
            {"assay": sample.assay, **{k: v for k, v in result.items() if k != "families"}}
        )
        profiles.extend(
            {
                "assay": sample.assay,
                "sample_name": sample.sample_name,
                "pfam_accession": f,
                "value_sum": value,
            }
            for f, value in result["families"].items()
        )
        profile_rows += len(result["families"])
        if profile_rows > 20000000:
            raise ValueError("功能谱准备超过两千万家族记录保护上限")
        if len(profiles) >= 50000:
            flush()
    flush()
    frames = {
        "summaries": pl.DataFrame(
            totals,
            schema={
                "assay": pl.String,
                "sample_name": pl.String,
                "taxon_value_sum": pl.Float64,
                "observed_gene_count": pl.Int64,
            },
        ),
    }
    artifacts = {}
    for name, frame in frames.items():
        guard()
        path = stage / f"{name}.parquet"
        frame.write_parquet(path, compression="zstd")
        record = describe(path).model_dump()
        record["path"] = path.relative_to(reader.directory).as_posix()
        artifacts[name] = MatouArtifact(**record, rows=frame.height)
    path = stage / "profiles.parquet"
    if parts:
        pl.scan_parquet(parts).sink_parquet(path, compression="zstd")
    else:
        pl.DataFrame(schema=profile_schema).write_parquet(path)
    record = describe(path).model_dump()
    record["path"] = path.relative_to(reader.directory).as_posix()
    artifacts["profiles"] = MatouArtifact(**record, rows=profile_rows)
    for part in parts:
        part.unlink()
    sequence_artifact, source = None, None
    if fasta is not None:
        if not sequence_pfams or any(
            re.fullmatch(r"PF[0-9]{5}", p) is None for p in sequence_pfams
        ):
            raise ValueError("序列准备须明确指定有效 Pfam，不能默认扫描并输出全部序列")
        wanted = set(mapping.filter(pl.col("pfamAcc").is_in(sequence_pfams))["geneID"])
        if not wanted:
            raise ValueError("目标 Pfam 没有候选基因")
        frame, source = extract_sequences(
            fasta, wanted, max_bases=max_bases, max_seconds=max_seconds - (time.monotonic() - start)
        )
        guard()
        path = stage / "sequences.parquet"
        frame.write_parquet(path, compression="zstd")
        record = describe(path).model_dump()
        record["path"] = path.relative_to(reader.directory).as_posix()
        sequence_artifact = MatouArtifact(**record, rows=frame.height)
    manifest = StudyManifest(
        version="matou-study-v1",
        source_manifest_sha256=reader.manifest_record.sha256,
        max_i_evalue=max_i_evalue,
        **artifacts,
        sequences=sequence_artifact,
        fasta_source=source,
        correspondence_evidence=CODE_EVIDENCE,
        sequence_selection={
            "pfam_accessions": list(sequence_pfams),
            "header_mapping": "exact_MATOU-v1.5.geneID",
            "sequence_case": "normalized_to_uppercase",
            "soft_mask_case_preserved": False,
        }
        if fasta
        else None,
    )
    guard()
    reader.ensure_unchanged()
    # 清单出现是唯一成功标记，中断目录不会进入读取路径。
    target = reader.directory / "study_manifest.json"
    temporary = stage / "study_manifest.json"
    temporary.write_text(
        json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 同文件系统硬链接原子发布完整字节，并且目标存在时失败，避免并发覆盖。
    os.link(temporary, target)
    return manifest
