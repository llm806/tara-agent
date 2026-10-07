"""跨样本功能谱、来源明确的采样编码对应比较和序列查询契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tara_agent.analysis.function_signal_models import FunctionSignalQuery
from tara_agent.domain.contracts import ResultMetadata


class AtlasQuery(FunctionSignalQuery):
    assay: Literal["MetaG", "MetaT"] = "MetaT"
    sample_names: list[str] | None = Field(default=None, min_length=1, max_length=2000)
    taxon: str | None = Field(default=None, min_length=1, max_length=200)
    top_n: int = Field(default=20, ge=1, le=100)

    @field_validator("sample_names")
    @classmethod
    def names(cls, values):
        if values is not None and (
            len(values) != len(set(values))
            or any(not v or v != v.strip() or any(ord(c) < 32 for c in v) for v in values)
        ):
            raise ValueError("样本名须准确、非空且不能重复")
        return values


class FunctionComparisonQuery(FunctionSignalQuery):
    pfam_accessions: list[str] = Field(min_length=1, max_length=20)
    sampling_keys: list[str] | None = Field(default=None, min_length=1, max_length=2000)
    taxon: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("sampling_keys")
    @classmethod
    def keys(cls, values):
        return AtlasQuery.names(values)


class SequenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gene_ids: list[int] | None = Field(default=None, min_length=1, max_length=100)
    pfam_accession: str | None = Field(default=None, pattern=r"^PF[0-9]{5}$")
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000000)

    @field_validator("gene_ids")
    @classmethod
    def genes(cls, values):
        if values is not None and (len(values) != len(set(values)) or any(g <= 0 for g in values)):
            raise ValueError("geneID 必须为唯一的正整数")
        return values


class AtlasObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assay: str
    sample_name: str
    pfam_accession: str
    status: Literal["observed", "no_observed_gene_record", "not_in_retained_mapping"]
    value_sum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    observed_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    taxon_value_sum: float = Field(ge=0, allow_inf_nan=False)


class AtlasRank(BaseModel):
    pfam_accession: str
    rank: int
    mean_observed_contribution: float = Field(ge=0, le=1, allow_inf_nan=False)
    samples_with_observed_records: int
    informative_samples: int
    median_fraction_when_observed: float | None = None


class SampleSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assay: Literal["MetaG", "MetaT"]
    sample_name: str
    taxon_value_sum: float = Field(ge=0, allow_inf_nan=False)
    observed_gene_count: int = Field(ge=0)


class ComparisonPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sampling_key: str
    metag_sample: str
    metat_sample: str
    pfam_accession: str
    metag_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    metat_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    fraction_difference: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    metag_status: Literal["observed", "no_observed_gene_record", "not_in_retained_mapping"]
    metat_status: Literal["observed", "no_observed_gene_record", "not_in_retained_mapping"]
    metag_denominator: float = Field(ge=0, allow_inf_nan=False)
    metat_denominator: float = Field(ge=0, allow_inf_nan=False)


class ComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pfam_accession: str
    complete_correspondences: int = Field(ge=0)
    excluded_correspondences: int = Field(ge=0)
    spearman_rho: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    metag_median_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    metat_median_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    median_fraction_difference: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    correlation_status: Literal["defined", "insufficient_or_constant"]
    inference: Literal["descriptive_no_significance_test"]
    metag_iqr: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    metat_iqr: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    metag_population_cv: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    metat_population_cv: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ExcludedSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_name: str
    assay: Literal["MetaG", "MetaT"]
    reason: Literal[
        "unsupported_sample_code",
        "unsupported_protocol_or_sampling_scope",
        "no_exact_sampling_code_counterpart",
    ]


class SequenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    geneID: int = Field(gt=0)
    header: str = Field(pattern=r"^MATOU-v1\.5\.[1-9][0-9]*$")
    sequence: str = Field(min_length=1, pattern=r"^[ACGTRYSWKMBDHVN]+$")

    @model_validator(mode="after")
    def validate_identity(self):
        if self.header != f"MATOU-v1.5.{self.geneID}":
            raise ValueError("FASTA 标题与 geneID 不一致")
        return self


class AtlasResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: AtlasQuery
    taxon: str
    dataset_version: str
    method_version: str = "observed-function-atlas-v1"
    denominator: str = "all_observed_taxon_gene_values_per_original_sample"
    ranking_method: str = "equal_sample_mean_of_observed_contributions"
    sample_count: int
    informative_samples: int
    zero_denominator_samples: list[str]
    family_count: int
    ranks: list[AtlasRank]
    observations: list[AtlasObservation]
    sample_summaries: list[SampleSummary]
    metadata: ResultMetadata


class ComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: FunctionComparisonQuery
    method_version: str = "sampling-code-relative-signal-comparison-v1"
    pairing_method: str = "published_sampling_code_exact_station_depth_iteration_fraction"
    denominator: str = "each_assay_all_observed_taxon_gene_values"
    comparison_interpretation: str = "relative_contributions_at_corresponding_sampling_codes"
    matched_sampling_keys: int
    excluded_samples: list[ExcludedSample]
    summaries: list[ComparisonSummary]
    points: list[ComparisonPoint]
    metadata: ResultMetadata


class SequenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: SequenceQuery
    total: int
    sequences: list[SequenceItem]
    unavailable_gene_ids: list[int] = Field(default_factory=list)
    metadata: ResultMetadata
