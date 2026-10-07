"""功能样本检索与单样本候选功能谱的公开参数契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tara_agent.analysis.function_signal_models import FunctionSignalQuery
from tara_agent.domain.contracts import ResultMetadata


class FunctionSamplesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assay: Literal["MetaG", "MetaT"]
    taxon: str | None = Field(default=None, min_length=1, max_length=200)
    sample_name_contains: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=20000)

    @field_validator("taxon", "sample_name_contains")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is not None and (value != value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("范围条件不能包含首尾空白或控制字符")
        return value


class FunctionProfileQuery(FunctionSignalQuery):
    assay: Literal["MetaG", "MetaT"]
    sample_name: str = Field(min_length=1, max_length=200)
    taxon: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("sample_name", "taxon")
    @classmethod
    def validate_identity(cls, value: str | None) -> str | None:
        return FunctionSamplesQuery.validate_text(value)


class FunctionSampleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_name: str
    observed_gene_records: int = Field(ge=0)


class FunctionSamplesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assay: Literal["MetaG", "MetaT"]
    taxon: str
    dataset_version: str
    query: FunctionSamplesQuery
    total: int = Field(ge=0)
    items: list[FunctionSampleItem]
    metadata: ResultMetadata
