"""复用已发布功能缓存，在新目录准备目标核酸序列；不改旧数据或服务代码。"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

import polars as pl

from tara_agent.analysis.function_study_prepare import extract_sequences
from tara_agent.data.matou_artifacts import (
    MatouArtifact,
    check_file,
    describe,
    fingerprint,
    safe_child,
)
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.matou_study import StudyManifest, load_study

# Genoscope MATOU v1.5 发布页的压缩核酸文件 MD5；不能用其他版本或蛋白文件替代。
OFFICIAL_FASTA_MD5 = "045fd2cda0e99e3b6ee52b78ea572da2"
OFFICIAL_SOURCE = "https://www.genoscope.cns.fr/tara/#MATOU-1.5"


def prepare_sequences(
    data_dir: Path,
    fasta: Path,
    output_dir: Path,
    *,
    pfams,
    expected_fasta_md5: str | None = OFFICIAL_FASTA_MD5,
    max_seconds=43200,
    min_free_gib=2,
    max_bases=100000000,
):
    reader = MatouDataReader(data_dir)
    study = load_study(reader)
    if study is None:
        raise ValueError("先准备功能缓存；此脚本只升级已有研究目录")
    fasta = fasta.resolve(strict=True)
    output_dir = output_dir.resolve(strict=False)
    if (
        output_dir.exists()
        or output_dir.is_relative_to(reader.directory)
        or reader.directory.is_relative_to(output_dir)
        or output_dir.is_relative_to(fasta.parent)
    ):
        raise ValueError("输出必须是旧功能目录及原始 FASTA 目录之外的新目录")
    if (
        not pfams
        or len(set(pfams)) != len(pfams)
        or any(re.fullmatch(r"PF[0-9]{5}", p) is None for p in pfams)
    ):
        raise ValueError("须指定互不重复的有效目标 Pfam")
    if (
        (
            expected_fasta_md5 is not None
            and re.fullmatch(r"[0-9a-f]{32}", expected_fasta_md5) is None
        )
        or not math.isfinite(max_seconds)
        or not 0 < max_seconds <= 172800
        or not math.isfinite(min_free_gib)
        or min_free_gib < 0
        or not isinstance(max_bases, int)
        or max_bases <= 0
    ):
        raise ValueError("校验值或资源保护参数无效")
    start = time.monotonic()
    fasta_before = fingerprint(fasta)

    def guard(required_bytes=0):
        if time.monotonic() - start > max_seconds:
            raise ValueError("序列准备超过时间保护上限")
        location = output_dir if output_dir.exists() else output_dir.parent
        if shutil.disk_usage(location).free - required_bytes < min_free_gib * 1024**3:
            raise ValueError("磁盘低于保留额度")

    mapping = reader.load_gene_pfam()
    if study.max_i_evalue is not None:
        mapping = mapping.filter(pl.col("min_iEvalue") <= study.max_i_evalue)
    wanted = set(mapping.filter(pl.col("pfamAcc").is_in(pfams))["geneID"])
    if not wanted:
        raise ValueError("目标 Pfam 没有候选基因")
    if expected_fasta_md5 is not None:
        print("校验 FASTA 发布文件 MD5…", file=sys.stderr, flush=True)
        md5 = hashlib.md5(usedforsecurity=False)
        with fasta.open("rb") as stream:
            while chunk := stream.read(8 * 1024**2):
                guard()
                md5.update(chunk)
        if fingerprint(fasta) != fasta_before or md5.hexdigest() != expected_fasta_md5:
            raise ValueError("FASTA 发布文件 MD5 不符或校验期间发生变化")
    else:
        print("按用户选择跳过官网 MD5 比对，直接读取并提取序列…", file=sys.stderr, flush=True)

    print(f"完整核验 FASTA 并提取 {len(wanted)} 个目标基因…", file=sys.stderr, flush=True)
    frame, source = extract_sequences(
        fasta, wanted, max_bases=max_bases, max_seconds=max_seconds - (time.monotonic() - start)
    )
    guard()
    output_dir.mkdir()
    stage = output_dir / f"sequence-{uuid4().hex}"
    stage.mkdir()
    path = stage / "sequences.parquet"
    guard(frame.estimated_size() + 1024**2)
    frame.write_parquet(path, compression="zstd")
    record = describe(path).model_dump()
    record["path"] = path.relative_to(output_dir).as_posix()
    sequence_artifact = MatouArtifact(**record, rows=frame.height)

    artifacts = [reader.manifest.gene_pfam, *(s.artifact for s in reader.manifest.samples)]
    artifacts.extend([study.summaries, study.profiles])
    unique = {}
    for artifact in artifacts:
        if artifact.path in unique and unique[artifact.path] != artifact:
            raise ValueError("同一路径存在冲突产物描述")
        unique[artifact.path] = artifact
    print(f"复制并核验 {len(unique)} 个既有产物，保留功能缓存…", file=sys.stderr, flush=True)
    for artifact in unique.values():
        guard(artifact.size_bytes)
        origin = safe_child(reader.directory, artifact.path)
        target = output_dir / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        # 使用独立文件，不创建旧目录的硬链接；避免影响在线读取的文件指纹。
        shutil.copy2(origin, target)
        check_file(target, artifact)
    updated = StudyManifest.model_validate(
        {
            **study.model_dump(),
            "sequences": sequence_artifact.model_dump(),
            "fasta_source": source.model_dump(),
            "sequence_selection": {
                "pfam_accessions": list(pfams),
                "header_mapping": "exact_MATOU-v1.5.geneID",
                "sequence_case": "normalized_to_uppercase",
                "soft_mask_case_preserved": False,
            },
        }
    )
    guard()
    if fingerprint(fasta) != fasta_before:
        raise ValueError("准备期间 FASTA 发生变化")
    reader.ensure_unchanged()
    primary = stage / "manifest.json"
    primary.write_bytes((reader.directory / "manifest.json").read_bytes())
    check_file(primary, reader.manifest_record)
    study_path = stage / "study_manifest.json"
    study_path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    report = {
        "status": "completed",
        "data_dir": str(output_dir),
        "sequence_count": frame.height,
        "pfam_accessions": list(pfams),
        "sequence_case": "normalized_to_uppercase",
        "soft_mask_case_preserved": False,
        "fasta_md5": expected_fasta_md5,
        "md5_reference": (
            None
            if expected_fasta_md5 is None
            else OFFICIAL_SOURCE
            if expected_fasta_md5 == OFFICIAL_FASTA_MD5
            else "explicit"
        ),
        "publisher_md5_verification": "not_performed" if expected_fasta_md5 is None else "matched",
        "publisher_md5_skip_reason": "operator_request" if expected_fasta_md5 is None else None,
        "fasta_sha256": source.sha256,
        "source_manifest_sha256": reader.manifest_record.sha256,
        "old_study_manifest_sha256": reader.study_sha256,
        "new_study_manifest_sha256": describe(study_path).sha256,
        "functional_cache_reused": True,
    }
    (stage / "sequence_preparation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 主清单最后原子发布；中断目录没有主清单，不会被服务读成成功数据。
    os.link(study_path, output_dir / "study_manifest.json")
    os.link(primary, output_dir / "manifest.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("data-dir", "fasta", "output-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--pfam", action="append", required=True)
    md5_options = parser.add_mutually_exclusive_group()
    md5_options.add_argument("--expected-fasta-md5", default=OFFICIAL_FASTA_MD5)
    md5_options.add_argument(
        "--skip-publisher-md5", action="store_true", help="跳过官网 MD5 比对并在报告中记录"
    )
    parser.add_argument("--max-seconds", type=float, default=43200)
    parser.add_argument("--min-free-gib", type=float, default=2)
    parser.add_argument("--max-bases", type=int, default=100000000)
    args = parser.parse_args()
    try:
        result = prepare_sequences(
            args.data_dir,
            args.fasta,
            args.output_dir,
            pfams=args.pfam,
            expected_fasta_md5=None if args.skip_publisher_md5 else args.expected_fasta_md5,
            max_seconds=args.max_seconds,
            min_free_gib=args.min_free_gib,
            max_bases=args.max_bases,
        )
    except (OSError, ValueError, pl.exceptions.PolarsError) as error:
        print(f"序列准备失败：{error}；未发布主清单的目录不可使用。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
