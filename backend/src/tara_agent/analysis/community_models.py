"""群落和功能环境分析的白名单契约；选择输出而非绑定某一个科研问题。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tara_agent.analysis.compute_models import EnvironmentVariable
from tara_agent.analysis.function_study_models import PLSResult, StudySection
from tara_agent.analysis.models import TaxonMatchMode
from tara_agent.domain.contracts import Marker, ResultMetadata


class CommunityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    markers: list[Marker] = Field(
        default_factory=lambda: [Marker.V4, Marker.V9], min_length=1, max_length=2
    )
    taxon: str = Field(default="Bacillariophyta", min_length=1, max_length=200)
    match_mode: TaxonMatchMode = TaxonMatchMode.LEVEL
    sample_ids: list[str] | None = Field(default=None, min_length=1, max_length=2000)
    depths: list[Literal["SRF", "DCM"]] = Field(
        default_factory=lambda: ["SRF", "DCM"], min_length=1, max_length=2
    )
    outputs: list[Literal["distribution", "diversity", "association", "ordination"]] = Field(
        default_factory=lambda: ["distribution", "diversity", "association", "ordination"],
        min_length=1,
        max_length=4,
    )
    environment_source: Literal["paper", "context_stat"] = "paper"
    environment_variables: list[str] | None = Field(default=None, min_length=1, max_length=20)
    min_total_reads: int = Field(default=3, ge=1)
    min_sample_occurrence: int = Field(default=2, ge=1)
    permutations: int = Field(default=999, ge=99, le=9999)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    nmds_starts: int = Field(default=20, ge=2, le=50)
    nmds_max_iterations: int = Field(default=1000, ge=100, le=5000)

    @field_validator("markers", "sample_ids", "depths", "outputs", "environment_variables")
    @classmethod
    def unique(cls, values):
        if values is not None and len(values) != len(set(values)):
            raise ValueError("列表不能含重复项")
        if values is not None and any(
            isinstance(v, str) and (not v.strip() or v != v.strip()) for v in values
        ):
            raise ValueError("列表项不能为空或带首尾空格")
        return values

    @field_validator("taxon")
    @classmethod
    def taxon_text(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError("分类群不能为空或带首尾空格")
        return value


class CommunityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: CommunityQuery
    status: Literal["completed", "partial", "blocked"]
    method_version: str = "community-study-v1"
    sections: list[StudySection]
    observations: list[dict]
    groups: list[dict]
    latitude_bands: list[dict] = Field(default_factory=list)
    size_signal_summary: list[dict] = Field(default_factory=list)
    associations: list[dict]
    pls_results: list[PLSResult]
    size_composition: list[dict]
    ordinations: list[dict]
    excluded_samples: list[dict]
    metadata: ResultMetadata
    artifact_roles: dict[str, Literal["intermediate", "final"]] = Field(
        default_factory=lambda: {
            "observations": "intermediate",
            "groups": "final",
            "latitude_bands": "final",
            "size_signal_summary": "final",
            "associations": "final",
            "pls_results": "final",
            "size_composition": "final",
            "ordinations": "final",
            "excluded_samples": "intermediate",
            "sections": "intermediate",
        }
    )


class FunctionEnvironmentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pfam_accession: str = Field(pattern=r"^PF\d{5}$")
    taxon: str | None = Field(default=None, min_length=1, max_length=200)
    assay: Literal["MetaG", "MetaT"] = "MetaT"
    environment_variable: EnvironmentVariable = EnvironmentVariable.TEMPERATURE
    sample_names: list[str] | None = Field(default=None, min_length=1, max_length=2000)
    signal: Literal["relative", "provided_sum"] = "provided_sum"
    max_i_evalue: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    permutations: int = Field(default=999, ge=99, le=9999)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)

    @field_validator("sample_names")
    @classmethod
    def unique(cls, values):
        if values is not None and (
            len(values) != len(set(values)) or any(not v.strip() for v in values)
        ):
            raise ValueError("样本名不能为空或重复")
        return values


class FunctionEnvironmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: FunctionEnvironmentQuery
    status: Literal["completed", "partial", "blocked"]
    method_version: str = "function-environment-v1"
    points: list[dict]
    associations: list[dict]
    excluded_samples: list[dict]
    mapping_coverage: dict
    metadata: ResultMetadata
    artifact_roles: dict[str, Literal["intermediate", "final"]] = Field(
        default_factory=lambda: {
            "points": "final",
            "associations": "final",
            "excluded_samples": "intermediate",
            "mapping_coverage": "intermediate",
        }
    )
