"""单样本候选功能信号的输入、方法和来源契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tara_agent.domain.contracts import ResultMetadata


class FunctionSignalContext(BaseModel):
    """由数据层绑定的已筛选样本范围；算法不自行判断分类归属。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assay: Literal["MetaG", "MetaT"]
    sample_name: str = Field(min_length=1, max_length=200)
    taxon: str = Field(min_length=1, max_length=200)
    dataset_version: str = Field(min_length=1, max_length=100)
    occurrence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    annotation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("sample_name", "taxon", "dataset_version")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("来源和范围标识不能包含首尾空白或控制字符")
        return value


class FunctionSignalQuery(BaseModel):
    """显式的候选筛选；统一 E-value 只是一项分析条件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pfam_accessions: list[str] | None = Field(default=None, min_length=1, max_length=500)
    max_i_evalue: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    top_n: int = Field(default=20, ge=1, le=500)

    @field_validator("pfam_accessions")
    @classmethod
    def validate_pfams(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if len(values) != len(set(values)):
            raise ValueError("Pfam 编号不能重复")
        if any(
            len(v) != 7 or not v.startswith("PF") or not v[2:].isascii() or not v[2:].isdigit()
            for v in values
        ):
            raise ValueError("Pfam 编号必须采用 PF 加五位数字的格式")
        return values


class FunctionSignalObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pfam_accession: str
    status: Literal["observed", "no_observed_gene_record", "not_in_retained_mapping"]
    observed_gene_count: int = Field(ge=0)
    value_sum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    fraction_of_observed_taxon_signal: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )


class FunctionSignalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: FunctionSignalContext
    query: FunctionSignalQuery
    method_version: Literal["candidate-function-signal-v1"] = "candidate-function-signal-v1"
    annotation_interpretation: Literal["candidate"] = "candidate"
    value_interpretation: Literal["provided_values_export_unit_unconfirmed"] = (
        "provided_values_export_unit_unconfirmed"
    )
    denominator: Literal["all_observed_taxon_gene_values"] = "all_observed_taxon_gene_values"
    observed_gene_count: int = Field(ge=0)
    taxon_value_sum: float = Field(ge=0, allow_inf_nan=False)
    annotated_gene_count: int = Field(ge=0)
    annotated_value_sum: float = Field(ge=0, allow_inf_nan=False)
    annotated_signal_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    genes_with_multiple_pfams: int = Field(ge=0)
    observed_family_count: int = Field(ge=0)
    unobserved_requested_pfams: list[str]
    observations: list[FunctionSignalObservation]
    metadata: ResultMetadata
