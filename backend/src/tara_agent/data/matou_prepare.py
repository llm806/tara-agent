"""将已核验的类群定量子集转换为单样本 Parquet，不重扫原始 MATOU 定量表。"""

import gzip
import math
import re
import shutil
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from tara_agent.data.matou_artifacts import (
    FileRecord,
    MatouArtifact,
    MatouManifest,
    MatouSample,
    check_file,
    describe,
    fingerprint,
    read_json,
)

OCCURRENCE_SCHEMA = {"geneID": pl.Int64, "samplename": pl.String, "value": pl.Float64}
MAPPING_SCHEMA = {"geneID": pl.Int64, "pfamAcc": pl.String, "min_iEvalue": pl.Float64}
SELECTION_METHOD = "exact_case_insensitive_taxName_or_semicolon_lineage_token"


class PreparationLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_genes: int = Field(default=10000000, gt=0, le=10000000)
    max_pairs: int = Field(default=10000000, gt=0, le=10000000)
    max_sample_genes: int = Field(default=1000000, gt=0, le=10000000)
    max_samples: int = Field(default=10000, gt=0, le=10000)
    min_free_gib: float = Field(default=10.0, ge=0, allow_inf_nan=False)
    max_seconds: float = Field(default=86400.0, gt=0, allow_inf_nan=False)
    progress_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)


def _number(text: str) -> float:
    # 同时检查十进制和二进制表示，避免极小的正数被悄悄转换为零。
    try:
        exact = Decimal(text)
        value = float(exact)
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise ValueError("数值无法读取") from error
    if not exact.is_finite() or exact < 0 or not math.isfinite(value):
        raise ValueError("数值必须有限且非负")
    if exact != 0 and value == 0:
        raise ValueError("非零数值发生 Float64 下溢；停止转换")
    return value


def _identifier(text: str) -> int:
    if not text.isascii() or not text.isdigit() or not 0 < int(text) < 2**63:
        raise ValueError("geneID 必须是 Int64 范围内的正整数")
    return int(text)


def _lines(stream, header: str):
    if stream.readline(4097).rstrip("\r\n") != header:
        raise ValueError("输入表头不符合契约")
    while line := stream.readline(4097):
        if len(line) > 4096:
            raise ValueError("输入行超过长度保护上限")
        fields = line.rstrip("\r\n").split("\t")
        if len(fields) != len(header.split("\t")):
            raise ValueError("输入行字段数量不符")
        yield fields


def _artifact(path: Path, root: Path, rows: int) -> MatouArtifact:
    return MatouArtifact(
        **{**describe(path).model_dump(), "path": path.relative_to(root).as_posix()}, rows=rows
    )


