"""MATOU 独立功能数据的版本化产物契约与只读校验。"""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MatouArtifact(FileRecord):
    rows: int = Field(ge=0)


class MatouSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assay: Literal["MetaG", "MetaT"]
    sample_name: str = Field(min_length=1, max_length=200)
    artifact: MatouArtifact


class MatouManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed"] = "completed"
    manifest_version: Literal[1] = 1
    pipeline_version: Literal["matou-function-data-v1"] = "matou-function-data-v1"
    dataset_version: Literal["MATOU-v1.5"] = "MATOU-v1.5"
    selection: dict[str, str]
    source_reports: dict[str, FileRecord]
    source_files: dict[str, FileRecord]
    gene_pfam: MatouArtifact
    samples: list[MatouSample] = Field(min_length=1, max_length=20000)
    parameters: dict[str, int | float]
    numeric_representation: Literal["Float64_nonzero_underflow_rejected"] = (
        "Float64_nonzero_underflow_rejected"
    )

    @model_validator(mode="after")
    def validate_scope(self):
        keys = [(s.assay, s.sample_name) for s in self.samples]
        if len(keys) != len(set(keys)):
            raise ValueError("清单包含重复样本")
        if not self.selection.get("taxon"):
            raise ValueError("清单缺少目标类群")
        if set(self.source_reports) != {"preparation", "occurrences", "mapping"}:
            raise ValueError("清单缺少来源报告")
        if not {"MetaG", "MetaT", "gene_pfam", "gene_ids"} <= self.source_files.keys():
            raise ValueError("清单缺少输入来源")
        return self


def fingerprint(path: Path) -> tuple[int, int, int, int]:
    state = path.stat()
    return state.st_size, state.st_mtime_ns, state.st_ctime_ns, state.st_ino


def describe(path: Path) -> FileRecord:
    before = fingerprint(path)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if fingerprint(path) != before:
        raise ValueError(f"读取期间文件变化：{path}")
    return FileRecord(path=str(path), size_bytes=before[0], sha256=digest)


def check_file(path: Path, expected: FileRecord) -> None:
    actual = describe(path)
    if (actual.size_bytes, actual.sha256) != (expected.size_bytes, expected.sha256):
        raise ValueError(f"文件大小或 SHA-256 不符：{path}")


def read_json(path: Path) -> dict:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("报告超过大小保护上限")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("报告必须是 JSON 对象")
    return result


def safe_child(parent: Path, relative: str) -> Path:
    child = Path(relative)
    if child.is_absolute() or ".." in child.parts:
        raise ValueError("产物必须使用目录内的相对路径")
    path = (parent / child).resolve(strict=True)
    if not path.is_relative_to(parent) or not path.is_file():
        raise ValueError("产物路径越界或不是文件")
    return path
