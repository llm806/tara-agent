"""版本化采样编码规则、预计算缓存与只读序列来源核验。"""

import re
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from tara_agent.data.matou_artifacts import (
    FileRecord,
    MatouArtifact,
    check_file,
    describe,
    read_json,
    safe_child,
)
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe

AUTHOR_COMMIT = "2647f6be2cd709f48d4dcde314be3783978afa97"
CODE_EVIDENCE = {
    "version": "published-matou-sample-code-v1",
    "author_commit": AUTHOR_COMMIT,
    "source_sha256": "f3fac9c12224adb81ef6784c4ab60f8b0b05dcfed8f18763526c41a3453e4e57",
    "source": f"https://github.com/JJPierellaKarlusich/Diatom_patters/blob/{AUTHOR_COMMIT}/metaT/scripts/organizador.R",
    "method_reference": "https://doi.org/10.1038/s41467-025-58027-7",
    "fields": ["station", "depth", "iteration", "original_fraction"],
    "dna_protocol": "11",
    "cdna_protocol": "14",
    "scope": "sampling_code_correspondence_not_extraction_identity",
}
SAMPLE_CODE = re.compile(
    r"(?P<station>[1-9][0-9]*)(?P<depth>[A-Z]+)(?P<iteration>[0-9]+)(?P<fraction>[A-Z]+)(?P<protocol>[0-9]{2})"
)
# 不合并论文中的近似过滤组；只有来源脚本明确描述的过滤码进入对应比较。
FRACTIONS = {
    "GGMM",
    "GGQQ",
    "GGZZ",
    "MMQQ",
    "QQSS",
    "SSUU",
    "KKQQ",
    "KKZZ",
    "QQRR",
    "GGKK",
    "CCKK",
    "AACC",
    "CCII",
    "EEGG",
    "CCEE",
    "BBCC",
    "IIQQ",
    "GGRR",
}
DEPTHS = {"SUR", "DCM", "MES", "MXL"}


def correspondence(reader):
    groups = {}
    excluded = []
    for sample in reader.manifest.samples:
        match = SAMPLE_CODE.fullmatch(sample.sample_name)
        if match is None:
            excluded.append(
                {
                    "sample_name": sample.sample_name,
                    "assay": sample.assay,
                    "reason": "unsupported_sample_code",
                }
            )
            continue
        fields = match.groupdict()
        protocol = "11" if sample.assay == "MetaG" else "14"
        if (
            fields["protocol"] != protocol
            or fields["fraction"] not in FRACTIONS
            or fields["depth"] not in DEPTHS
        ):
            excluded.append(
                {
                    "sample_name": sample.sample_name,
                    "assay": sample.assay,
                    "reason": "unsupported_protocol_or_sampling_scope",
                }
            )
            continue
        key = sample.sample_name[:-2]
        sides = groups.setdefault(key, {})
        if sample.assay in sides:
            raise ValueError("同一采样编码存在重复实验，不能推断合并或配对")
        sides[sample.assay] = sample.sample_name
    pairs = []
    for key, sides in sorted(groups.items()):
        if set(sides) == {"MetaG", "MetaT"}:
            pairs.append(
                {
                    "sampling_key": key,
                    "metag_sample": sides["MetaG"],
                    "metat_sample": sides["MetaT"],
                }
            )
        else:
            excluded.extend(
                {
                    "sample_name": name,
                    "assay": assay,
                    "reason": "no_exact_sampling_code_counterpart",
                }
                for assay, name in sides.items()
            )
    return pairs, excluded, CODE_EVIDENCE


class StudyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["matou-study-v1"]
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_i_evalue: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    summaries: MatouArtifact
    profiles: MatouArtifact
    sequences: MatouArtifact | None = None
    fasta_source: FileRecord | None = None
    correspondence_evidence: dict
    sequence_selection: dict | None = None


def load_study(reader):
    reader.ensure_unchanged()
    path = reader.directory / "study_manifest.json"
    if not path.exists():
        return None
    before = describe(path)
    if before.sha256 != reader.study_sha256:
        raise ValueError("研究清单内容已变化，请重新创建读取器")
    manifest = StudyManifest.model_validate(read_json(path))
    check_file(path, before)
    if (
        manifest.version != "matou-study-v1"
        or manifest.source_manifest_sha256 != reader.manifest_record.sha256
        or manifest.correspondence_evidence != CODE_EVIDENCE
    ):
        raise ValueError("功能谱缓存与当前数据或采样编码规则不一致，请重新准备")
    return manifest


def load_study_frame(reader, artifact, name):
    reader.ensure_unchanged()
    with observe(
        name,
        ObservationKind.DATA,
        ObservationUpdate(
            data_sources=[artifact.model_dump(), {"study_manifest_sha256": reader.study_sha256}]
        ),
    ) as observation:
        path = safe_child(reader.directory, artifact.path)
        before = describe(path)
        if (before.sha256, before.size_bytes) != (artifact.sha256, artifact.size_bytes):
            raise ValueError("功能谱或序列产物摘要不符合清单")
        frame = pl.read_parquet(path)
        check_file(path, artifact)
        if frame.height != artifact.rows:
            raise ValueError("功能谱或序列产物行数不符合清单")
        reader.ensure_unchanged()
        observation.finish(ObservationUpdate(output_data={"rows": frame.height}))
        return frame


def load_sequences(reader):
    study = load_study(reader)
    if study is None or study.sequences is None or study.fasta_source is None:
        raise ValueError("未准备核酸序列；先执行 prepare-study 并指定官方 FASTA 和目标 Pfam")
    frame = load_study_frame(reader, study.sequences, "读取 MATOU 核酸序列派生产物")
    if (
        frame.schema != {"geneID": pl.Int64, "header": pl.String, "sequence": pl.String}
        or any(frame.null_count().row(0))
        or frame["geneID"].n_unique() != frame.height
    ):
        raise ValueError("核酸序列产物的类型、空值或唯一键不符合契约")
    return frame, {
        "source": study.fasta_source.model_dump(),
        "selection": study.sequence_selection,
        "artifact": study.sequences.model_dump(),
        "study_manifest_sha256": reader.study_sha256,
    }
