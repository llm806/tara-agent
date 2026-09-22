"""从处理后数据提取问题建议所需的真实值。"""

from __future__ import annotations

import polars as pl

from tara_agent.agent.suggestions.models import SampleFilter, SuggestionProfile
from tara_agent.analysis.compute_models import EnvironmentVariable
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.domain.contracts import Marker

MAX_PROFILE_VALUES = 8
MAX_TAXA_PER_MARKER = 10
FEATURED_TAXA = ("Bacillariophyta", "Dinoflagellata")

# 这些变量已有明确的统计含义和用户可理解的名称。其余变量仍可由用户直接提问。
ENVIRONMENT_LABELS = {
    EnvironmentVariable.TEMPERATURE: "温度",
    EnvironmentVariable.CHLA: "叶绿素 a",
    EnvironmentVariable.NITRATE_NITRITE: "硝酸盐与亚硝酸盐",
    EnvironmentVariable.PHOSPHATE: "磷酸盐",
    EnvironmentVariable.SILICATE: "硅酸盐",
    EnvironmentVariable.PAR: "光合有效辐射",
    EnvironmentVariable.ABS_LAT: "绝对纬度",
    EnvironmentVariable.MIXED_LAYER_DEPTH_SIGMA: "混合层深度",
}

_IGNORED_TAXA = frozenset(
    {
        "root",
        "cellular organisms",
        "eukaryota",
        "eukaryota incertae sedis",
        "bacteria",
        "archaea",
    }
)
_IGNORED_TAXON_PATTERN = (
    r"(?i)(^bacteria|^archaea|unclassified|uncultured|unknown|environmental sample)"
)


def read_suggestion_profile(reader: ProcessedDataReader) -> SuggestionProfile:
    context = reader.collect(
        reader.scan_context().select(
            "sample_id_pangaea",
            "ocean_region",
            "depth",
            "size_fraction",
            "polar",
            "temperature",
            *[
                variable.value
                for variable in ENVIRONMENT_LABELS
                if variable is not EnvironmentVariable.TEMPERATURE
            ],
        ),
        name="读取示例问题背景",
        artifacts=["context"],
    )
    return SuggestionProfile(
        sample_ids=_unique_strings(context.get_column("sample_id_pangaea"), limit=6),
        ocean_regions=_unique_strings(
            context.get_column("ocean_region"), limit=MAX_PROFILE_VALUES
        ),
        depths=_unique_strings(context.get_column("depth"), limit=MAX_PROFILE_VALUES),
        size_fractions=_unique_strings(
            context.get_column("size_fraction"), limit=MAX_PROFILE_VALUES
        ),
        polar_options=_available_polar_options(context.get_column("polar")),
        temperatures=_representative_temperatures(context.get_column("temperature")),
        sample_filters=_sample_filters(context, limit=MAX_PROFILE_VALUES),
        environment_variables=_available_environment_variables(context),
        taxa_by_marker={marker: _read_marker_taxa(reader, marker) for marker in Marker},
    )


def _read_marker_taxa(reader: ProcessedDataReader, marker: Marker) -> tuple[str, ...]:
    """按数据中的出现范围和读数选择真实存在且适合展示的分类群。"""

    query = (
        reader.scan_asv_metadata(marker)
        .select(
            pl.col("taxonomy").str.split(";").alias("taxon"),
            "total",
            "spread",
        )
        .explode("taxon", empty_as_null=True)
        .with_columns(pl.col("taxon").str.strip_chars())
        .filter(
            pl.col("taxon").str.len_chars() >= 3,
            ~pl.col("taxon").str.to_lowercase().is_in(_IGNORED_TAXA),
            ~pl.col("taxon").str.contains(_IGNORED_TAXON_PATTERN),
        )
        .group_by("taxon")
        .agg(
            pl.col("spread").sum().alias("occurrence_score"),
            pl.col("total").sum().alias("read_count"),
        )
        .with_columns(pl.col("taxon").is_in(FEATURED_TAXA).alias("featured"))
        .sort(
            ["featured", "occurrence_score", "read_count", "taxon"],
            descending=[True, True, True, False],
        )
        .limit(MAX_TAXA_PER_MARKER)
    )
    taxa = reader.collect(
        query,
        name="选择示例问题分类群",
        artifacts=[f"{marker.value}_metadata"],
        marker=marker,
        streaming=True,
    )
    return tuple(taxa.get_column("taxon").to_list())


def _unique_strings(column: pl.Series, *, limit: int) -> tuple[str, ...]:
    values = column.drop_nulls().cast(pl.String).str.strip_chars().unique(maintain_order=True)
    return tuple(value for value in values.head(limit).to_list() if value)


def _sample_filters(context: pl.DataFrame, *, limit: int) -> tuple[SampleFilter, ...]:
    rows = (
        context.select("ocean_region", "depth", "size_fraction")
        .drop_nulls()
        .unique(maintain_order=True)
        .head(limit)
        .iter_rows(named=True)
    )
    return tuple(SampleFilter(**row) for row in rows)


def _available_polar_options(column: pl.Series) -> tuple[bool, ...]:
    values = {
        value.strip().casefold()
        for value in column.drop_nulls().to_list()
        if isinstance(value, str)
    }
    options: list[bool] = []
    if "polar" in values:
        options.append(True)
    if values & {"non polar", "non-polar", "nonpolar"}:
        options.append(False)
    return tuple(options)


def _available_environment_variables(
    context: pl.DataFrame,
) -> tuple[EnvironmentVariable, ...]:
    available: list[EnvironmentVariable] = []
    for variable in ENVIRONMENT_LABELS:
        values = context.get_column(variable.value).drop_nulls()
        if values.len() >= 3 and values.n_unique() >= 2:
            available.append(variable)
    return tuple(available)


def _representative_temperatures(column: pl.Series) -> tuple[float, ...]:
    values = sorted(float(value) for value in column.drop_nulls().unique().to_list())
    if len(values) <= 3:
        return tuple(values)
    indexes = (len(values) // 4, len(values) // 2, len(values) * 3 // 4)
    return tuple(values[index] for index in indexes)
