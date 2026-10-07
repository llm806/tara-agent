"""论文可对照功能分析的输入、结果和明确的未完成状态。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tara_agent.domain.contracts import ResultMetadata


class FunctionStudyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["current_data", "paper_reference"] = "current_data"
    top_n: int = Field(default=100, ge=1, le=100)
    include_environment: bool = True
    normalization: Literal["taxon_total", "author_script"] = "taxon_total"


class FunctionRank(BaseModel):
    rank: int
    function_id: str
    function_name: str
    relative_contribution: float = Field(ge=0, allow_inf_nan=False)
    mean_sample_relative_signal: float = Field(ge=0, allow_inf_nan=False)
    member_accessions: list[str]
    observed_groups: int
    reference_rank: int | None = None
    reference_contribution: float | None = None


class FunctionDistribution(BaseModel):
    function_id: str
    function_name: str
    group: str
    allocation_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    observed_groups: int = Field(ge=0)
    mapped_signal_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class TargetSignal(BaseModel):
    target: str
    assay: Literal["MetaG", "MetaT"]
    station: str
    depth: str
    size_fraction: str
    relative_signal: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    numerator: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    denominator: float = Field(ge=0, allow_inf_nan=False)
    denominator_scope: str
    status: Literal["observed", "missing", "paper_zero_fill"]


class PLSCoordinate(BaseModel):
    variable: str
    role: Literal["environment", "response"]
    component_1: float = Field(ge=-1, le=1, allow_inf_nan=False)
    component_2: float = Field(ge=-1, le=1, allow_inf_nan=False)


class PLSResult(BaseModel):
    target: str
    figure: str
    status: Literal["completed", "blocked"]
    reason: str | None = None
    sample_count: int = 0
    excluded_rows: int = 0
    constant_variables: list[str] = Field(default_factory=list)
    coordinates: list[PLSCoordinate] = Field(default_factory=list)
    scores: list[dict[str, str | float]] = Field(default_factory=list)
    explained_variance: list[dict[str, float]] = Field(default_factory=list)
    input_rows: list[dict[str, str | float | None]] = Field(default_factory=list)
    method: str = "PLS2_NIPALS_2_components_scaled_sample_sd_no_inference"


class StudySection(BaseModel):
    figure: str
    output: str
    status: Literal["completed", "blocked"]
    reason: str | None = None


class FunctionStudyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: FunctionStudyQuery
    status: Literal["completed", "partial", "blocked"]
    method_version: str = "diatom-function-study-v1"
    denominator: str
    sample_group_count: int
    top_contribution_sum: float
    source_comparison: str
    sections: list[StudySection]
    function_ranks: list[FunctionRank]
    size_distribution: list[FunctionDistribution]
    ocean_distribution: list[FunctionDistribution]
    target_signals: list[TargetSignal]
    pls_results: list[PLSResult]
    excluded_samples: list[dict[str, str]]
    metadata: ResultMetadata
    artifact_roles: dict[str, Literal["intermediate", "final"]] = Field(
        default_factory=lambda: {
            "function_ranks": "final",
            "size_distribution": "final",
            "ocean_distribution": "final",
            "target_signals": "final",
            "pls_results": "final",
            "sections": "intermediate",
            "excluded_samples": "intermediate",
        }
    )