def prepare_function_data(
    prepared_dir: Path,
    occurrences_dir: Path,
    mapping_dir: Path,
    output_dir: Path,
    limits: PreparationLimits | None = None,
) -> MatouManifest:
    limits = limits or PreparationLimits()
    directories = [p.resolve(strict=True) for p in (prepared_dir, occurrences_dir, mapping_dir)]
    prepared, occurrences, mapping = directories
    reports = {
        name: directory / "report.json"
        for name, directory in zip(
            ("preparation", "occurrences", "mapping"), directories, strict=True
        )
    }
    states = {p: fingerprint(p) for p in reports.values()}
    evidence = {name: describe(path) for name, path in reports.items()}
    prep, occ, pfam = (read_json(reports[n]) for n in ("preparation", "occurrences", "mapping"))
    if any(r.get("status") != "completed" for r in (prep, occ, pfam)):
        raise ValueError("需要三份完整成功报告")
    if (prep.get("script_version"), occ.get("script_version"), pfam.get("script_version")) != (
        "2",
        "1",
        "2",
    ):
        raise ValueError("不支持的准备报告版本")
    if occ.get("scope") != "full_occurrence_files" or pfam.get("scope") != "prepared_pfam_hits":
        raise ValueError("来源报告的核验范围不符")
    if prep.get("validation", {}).get("selected_taxonomy_geneIDs_unique") is not True:
        raise ValueError("准备报告未证明目标基因唯一")
    for report, required in (
        (occ, ("inputs_and_script_unchanged", "gene_list_digest_matches")),
        (pfam, ("prepared_output_digest_and_counts_match", "inputs_unchanged")),
    ):
        if any(report.get("validation", {}).get(key) is not True for key in required):
            raise ValueError("来源报告缺少已通过的验证状态")
    if len(occ["files"]) != 2 or {f["assay"] for f in occ["files"]} != {"MetaG", "MetaT"}:
        raise ValueError("提取报告需要且仅包含 MetaG 和 MetaT")
    selection = prep["selection"]
    if selection.get("method") != SELECTION_METHOD or any(
        r["selection"] != selection for r in (occ, pfam)
    ):
        raise ValueError("三份报告的分类筛选条件不一致或不支持")
    for report in (occ, pfam):
        bound = FileRecord.model_validate(report["preparation_report"])
        if (bound.sha256, bound.size_bytes) != (
            evidence["preparation"].sha256,
            evidence["preparation"].size_bytes,
        ):
            raise ValueError("未绑定同一份分类准备报告")
    if pfam.get("mapping_method") != {
        "pair": "unique_geneID_pfamAcc",
        "min_iEvalue": "minimum_over_all_original_hits_and_frames",
        "quality_policy": "all_original_pairs_preserved_no_threshold_selected",
    }:
        raise ValueError("不支持的候选映射方法")
    if pfam["source"]["sha256"] != prep["outputs"]["pfam_hits"]["sha256"]:
        raise ValueError("映射不是来自同一份 Pfam 准备产物")
    if occ["gene_ids"]["sha256"] != prep["outputs"]["gene_ids"]["sha256"]:
        raise ValueError("定量提取的目标基因清单不一致")
    sources = {
        "gene_ids": prepared / "gene_ids.tsv",
        "gene_pfam": mapping / "gene_pfam.tsv",
        "samples": occurrences / "samples.tsv",
    }
    sources.update(
        {a: occurrences / f"{a}.selected.occurrences.tsv.gz" for a in ("MetaG", "MetaT")}
    )
    expected = {
        "gene_ids": prep["outputs"]["gene_ids"],
        "gene_pfam": pfam["outputs"]["gene_pfam"],
        **{key: occ["outputs"][key] for key in ("samples", "MetaG", "MetaT")},
    }
    source_records = {key: FileRecord.model_validate(value) for key, value in expected.items()}
    states.update({p: fingerprint(p) for p in sources.values()})
    output = output_dir.resolve()
    raw_parents = [Path(r["source"]["path"]).resolve().parent for r in occ["files"]]
    raw_parents.extend(
        Path(prep[key]["source"]["path"]).resolve().parent for key in ("taxonomy", "pfam")
    )
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in directories + raw_parents):
        raise ValueError("输出须与原始目录和已有准备目录分开")
    if output.exists():
        raise FileExistsError("输出目录已存在，不覆盖旧运行")
    for key in ("gene_ids", "gene_pfam", "samples", "MetaG", "MetaT"):
        check_file(sources[key], source_records[key])
    gene_count = prep["taxonomy"]["selected_unique_geneIDs"]
    if not 0 < gene_count <= limits.max_genes or gene_count != occ["selected_unique_geneIDs"]:
        raise ValueError("目标基因数量不一致或超过保护上限")
    pair_count = pfam["unique_gene_pfam_pairs"]
    if not 0 <= pair_count <= limits.max_pairs:
        raise ValueError("候选映射数量超过保护上限")
    genes = pl.read_csv(
        sources["gene_ids"], separator="\t", schema={"geneID": pl.Int64}, n_rows=gene_count + 1
    )
    if (
        genes.height != gene_count
        or genes["geneID"].null_count()
        or genes["geneID"].min() <= 0
        or genes["geneID"].n_unique() != gene_count
    ):
        raise ValueError("基因清单无效")
    sample_counts = {}
    with sources["samples"].open(encoding="utf-8", newline="") as stream:
        for assay, sample, total, selected, zeros in _lines(
            stream, "assay\tsamplename\ttotal_records\tselected_records\tzero_values"
        ):
            key = (assay, sample)
            if len(sample_counts) >= 2 * limits.max_samples:
                raise ValueError("样本清单超过数量保护上限")
            if (
                assay not in ("MetaG", "MetaT")
                or key in sample_counts
                or not 0 < len(sample) <= 200
                or sample != sample.strip()
                or any(ord(c) < 32 for c in sample)
            ):
                raise ValueError("样本清单无效或重复")
            counts = [int(v) for v in (total, selected, zeros)]
            if (
                not 0 <= counts[1] <= counts[0]
                or not 0 <= counts[2] <= counts[0]
                or counts[1] > limits.max_sample_genes
            ):
                raise ValueError("样本行数无效或超过保护上限")
            sample_counts[key] = counts[1]
    for assay in ("MetaG", "MetaT"):
        files = [f for f in occ["files"] if f["assay"] == assay]
        counts = [count for (a, _), count in sample_counts.items() if a == assay]
        if (
            len(files) != 1
            or not 0 < len(counts) <= limits.max_samples
            or len(counts) != files[0]["sample_count"]
            or sum(counts) != files[0]["selected_records"]
        ):
            raise ValueError("样本清单数量与提取报告不符")
        if any(
            files[0]["validation"].get(key) is not True
            for key in ("full_gzip_read", "gene_sample_keys_unique")
        ):
            raise ValueError("提取报告缺少完整性或唯一键验证")
    started = time.monotonic()
    last_progress = started
    output.parent.mkdir(parents=True, exist_ok=True)

    def guard():
        nonlocal last_progress
        now = time.monotonic()
        if now - started > limits.max_seconds:
            raise ValueError("转换超过时间保护上限")
        if shutil.disk_usage(output.parent).free < limits.min_free_gib * 1024**3:
            raise ValueError("磁盘可用空间低于保留额度")
        if now - last_progress >= limits.progress_seconds:
            print(f"MATOU 转换进行中，耗时 {now - started:.0f} 秒", file=sys.stderr, flush=True)
            last_progress = now

    guard()
    output.mkdir(exist_ok=False)
    rows = []
    with sources["gene_pfam"].open(encoding="utf-8", newline="") as stream:
        for gene, accession, evalue in _lines(stream, "geneID\tpfamAcc\tmin_iEvalue"):
            if len(rows) >= pair_count or re.fullmatch(r"PF[0-9]{5}", accession) is None:
                raise ValueError("候选映射数量或编号无效")
            rows.append((_identifier(gene), accession, _number(evalue)))
            if len(rows) % 100000 == 0:
                guard()
    mapping_frame = pl.DataFrame(rows, schema=MAPPING_SCHEMA, orient="row")
    del rows
    if (
        mapping_frame.height != pair_count
        or mapping_frame.select("geneID", "pfamAcc").unique().height != pair_count
        or mapping_frame.join(genes, on="geneID", how="anti").height
    ):
        raise ValueError("映射数量、唯一键或目标基因范围不符")
    if mapping_frame["geneID"].n_unique() != pfam["genes_with_pfam"]:
        raise ValueError("注释基因数量不符")
    mapping_path = output / "gene_pfam.parquet"
    mapping_frame.write_parquet(mapping_path, compression="zstd")
    mapping_artifact = _artifact(mapping_path, output, pair_count)
    del mapping_frame
    samples = []

    def flush(assay, sample, rows):
        guard()
        count = sample_counts[(assay, sample)]
        if len(rows) != count:
            raise ValueError("完整样本行数与清单不符")
        frame = pl.DataFrame(rows, schema=OCCURRENCE_SCHEMA, orient="row")
        if frame.join(genes, on="geneID", how="anti").height:
            raise ValueError("定量记录包含目标清单之外的基因")
        # 文件名使用顺序号，原始样本名永远不作为路径。
        path = output / f"{assay}-{len(samples):05d}.parquet"
        frame.write_parquet(path, compression="zstd")
        samples.append(
            MatouSample(assay=assay, sample_name=sample, artifact=_artifact(path, output, count))
        )

    for assay in ("MetaG", "MetaT"):
        seen = set()
        current = None
        previous_gene = 0
        rows = []

        with gzip.open(sources[assay], "rt", encoding="utf-8", newline="") as stream:
            for gene, sample, value in _lines(stream, "geneid\tsamplename\tvalue"):
                if (assay, sample) not in sample_counts:
                    raise ValueError("定量记录包含清单之外的样本")
                if sample != current:
                    if current is not None:
                        flush(assay, current, rows)
                    if sample in seen:
                        raise ValueError("样本分散在多个块中")
                    seen.add(sample)
                    current, previous_gene, rows = sample, 0, []
                identifier = _identifier(gene)
                if identifier <= previous_gene or len(rows) >= sample_counts[(assay, sample)]:
                    raise ValueError("基因未严格递增或样本行数超出清单")
                previous_gene = identifier
                rows.append((identifier, sample, _number(value)))
                if len(rows) % 100000 == 0:
                    guard()
        if current is not None:
            flush(assay, current, rows)
        # 完整提取可包含没有目标记录的样本；空输入是未知范围，不能据此宣称生物学零值。
        for (a, sample), count in sample_counts.items():
            if a == assay and sample not in seen:
                if count:
                    raise ValueError("清单中的非空样本缺少定量块")
                rows = []
                flush(assay, sample, rows)
    guard()
    if any(fingerprint(p) != before for p, before in states.items()):
        raise ValueError("转换期间输入发生变化，不发布清单")
    source_records.update(
        {f"raw_{f['assay']}": FileRecord.model_validate(f["source"]) for f in occ["files"]}
    )
    source_records["raw_taxonomy"] = FileRecord.model_validate(prep["taxonomy"]["source"])
    source_records["raw_pfam"] = FileRecord.model_validate(prep["pfam"]["source"])
    manifest = MatouManifest(
        selection=selection,
        source_reports=evidence,
        source_files=source_records,
        gene_pfam=mapping_artifact,
        samples=samples,
        parameters=limits.model_dump(),
    )
    partial = output / "manifest.json.partial"
    partial.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    partial.rename(output / "manifest.json")
    return manifest
