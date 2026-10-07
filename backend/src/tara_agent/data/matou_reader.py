"""只读访问已发布的 MATOU 功能数据；不按名称推断 DNA/RNA 配对。"""

from pathlib import Path
from typing import Literal

import polars as pl

from tara_agent.data.matou_artifacts import (
    MatouArtifact,
    MatouManifest,
    check_file,
    describe,
    fingerprint,
    read_json,
    safe_child,
)
from tara_agent.data.matou_prepare import MAPPING_SCHEMA, OCCURRENCE_SCHEMA
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe


class MatouDataReader:
    def __init__(self, directory: Path):
        self.directory = directory.resolve(strict=True)
        path = self.directory / "manifest.json"
        before = fingerprint(path)
        self.manifest = MatouManifest.model_validate(read_json(path))
        self.manifest_record = describe(path)
        if fingerprint(path) != before:
            raise ValueError("读取期间清单发生变化")
        self._manifest_state = before
        study_path = self.directory / "study_manifest.json"
        self._study_state = fingerprint(study_path) if study_path.exists() else None
        self.study_sha256 = describe(study_path).sha256 if study_path.exists() else None
        reference_path = self.directory / "paper_task2/manifest.json"
        self._reference_state = fingerprint(reference_path) if reference_path.exists() else None
        self.reference_sha256 = describe(reference_path).sha256 if reference_path.exists() else None
        self._samples = {(s.assay, s.sample_name): s for s in self.manifest.samples}
        # 初始化仅核对目录和大小；实际读取时验证内容摘要。
        for artifact in [self.manifest.gene_pfam, *(s.artifact for s in self.manifest.samples)]:
            if safe_child(self.directory, artifact.path).stat().st_size != artifact.size_bytes:
                raise ValueError("产物大小与清单不符")
        if self.study_sha256 is not None:
            from tara_agent.data.matou_study import load_study

            study = load_study(self)
            for artifact in (study.summaries, study.profiles, study.sequences):
                if (
                    artifact is not None
                    and safe_child(self.directory, artifact.path).stat().st_size
                    != artifact.size_bytes
                ):
                    raise ValueError("研究产物大小与清单不符")

    def list_samples(self, assay: Literal["MetaG", "MetaT"]):
        with observe(
            "读取 MATOU 样本清单",
            ObservationKind.DATA,
            ObservationUpdate(data_sources=[self.manifest_record.model_dump()]),
        ) as observation:
            self.ensure_unchanged()
            if assay not in ("MetaG", "MetaT"):
                raise ValueError("不支持的实验类型")
            samples = [s for s in self.manifest.samples if s.assay == assay]
            observation.finish(ObservationUpdate(output_data={"sample_count": len(samples)}))
            return samples

    def sample_artifact(self, assay: str, sample_name: str) -> MatouArtifact:
        try:
            return self._samples[(assay, sample_name)].artifact
        except KeyError as error:
            raise ValueError("未知实验类型或样本；不自动转换编号") from error

    def load_sample(self, assay: str, sample_name: str) -> pl.DataFrame:
        return self._load(
            self.sample_artifact(assay, sample_name), OCCURRENCE_SCHEMA, "读取 MATOU 单样本定量"
        )

    def load_gene_pfam(self) -> pl.DataFrame:
        return self._load(self.manifest.gene_pfam, MAPPING_SCHEMA, "读取 MATOU 候选功能映射")

    def _load(self, artifact: MatouArtifact, schema: dict, name: str) -> pl.DataFrame:
        self.ensure_unchanged()
        path = safe_child(self.directory, artifact.path)
        before = fingerprint(path)
        with observe(
            name,
            ObservationKind.DATA,
            ObservationUpdate(
                data_sources=[
                    {
                        **artifact.model_dump(),
                        "pipeline_version": self.manifest.pipeline_version,
                        "manifest_sha256": self.manifest_record.sha256,
                        "dataset_version": self.manifest.dataset_version,
                    }
                ]
            ),
        ) as observation:
            check_file(path, artifact)
            frame = pl.read_parquet(path)
            if (
                frame.height != artifact.rows
                or frame.schema != schema
                or any(frame.null_count().row(0))
            ):
                raise ValueError("产物行数、类型或空值不符合契约")
            if fingerprint(path) != before:
                raise ValueError("读取期间产物发生变化")
            self.ensure_unchanged()
            observation.finish(ObservationUpdate(output_data={"rows": frame.height}))
            return frame

    def ensure_unchanged(self):
        if fingerprint(self.directory / "manifest.json") != self._manifest_state:
            raise ValueError("清单已变化，请重新创建读取器")
        study_path = self.directory / "study_manifest.json"
        state = fingerprint(study_path) if study_path.exists() else None
        if state != self._study_state:
            raise ValueError("功能谱缓存清单已变化，请重新创建读取器")
        reference_path = self.directory / "paper_task2/manifest.json"
        state = fingerprint(reference_path) if reference_path.exists() else None
        if state != self._reference_state:
            raise ValueError("论文功能参考资料已变化，请重新创建读取器")
