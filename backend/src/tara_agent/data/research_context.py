"""独立研究资料包；固定来源、单位及核心/MATOU版本，不按样本名猜跨库映射。"""

from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tara_agent.data.matou_artifacts import FileRecord, check_file, describe, read_json, safe_child
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe


class ResearchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1)
    file: FileRecord
    version: str | None = None


class ResearchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["research-context-v1", "research-context-v2"]
    core_generation: str = Field(min_length=1)
    files: dict[Literal["environment", "sample_mapping", "sample_environment"], FileRecord]
    sources: dict[Literal["environment", "sample_mapping", "sample_environment"], ResearchSource]
    units: dict[str, str] = Field(default_factory=dict)
    matou_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mapping_coverage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_sources(self):
        if not self.files or self.files.keys() != self.sources.keys():
            raise ValueError("研究资料产物与来源不完整")
        if "sample_environment" in self.files and (
            self.version != "research-context-v2" or not self.sources["sample_environment"].version
        ):
            raise ValueError("样本环境补充需要v2资料包和明确来源版本")
        if "sample_mapping" in self.files and (
            not self.matou_manifest_sha256 or not self.sources["sample_mapping"].version
        ):
            raise ValueError("跨库映射缺少数据版本或证据版本")
        return self


class ResearchContext:
    def __init__(self, processed_dir: Path):
        self.directory = processed_dir / "research"
        self.path = self.directory / "manifest.json"
        self.record = describe(self.path) if self.path.exists() else None
        self.manifest = (
            ResearchManifest.model_validate(read_json(self.path)).model_dump()
            if self.record
            else None
        )

    @property
    def sha256(self):
        return self.record.sha256 if self.record else None

    def ensure_unchanged(self):
        if (self.record is None) != (not self.path.exists()):
            raise ValueError("研究资料包出现或消失，请重启数据服务")
        if self.record:
            check_file(self.path, self.record)

    def frame(self, key, *, generation, matou_sha256=None):
        self.ensure_unchanged()
        if not self.manifest or key not in self.manifest["files"]:
            raise ValueError("未准备研究资料：" + key)
        if self.manifest["core_generation"] != generation:
            raise ValueError("研究资料与核心数据版本不匹配，请重新准备")
        if key == "sample_mapping" and self.manifest.get("matou_manifest_sha256") != matou_sha256:
            raise ValueError("跨库映射与MATOU版本不匹配，请重新核验")
        record = FileRecord.model_validate(self.manifest["files"][key])
        path = safe_child(self.directory, record.path)
        with observe(
            "读取研究资料：" + key,
            ObservationKind.DATA,
            ObservationUpdate(
                data_sources=[record.model_dump(), {"research_manifest_sha256": self.sha256}]
            ),
        ):
            check_file(path, record)
            frame = pl.read_parquet(path)
            if key == "sample_mapping":
                required = {"assay", "sample_name", "sample_id_pangaea", "evidence"}
                if (
                    set(frame.columns) != required
                    or any(frame.null_count().row(0))
                    or frame.unique(["assay", "sample_name"]).height != frame.height
                ):
                    raise ValueError("跨库映射结构或唯一键无效")
            elif key == "sample_environment":
                validate_sample_environment(frame)
            else:
                if (
                    not {"station", "depth"} <= set(frame.columns)
                    or frame.unique(["station", "depth"]).height != frame.height
                ):
                    raise ValueError("研究环境表结构或唯一键无效")
                if not set(frame.columns) - {"station", "depth"} <= self.manifest["units"].keys():
                    raise ValueError("研究环境表缺少单位声明")
            check_file(path, record)
            self.ensure_unchanged()
            return frame


def validate_sample_environment(frame):
    """样本身份与环境量分别保留证据；不按站位推断，不填补测量缺失。"""
    required = {
        "sample_id_pangaea",
        "variable",
        "value",
        "unit",
        "evidence",
        "method",
        "context_details",
    }
    if set(frame.columns) != required or not frame.height:
        raise ValueError("样本环境补充表列不完整或为空")
    for field in required - {"value"}:
        if frame[field].dtype != pl.String or any(
            v is None or not v.strip() or v != v.strip() for v in frame[field]
        ):
            raise ValueError("样本环境补充表缺少有效文本：" + field)
    if frame.unique(["sample_id_pangaea", "variable"]).height != frame.height:
        raise ValueError("样本环境补充表存在重复样本变量")
    if (
        not frame["value"].dtype.is_numeric()
        or frame.filter(pl.col("value").is_not_null() & ~pl.col("value").is_finite()).height
    ):
        raise ValueError("样本环境值必须为有限数值或缺失")
    return frame


def station_key(value):
    """站位编号格式统一不用于证明样本身份；仅连接作者站位环境表。"""
    value = str(value).removeprefix("TARA_")
    if not value.isdigit():
        raise ValueError("站位格式无效")
    return str(int(value))


