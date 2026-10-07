"""论文功能分析参考资料的离线读取；不执行作者脚本，不猜样本映射。"""

import math
from pathlib import Path

import polars as pl

from tara_agent.data.matou_artifacts import FileRecord, check_file, describe, read_json, safe_child
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe

AUTHOR_COMMIT = "2647f6be2cd709f48d4dcde314be3783978afa97"
REFERENCE_VERSION = "diatom-paper-task2-v1"
REFERENCE_URL = f"https://github.com/JJPierellaKarlusich/Diatom_patters/tree/{AUTHOR_COMMIT}"


class FunctionReference:
    def __init__(self, directory: Path):
        self.directory = directory.resolve(strict=True)
        self.record = describe(self.directory / "manifest.json")
        self.manifest = read_json(self.directory / "manifest.json")
        if (self.manifest.get("version"), self.manifest.get("author_commit")) != (
            REFERENCE_VERSION,
            AUTHOR_COMMIT,
        ):
            raise ValueError("功能分析参考资料版本不匹配")

    def frame(self, key: str) -> pl.DataFrame:
        record = FileRecord.model_validate(self.manifest["files"][key])
        path = safe_child(self.directory, record.path)
        with observe(
            "读取论文功能分析资料：" + key,
            ObservationKind.DATA,
            ObservationUpdate(data_sources=[record.model_dump(), {"url": REFERENCE_URL}]),
        ) as observation:
            check_file(self.directory / "manifest.json", self.record)
            check_file(path, record)
            frame = pl.read_parquet(path)
            check_file(path, record)
            observation.finish(ObservationUpdate(output_data={"rows": frame.height}))
            return frame


def prepare_reference(source_dir: Path, output_dir: Path, *, matou_dir: Path | None = None) -> None:
    """将明确下载的作者文件转为带摘要的资料包，拒绝覆盖已有目录。"""
    import gzip

    output_dir.mkdir(parents=True, exist_ok=False)
    files = {}
    sources = {}
    names = {
        "pfam": "PfamA.list",
        "oceans": "station_ocean.tsv",
        "environment": "physicochemistry.for.metaT.tsv",
        "coordinates": "station_Lat_Long_uniq.withTaraPrefix.tsv",
        "MetaG": "Bacillariophyta.MATOU-v1.5.Pfam.metaG.tsv.gz",
        "MetaT": "Bacillariophyta.MATOU-v1.5.Pfam.metaT.tsv.gz",
        "lhc": "LHC.diatoms.MATOUv1.5.seqs.function.tsv.gz",
    }
    lhc = None
    for key, name in names.items():
        path = source_dir / name
        source = describe(path)
        if key in ("MetaG", "MetaT", "lhc"):
            with gzip.open(path, "rb") as stream:
                frame = pl.read_csv(stream, separator="\t", null_values=["NA", "NaN"])
        else:
            frame = pl.read_csv(path, separator="\t", null_values=["NA", "NaN"])
        if key == "pfam":
            frame.columns = ["pfam_accession", "name", "description"]
        if key == "lhc":
            frame = frame.select(
                pl.col("unigene").str.replace(r"^MATOU-v1\.5\.", "").cast(pl.Int64).alias("geneID"),
                pl.col("LHCtype").alias("subfamily"),
            )
            if frame["geneID"].n_unique() != frame.height:
                raise ValueError("作者LHC注释存在重复基因编号")
            lhc = frame
        target = output_dir / (key + ".parquet")
        frame.write_parquet(target)
        files[key] = {**describe(target).model_dump(), "path": target.name}
        sources[key] = source.model_dump()
        check_file(path, source)
    import json

    extra = {}
    if matou_dir is not None:
        from tara_agent.data.matou_reader import MatouDataReader

        reader = MatouDataReader(matou_dir)
        if reader.manifest.selection["taxon"].casefold() != "bacillariophyta":
            raise ValueError("LHC预计算只适用于硅藻数据")
        import time

        start = time.monotonic()
        records = []
        for sample in reader.manifest.samples:
            if time.monotonic() - start > 12 * 3600:
                raise ValueError("LHC预计算超过12小时保护上限")
            signal = reader.load_sample(sample.assay, sample.sample_name)
            if signal.filter(
                pl.col("value").is_null() | ~pl.col("value").is_finite() | (pl.col("value") < 0)
            ).height:
                raise ValueError("LHC预计算输入包含缺失、负值或非有限信号")
            linked = signal.join(lhc, on="geneID", how="inner")
            for row in (
                linked.group_by("subfamily").agg(pl.col("value").sum()).iter_rows(named=True)
            ):
                if (
                    not isinstance(row["value"], float)
                    or not math.isfinite(row["value"])
                    or row["value"] < 0
                ):
                    raise ValueError("LHC信号求和溢出")
                records.append(
                    {
                        "assay": sample.assay,
                        "sample_name": sample.sample_name,
                        "subfamily": row["subfamily"],
                        "value_sum": row["value"],
                    }
                )
        frame = pl.DataFrame(
            records,
            schema={
                "assay": pl.String,
                "sample_name": pl.String,
                "subfamily": pl.String,
                "value_sum": pl.Float64,
            },
        )
        target = output_dir / "lhc_profiles.parquet"
        frame.write_parquet(target)
        files["lhc_profiles"] = {**describe(target).model_dump(), "path": target.name}
        extra["lhc_source_manifest_sha256"] = reader.manifest_record.sha256
        reader.ensure_unchanged()
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": REFERENCE_VERSION,
                "author_commit": AUTHOR_COMMIT,
                "source_url": REFERENCE_URL,
                "files": files,
                "sources": sources,
                **extra,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
