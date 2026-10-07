"""Tara 科学计算的数据契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tara_agent.analysis.models import Page, TaxonMatchMode
from tara_agent.domain.contracts import Marker, ResultMetadata


class MarkerSampleSelection(BaseModel):
    """选择一个标记，并可选定部分背景样本 ID。"""

    model_config = ConfigDict(extra="forbid")

    marker: Marker
    sample_ids: list[str] | None = Field(default=None, max_length=500)

    @field_validator("sample_ids")
    @classmethod
    def validate_sample_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if not normalized or any(not value for value in normalized):
            raise ValueError("sample_ids must contain at least one non-blank ID")
        if len(normalized) != len(set(normalized)):
            raise ValueError("sample_ids must not contain duplicates")
        return normalized


class TaxonSelection(MarkerSampleSelection):
    """按照共用的明确匹配规则选择一个分类单元。"""

    taxon: str = Field(max_length=200)
    match_mode: TaxonMatchMode = TaxonMatchMode.LEVEL

    @field_validator("taxon")
    @classmethod
    def normalize_taxon(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("taxon must not be blank")
        return normalized


class TaxonAbundanceQuery(TaxonSelection):
    """请求原始测序读数和样本内相对丰度。"""

    include_zero_samples: bool = True
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    order_by: Literal["sample_id", "relative_abundance"] = Field(
        default="sample_id",
        description="relative_abundance 在全部所选样本上降序排序后分页，空值最后",
    )
    group_by: Literal["station"] | None = Field(
        default=None,
        description="station 返回所有所选样本按站点等权均值和最大值及两套排名；不是独立重复",
    )
    aggregation: Literal["mean", "max"] = "mean"
    taxonomic_rank: Literal["genus", "species", "asv"] | None = Field(
        default=None,
        description="指定 1–20 个 sample_ids 的类群内组成；每样本分别排名和分页；保留未鉴定分类",
    )

    @model_validator(mode="after")
    def check_composition_scope(self):
        if self.taxonomic_rank is not None:
            if not self.sample_ids or len(self.sample_ids) > 20:
                raise ValueError("属/种/ASV 组成需明确指定 1–20 个样本")
            if self.group_by is not None:
                raise ValueError("组成分析保留各样本，不能同时按站点合并")
        return self


class StationAbundanceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    station: str
    mean_rank: int | None = Field(default=None, ge=1)
    max_rank: int | None = Field(default=None, ge=1)
    mean_relative_abundance: float | None = Field(default=None, ge=0, le=1)
    max_relative_abundance: float | None = Field(default=None, ge=0, le=1)
    sample_count: int = Field(ge=1)
    valid_sample_count: int = Field(ge=0)
    sample_ids: list[str]


class TaxonCompositionObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str
    rank: int = Field(ge=1)
    taxon_label: str
    classification_status: Literal["assigned", "unresolved", "asv"]
    read_count: int = Field(ge=1)
    relative_abundance: float | None = Field(ge=0, le=1)
    fraction_within_selected_taxon: float = Field(ge=0, le=1)
    cumulative_fraction: float = Field(ge=0, le=1)
    asv_count: int = Field(ge=1)
    taxonomy: str


class TaxonCompositionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str
    selected_taxon_read_count: int = Field(ge=0)
    sample_total_read_count: int = Field(ge=0)
    total_groups: int = Field(ge=0)
    returned_groups: int = Field(ge=0)
    unresolved_read_count: int = Field(ge=0)
    dominant_taxon: str | None
    dominant_fraction: float | None = Field(ge=0, le=1)


class TaxonAbundanceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    taxon_read_count: int = Field(ge=0)
    sample_total_read_count: int = Field(ge=0)
    relative_abundance: float | None = Field(default=None, ge=0, le=1)


class TaxonAbundanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marker: Marker
    taxon: str
    match_mode: TaxonMatchMode
    observations: list[TaxonAbundanceObservation]
    page: Page
    matching_asv_count: int = Field(ge=0)
    metadata: ResultMetadata
    group_by: Literal["station"] | None = None
    aggregation: Literal["mean", "max"] | None = None
    groups: list[StationAbundanceSummary] = Field(default_factory=list)
    station_leaders: list[StationAbundanceSummary] = Field(
        default_factory=list,
        description="完整所选范围内均值、最大值各第一名；相同站点去重，并列时按站点名取一个代表，排名仍保留并列",
    )
    group_page: Page | None = None
    taxonomic_rank: Literal["genus", "species", "asv"] | None = None
    composition: list[TaxonCompositionObservation] = Field(default_factory=list)
    composition_summaries: list[TaxonCompositionSummary] = Field(default_factory=list)


class DiversityGroup(StrEnum):
    POLAR = "polar"
    OCEAN_REGION = "ocean_region"
    DEPTH = "depth"
    SIZE_FRACTION = "size_fraction"


class DiversityQuery(MarkerSampleSelection):
    """请求所选样本未进行稀释抽样的 Alpha 多样性。"""

    taxon: str | None = Field(default=None, max_length=200)
    match_mode: TaxonMatchMode = TaxonMatchMode.LEVEL
    group_by: DiversityGroup | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("taxon")
    @classmethod
    def normalize_optional_taxon(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("taxon must not be blank")
        return normalized


class DiversityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    analysis_read_count: int = Field(ge=0)
    observed_asv_richness: int = Field(ge=0)
    shannon_index: float | None = Field(default=None, ge=0)


class DiversityGroupSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str
    sample_count: int = Field(ge=1)
    shannon_sample_count: int = Field(ge=0)
    richness_mean: float = Field(ge=0)
    richness_median: float = Field(ge=0)
    shannon_mean: float | None = Field(default=None, ge=0)
    shannon_median: float | None = Field(default=None, ge=0)


class DiversityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marker: Marker
    taxon: str | None
    match_mode: TaxonMatchMode
    matching_asv_count: int = Field(ge=0)
    observations: list[DiversityObservation]
    page: Page
    group_by: DiversityGroup | None
    groups: list[DiversityGroupSummary]
    metadata: ResultMetadata


class EnvironmentVariable(StrEnum):
    EVENT_LATITUDE = "event_latitude"
    EVENT_LONGITUDE = "event_longitude"
    ABS_LAT = "abs_lat"
    LOWER_SIZE_FRACTION = "lower_size_fraction"
    UPPER_SIZE_FRACTION = "upper_size_fraction"
    PAR = "par"
    DEPTH_BATHY = "depth_bathy"
    LYAPUNOV_EXP = "lyapunov_exp"
    TEMPERATURE = "temperature"
    CHLA = "chla"
    BACKSCATTERING = "backscattering"
    DEPTH_CHL_MAX = "depth_chl_max"
    MIXED_LAYER_DEPTH_SIGMA = "mixed_layer_depth_sigma"
    DEPTH_MAX_BRUNT_VAISALA = "depth_max_brunt_väisälä"
    NITRITE = "nitrite"
    PHOSPHATE = "phosphate"
    NITRATE_NITRITE = "nitrate_nitrite"
    SILICATE = "silicate"
    NSTAR = "nstar"


class EnvironmentAssociationQuery(TaxonSelection):
    """请求一次采用成对完整观测值的 Spearman 关联分析。"""

    environment_variable: EnvironmentVariable
    point_offset: int = Field(default=0, ge=0)
    point_limit: int = Field(default=200, ge=1, le=500)


class EnvironmentAssociationPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    environment_value: float
    relative_abundance: float = Field(ge=0, le=1)


class EnvironmentAssociationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    marker: Marker
    taxon: str
    match_mode: TaxonMatchMode
    environment_variable: EnvironmentVariable
    sample_count: int = Field(ge=0)
    rho: float | None = Field(default=None, ge=-1, le=1)
    p_value: float | None = Field(default=None, ge=0, le=1)
    points: list[EnvironmentAssociationPoint]
    point_page: Page
    metadata: ResultMetadata