def prepare_research(
    processed_dir,
    output_dir,
    *,
    environment=None,
    sample_mapping=None,
    mapping_source=None,
    mapping_version=None,
    matou_dir=None,
    units=None,
    environment_source=None,
    sample_environment=None,
    sample_environment_source=None,
    sample_environment_version=None,
):
    """只接受显式证据和唯一键；清单最后发布，拒绝覆盖，原始数据只读。"""
    import json

    from tara_agent.data.matou_reader import MatouDataReader
    from tara_agent.data.reader import ProcessedDataReader

    reader = ProcessedDataReader(processed_dir)
    context = reader.load_sample_context()
    files, sources, extra = {}, {}, {}
    frames = {}
    supplement_ids = set()
    if sample_environment is not None:
        if not sample_environment_source or not sample_environment_version:
            raise ValueError("样本环境补充需要来源和版本")
        record = describe(sample_environment)
        frame = pl.read_csv(sample_environment, separator="\t", infer_schema=False)
        if "value" not in frame.columns:
            raise ValueError("样本环境补充缺少value列")
        frame = frame.with_columns(pl.col("value").cast(pl.Float64))
        validate_sample_environment(frame)
        supplement_ids = set(frame["sample_id_pangaea"])
        frames["sample_environment"] = frame
        sources["sample_environment"] = {
            "source": sample_environment_source,
            "version": sample_environment_version,
            "file": record.model_dump(),
        }
        check_file(sample_environment, record)
    if environment is not None:
        if not environment_source or not units:
            raise ValueError("环境表必须记录来源和变量单位")
        record = describe(environment)
        frame = pl.read_csv(environment, separator="\t", null_values=["NA", "NaN", ""])
        if not {"station", "depth"} <= set(frame.columns):
            raise ValueError("环境表需要station、depth列")
        frame = frame.with_columns(
            pl.col("station").cast(pl.String).map_elements(station_key, return_dtype=pl.String)
        )
        if (
            frame.select("station", "depth").null_count().row(0) != (0, 0)
            or frame.unique(["station", "depth"]).height != frame.height
        ):
            raise ValueError("环境表存在空键或重复站位水层")
        for field in set(frame.columns) - {"station", "depth"}:
            if field not in units or not units[field]:
                raise ValueError("缺少环境单位：" + field)
            frame = frame.with_columns(pl.col(field).cast(pl.Float64))
            if frame.filter(pl.col(field).is_not_null() & ~pl.col(field).is_finite()).height:
                raise ValueError("环境值必须有限或为空")
        frames["environment"] = frame
        sources["environment"] = {"source": environment_source, "file": record.model_dump()}
        extra["units"] = units
        check_file(environment, record)
    if sample_mapping is not None:
        if not mapping_source or not mapping_version or matou_dir is None:
            raise ValueError("跨库映射需要证据来源、版本和MATOU目录")
        matou = MatouDataReader(matou_dir)
        record = describe(sample_mapping)
        frame = pl.read_csv(sample_mapping, separator="\t", infer_schema=False)
        expected = {"assay", "sample_name", "sample_id_pangaea", "evidence"}
        if set(frame.columns) != expected or any(frame.null_count().row(0)):
            raise ValueError(
                "映射须有且仅有assay、sample_name、sample_id_pangaea、evidence四列且非空"
            )
        if any(
            not str(v).strip() or str(v) != str(v).strip() for row in frame.iter_rows() for v in row
        ):
            raise ValueError("映射字段不能空白或有首尾空格")
        if frame.unique(["assay", "sample_name"]).height != frame.height:
            raise ValueError("一个MATOU实验样本不能指向多个PANGAEA样本")
        if (
            not set(frame["sample_id_pangaea"])
            <= set(context["sample_id_pangaea"]) | supplement_ids
        ):
            raise ValueError("映射含未知PANGAEA编号")
        available = {(s.assay, s.sample_name) for s in matou.manifest.samples}
        if not set(frame.select("assay", "sample_name").iter_rows()) <= available:
            raise ValueError("映射含未知MATOU实验或样本")
        frames["sample_mapping"] = frame
        sources["sample_mapping"] = {
            "source": mapping_source,
            "version": mapping_version,
            "file": record.model_dump(),
        }
        extra["matou_manifest_sha256"] = matou.manifest_record.sha256
        extra["mapping_coverage"] = {"mapped": frame.height, "available": len(available)}
        check_file(sample_mapping, record)
    if not frames:
        raise ValueError("至少提供环境表或经核验的样本映射")
    output_dir = Path(output_dir).resolve()
    # 派生产物不得覆盖输入；源目录也不能被新输出包含。
    inputs = [
        Path(p).resolve()
        for p in (environment, sample_mapping, sample_environment)
        if p is not None
    ]
    if any(p.is_relative_to(output_dir) for p in inputs):
        raise ValueError("研究资料输出不能包含原始输入")
    output_dir.mkdir(parents=True, exist_ok=False)
    for key, frame in frames.items():
        target = output_dir / (key + ".parquet")
        frame.write_parquet(target)
        files[key] = {**describe(target).model_dump(), "path": target.name}
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": "research-context-v2"
                if sample_environment is not None
                else "research-context-v1",
                "core_generation": reader.manifest.generation,
                "files": files,
                "sources": sources,
                **extra,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
