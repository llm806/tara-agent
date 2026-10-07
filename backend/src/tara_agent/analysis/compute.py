"""Tara 数据的丰度、多样性和环境关联计算。"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import polars as pl
from scipy.stats import entropy, spearmanr

from tara_agent.analysis.abundance_details import (
    TAXONOMY_LAYOUT,
    sample_composition,
    station_abundance,
)
from tara_agent.analysis.compute_models import (
    DiversityGroupSummary,
    DiversityObservation,
    DiversityQuery,
    DiversityResult,
    EnvironmentAssociationPoint,
    EnvironmentAssociationQuery,
    EnvironmentAssociationResult,
    TaxonAbundanceObservation,
    TaxonAbundanceQuery,
    TaxonAbundanceResult,
    TaxonSelection,
)
from tara_agent.analysis.models import Page
from tara_agent.analysis.taxonomy import taxonomy_predicate
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.domain.contracts import DataProvenance, Marker, ResultMetadata, ResultWarning

COUNT_COLUMNS_PER_BATCH = 32
MIN_CORRELATION_SAMPLES = 3
ASYMPTOTIC_P_VALUE_SAMPLE_THRESHOLD = 500


@dataclass(frozen=True, slots=True)
class _SampleSelection:
    selected: list[str]
    unavailable_for_marker: list[str]
    marker_sample_count: int


@dataclass(frozen=True, slots=True)
class _AbundanceCalculation:
    observations: list[TaxonAbundanceObservation]
    matching_asv_count: int
    zero_library_samples: list[str]


class TaraComputeService:
    """基于已校验的标记丰度矩阵执行限定范围的科学计算。"""

    def __init__(self, reader: ProcessedDataReader) -> None:
        self.reader = reader

    def taxon_abundance(self, query: TaxonAbundanceQuery) -> TaxonAbundanceResult:
        selection = self._resolve_samples(query.marker, query.sample_ids)
        calculation = self._calculate_taxon_abundance(query, selection.selected)
        observations = calculation.observations
        if not query.include_zero_samples:
            observations = [item for item in observations if item.taxon_read_count > 0]
        if query.order_by == "relative_abundance":
            observations = sorted(
                observations,
                key=lambda o: (
                    o.relative_abundance is None,
                    -(o.relative_abundance or 0),
                    o.sample_id,
                ),
            )
        groups, missing_station = (
            station_abundance(self.reader, observations, query.aggregation)
            if query.group_by
            else ([], [])
        )
        composition, composition_summaries = (
            sample_composition(self.reader, query, observations)
            if query.taxonomic_rank
            else ([], [])
        )

        total = len(observations)
        page_items = observations[query.offset : query.offset + query.limit]
        warnings = self._abundance_warnings(
            calculation.matching_asv_count,
            selection.unavailable_for_marker,
            calculation.zero_library_samples,
        )
        if query.group_by:
            warnings.append(
                ResultWarning(
                    code="station_descriptive_aggregation",
                    message="站点均值为有效样本相对丰度的等权平均，最大值为最高样本值；不同水层和粒径合并仅作描述，不是独立重复或站点绝对丰度。",
                )
            )
        if missing_station:
            warnings.append(
                ResultWarning(
                    code="missing_station_excluded",
                    message="缺少站点标识的样本未参与站点汇总。",
                    details={"sample_ids": missing_station},
                )
            )
        if query.taxonomic_rank:
            warnings.append(
                ResultWarning(
                    code="taxonomy_assignment_limit",
                    message="属/种名称来自原始分类注释；截短、占位或不支持的层级保留为未鉴定。未按置信度剔除；测序信号不证明种鉴定或细胞丰度。组成分别给出全样本占比和所选类群内部占比。",
                    details={
                        "taxonomy_layout": TAXONOMY_LAYOUT,
                        "pagination": "offset/limit independently per sample",
                    },
                )
            )
        excluded = selection.marker_sample_count - len(selection.selected)
        excluded += len(calculation.observations) - len(observations)

        return TaxonAbundanceResult(
            marker=query.marker,
            taxon=query.taxon,
            match_mode=query.match_mode,
            observations=page_items,
            page=Page(offset=query.offset, limit=query.limit, total=total),
            matching_asv_count=calculation.matching_asv_count,
            group_by=query.group_by,
            aggregation=query.aggregation if query.group_by else None,
            groups=groups[query.offset : query.offset + query.limit],
            station_leaders=self._station_leaders(groups),
            group_page=Page(offset=query.offset, limit=query.limit, total=len(groups))
            if query.group_by
            else None,
            taxonomic_rank=query.taxonomic_rank,
            composition=composition,
            composition_summaries=composition_summaries,
            metadata=ResultMetadata(
                provenance=DataProvenance(
                    source_datasets=[
                        f"18s_{query.marker.value}",
                        *(["context_general"] if query.group_by else []),
                    ],
                    marker=query.marker,
                    sample_count=total,
                    excluded_sample_count=excluded,
                    filters={
                        "taxon": query.taxon,
                        "match_mode": query.match_mode.value,
                        "include_zero_samples": query.include_zero_samples,
                        "sample_ids": query.sample_ids,
                        "order_by": query.order_by,
                        "group_by": query.group_by,
                        "aggregation": query.aggregation if query.group_by else None,
                        "taxonomic_rank": query.taxonomic_rank,
                        "offset": query.offset,
                        "limit": query.limit,
                    },
                ),
                warnings=warnings,
            ),
        )

    @staticmethod
    def _station_leaders(groups):
        # 先在完整范围取两种统计的最高站点，避免分页或模型摘要遗漏另一套排名的首位。
        leaders = {}
        for statistic in ("mean", "max"):
            first = next(
                (
                    row
                    for row in sorted(groups, key=lambda row: row.station)
                    if getattr(row, f"{statistic}_rank") == 1
                ),
                None,
            )
            if first is not None:
                leaders[first.station] = first
        return list(leaders.values())

    def diversity_analysis(self, query: DiversityQuery) -> DiversityResult:
        selection = self._resolve_samples(query.marker, query.sample_ids)
        amplicons, matching_asv_count = self._diversity_amplicons(query)
        observations = self._calculate_diversity(query.marker, selection.selected, amplicons)
        groups, group_warnings = self._summarize_diversity_groups(
            observations, query.group_by.value if query.group_by else None
        )
        zero_read_samples = [
            item.sample_id for item in observations if item.analysis_read_count == 0
        ]
        warnings = [
            ResultWarning(
                code="unrarefied_diversity",
                message=(
                    "观测丰富度和 Shannon 指数基于未经稀释抽样的测序读数计算，"
                    "测序量差异可能影响样本间比较。"
                ),
            )
        ]
        warnings.extend(self._unavailable_sample_warnings(selection.unavailable_for_marker))
        warnings.extend(group_warnings)
        if query.taxon is not None and matching_asv_count == 0:
            warnings.append(
                ResultWarning(
                    code="taxon_not_found",
                    message="没有 ASV 匹配请求的分类单元。",
                )
            )
        if zero_read_samples:
            warnings.append(
                ResultWarning(
                    code="zero_reads_in_analysis_set",
                    message=("所选 ASV 集合的测序读数为零时，Shannon 指数没有定义。"),
                    details={"sample_ids": zero_read_samples},
                )
            )

        total = len(observations)
        page_items = observations[query.offset : query.offset + query.limit]
        return DiversityResult(
            marker=query.marker,
            taxon=query.taxon,
            match_mode=query.match_mode,
            matching_asv_count=matching_asv_count,
            observations=page_items,
            page=Page(offset=query.offset, limit=query.limit, total=total),
            group_by=query.group_by,
            groups=groups,
            metadata=ResultMetadata(
                provenance=DataProvenance(
                    source_datasets=[f"18s_{query.marker.value}"],
                    marker=query.marker,
                    sample_count=total,
                    excluded_sample_count=selection.marker_sample_count - total,
                    filters={
                        "taxon": query.taxon,
                        "match_mode": query.match_mode.value,
                        "group_by": query.group_by.value if query.group_by else None,
                        "rarefied": False,
                    },
                ),
                warnings=warnings,
            ),
        )

    def environment_association(
        self, query: EnvironmentAssociationQuery
    ) -> EnvironmentAssociationResult:
        selection = self._resolve_samples(query.marker, query.sample_ids)
        calculation = self._calculate_taxon_abundance(query, selection.selected)
        points, missing_environment = self._association_points(query, calculation.observations)
        rho, p_value, statistical_warnings = self._spearman(points)

        warnings = self._abundance_warnings(
            calculation.matching_asv_count,
            selection.unavailable_for_marker,
            calculation.zero_library_samples,
        )
        if missing_environment:
            warnings.append(
                ResultWarning(
                    code="missing_environment_values_excluded",
                    message="环境变量缺失的样本未参与成对相关性计算。",
                    details={
                        "field": query.environment_variable.value,
                        "sample_count": missing_environment,
                    },
                )
            )
        warnings.extend(statistical_warnings)

        total = len(points)
        point_items = points[query.point_offset : query.point_offset + query.point_limit]
        return EnvironmentAssociationResult(
            marker=query.marker,
            taxon=query.taxon,
            match_mode=query.match_mode,
            environment_variable=query.environment_variable,
            sample_count=total,
            rho=rho,
            p_value=p_value,
            points=point_items,
            point_page=Page(
                offset=query.point_offset,
                limit=query.point_limit,
                total=total,
            ),
            metadata=ResultMetadata(
                provenance=DataProvenance(
                    source_datasets=[
                        f"18s_{query.marker.value}",
                        "context_general",
                        "context_stat",
                    ],
                    marker=query.marker,
                    sample_count=total,
                    excluded_sample_count=selection.marker_sample_count - total,
                    filters={
                        "taxon": query.taxon,
                        "match_mode": query.match_mode.value,
                        "environment_variable": query.environment_variable.value,
                        "missing_values": "pairwise_complete",
                        "multiple_testing_correction": "not_applied_single_test",
                    },
                ),
                warnings=warnings,
            ),
        )

    def _resolve_samples(self, marker: Marker, requested: list[str] | None) -> _SampleSelection:
        marker_samples = set(self.reader.marker_sample_ids(marker))
        if requested is None:
            return _SampleSelection(sorted(marker_samples), [], len(marker_samples))

        context_samples = set(self.reader.context_sample_ids())
        unknown = sorted(set(requested) - context_samples)
        if unknown:
            raise ValueError(f"Unknown sample IDs: {unknown[:5]}")

        selected = sorted(set(requested) & marker_samples)
        unavailable = sorted(set(requested) - marker_samples)
        return _SampleSelection(selected, unavailable, len(marker_samples))

    def _calculate_taxon_abundance(
        self, query: TaxonSelection, samples: list[str]
    ) -> _AbundanceCalculation:
        matching_query = (
            self.reader.scan_asv_metadata(query.marker)
            .filter(taxonomy_predicate(query.taxon, query.match_mode))
            .select("amplicon")
        )
        matching_ids = self.reader.collect(
            matching_query,
            name="读取匹配的分类单元标识",
            artifacts=[f"{query.marker.value}_metadata"],
            filters={
                "taxon": query.taxon,
                "match_mode": query.match_mode.value,
            },
            marker=query.marker,
            streaming=True,
        ).get_column("amplicon")
        observations: list[TaxonAbundanceObservation] = []
        zero_libraries: list[str] = []

        for batch in self._sample_batches(samples):
            abundance = self.reader.collect(
                self.reader.scan_abundance(query.marker, batch),
                name="读取样本丰度矩阵",
                artifacts=[f"{query.marker.value}_abundance"],
                filters={"sample_ids": batch},
                marker=query.marker,
                streaming=True,
            )
            library_totals = self._column_sums(abundance, batch)
            if matching_ids.is_empty():
                taxon_totals = dict.fromkeys(batch, 0)
            else:
                matching = abundance.filter(pl.col("amplicon").is_in(matching_ids.implode()))
                taxon_totals = self._column_sums(matching, batch)

            for sample in batch:
                library_total = library_totals[sample]
                taxon_total = taxon_totals[sample]
                relative = None
                if library_total > 0:
                    relative = taxon_total / library_total
                else:
                    zero_libraries.append(sample)
                observations.append(
                    TaxonAbundanceObservation(
                        sample_id=sample,
                        taxon_read_count=taxon_total,
                        sample_total_read_count=library_total,
                        relative_abundance=relative,
                    )
                )

        observations.sort(key=lambda item: item.sample_id)
        return _AbundanceCalculation(
            observations=observations,
            matching_asv_count=len(matching_ids),
            zero_library_samples=zero_libraries,
        )

    def _diversity_amplicons(self, query: DiversityQuery) -> tuple[pl.Series | None, int]:
        if query.taxon is None:
            count = self.reader.manifest.artifacts[f"{query.marker.value}_metadata"].row_count
            return None, count

        amplicon_query = (
            self.reader.scan_asv_metadata(query.marker)
            .filter(taxonomy_predicate(query.taxon, query.match_mode))
            .select("amplicon")
        )
        amplicons = self.reader.collect(
            amplicon_query,
            name="读取多样性分析分类单元",
            artifacts=[f"{query.marker.value}_metadata"],
            filters={
                "taxon": query.taxon,
                "match_mode": query.match_mode.value,
            },
            marker=query.marker,
            streaming=True,
        ).get_column("amplicon")
        return amplicons, len(amplicons)

    def _calculate_diversity(
        self,
        marker: Marker,
        samples: list[str],
        amplicons: pl.Series | None,
    ) -> list[DiversityObservation]:
        observations: list[DiversityObservation] = []
        for batch in self._sample_batches(samples):
            abundance = self.reader.collect(
                self.reader.scan_abundance(marker, batch),
                name="读取多样性分析丰度矩阵",
                artifacts=[f"{marker.value}_abundance"],
                filters={"sample_ids": batch},
                marker=marker,
                streaming=True,
            )
            if amplicons is not None:
                abundance = abundance.filter(pl.col("amplicon").is_in(amplicons.implode()))
            counts = abundance.select(batch).to_numpy()
            total_reads = counts.sum(axis=0, dtype=np.uint64)
            observed = (counts > 0).sum(axis=0)
            if abundance.is_empty():
                shannon = np.full(len(batch), np.nan)
            else:
                shannon = entropy(counts, axis=0)

            for index, sample in enumerate(batch):
                total = int(total_reads[index])
                value = float(shannon[index]) if total > 0 else None
                observations.append(
                    DiversityObservation(
                        sample_id=sample,
                        analysis_read_count=total,
                        observed_asv_richness=int(observed[index]),
                        shannon_index=value,
                    )
                )
        observations.sort(key=lambda item: item.sample_id)
        return observations

    def _summarize_diversity_groups(
        self,
        observations: list[DiversityObservation],
        group_field: str | None,
    ) -> tuple[list[DiversityGroupSummary], list[ResultWarning]]:
        if group_field is None or not observations:
            return [], []

        sample_ids = [item.sample_id for item in observations]
        context = self.reader.load_sample_context(sample_ids).select(
            "sample_id_pangaea", group_field
        )
        group_by_sample = {row["sample_id_pangaea"]: row[group_field] for row in context.to_dicts()}
        grouped: dict[str, list[DiversityObservation]] = defaultdict(list)
        missing = 0
        for observation in observations:
            group = group_by_sample.get(observation.sample_id)
            if group is None:
                missing += 1
                continue
            grouped[str(group)].append(observation)

        summaries = [
            self._diversity_group_summary(group, values)
            for group, values in sorted(grouped.items())
        ]
        warnings: list[ResultWarning] = []
        if missing:
            warnings.append(
                ResultWarning(
                    code="missing_group_values_excluded",
                    message="分组变量缺失的样本未纳入分组汇总。",
                    details={"field": group_field, "sample_count": missing},
                )
            )
        return summaries, warnings

    @staticmethod
    def _diversity_group_summary(
        group: str, observations: list[DiversityObservation]
    ) -> DiversityGroupSummary:
        richness = [item.observed_asv_richness for item in observations]
        shannon = [item.shannon_index for item in observations if item.shannon_index is not None]
        return DiversityGroupSummary(
            group=group,
            sample_count=len(observations),
            shannon_sample_count=len(shannon),
            richness_mean=statistics.fmean(richness),
            richness_median=statistics.median(richness),
            shannon_mean=statistics.fmean(shannon) if shannon else None,
            shannon_median=statistics.median(shannon) if shannon else None,
        )

    def _association_points(
        self,
        query: EnvironmentAssociationQuery,
        observations: list[TaxonAbundanceObservation],
    ) -> tuple[list[EnvironmentAssociationPoint], int]:
        if not observations:
            return [], 0
        sample_ids = [item.sample_id for item in observations]
        field = query.environment_variable.value
        context = self.reader.load_sample_context(sample_ids).select("sample_id_pangaea", field)
        environment = {row["sample_id_pangaea"]: row[field] for row in context.to_dicts()}

        points: list[EnvironmentAssociationPoint] = []
        missing = 0
        for observation in observations:
            value = environment.get(observation.sample_id)
            if value is None:
                missing += 1
                continue
            if observation.relative_abundance is None:
                continue
            points.append(
                EnvironmentAssociationPoint(
                    sample_id=observation.sample_id,
                    environment_value=float(value),
                    relative_abundance=observation.relative_abundance,
                )
            )
        points.sort(key=lambda item: item.sample_id)
        return points, missing

    @staticmethod
    def _spearman(
        points: list[EnvironmentAssociationPoint],
    ) -> tuple[float | None, float | None, list[ResultWarning]]:
        sample_count = len(points)
        if sample_count < MIN_CORRELATION_SAMPLES:
            return (
                None,
                None,
                [
                    ResultWarning(
                        code="insufficient_samples",
                        message="Spearman 相关性计算至少需要三个数据完整的样本。",
                        details={"sample_count": sample_count},
                    )
                ],
            )

        environment = [item.environment_value for item in points]
        abundance = [item.relative_abundance for item in points]
        if len(set(environment)) < 2 or len(set(abundance)) < 2:
            return (
                None,
                None,
                [
                    ResultWarning(
                        code="constant_input",
                        message="输入值全部相同时，Spearman 相关系数没有定义。",
                    )
                ],
            )

        result = spearmanr(environment, abundance, nan_policy="raise", alternative="two-sided")
        rho = float(result.statistic)
        p_value = float(result.pvalue)
        if not math.isfinite(rho) or not math.isfinite(p_value):
            return (
                None,
                None,
                [
                    ResultWarning(
                        code="undefined_correlation",
                        message="Spearman 相关性计算未得到有限数值结果。",
                    )
                ],
            )

        warnings: list[ResultWarning] = []
        if sample_count <= ASYMPTOTIC_P_VALUE_SAMPLE_THRESHOLD:
            warnings.append(
                ResultWarning(
                    code="asymptotic_p_value_caution",
                    message=(
                        "SciPy 文档说明，Spearman 渐近 p 值在样本量大于 500 时最准确；"
                        "当前探索性 p 值需要谨慎解释。"
                    ),
                    details={"sample_count": sample_count},
                )
            )
        return rho, p_value, warnings

    @staticmethod
    def _sample_batches(samples: list[str]):
        for start in range(0, len(samples), COUNT_COLUMNS_PER_BATCH):
            yield samples[start : start + COUNT_COLUMNS_PER_BATCH]

    @staticmethod
    def _column_sums(frame: pl.DataFrame, columns: list[str]) -> dict[str, int]:
        if frame.is_empty():
            return dict.fromkeys(columns, 0)
        values = frame.select([pl.col(column).cast(pl.UInt64).sum() for column in columns]).row(
            0, named=True
        )
        return {column: int(value or 0) for column, value in values.items()}

    def _abundance_warnings(
        self,
        matching_asv_count: int,
        unavailable_samples: list[str],
        zero_library_samples: list[str],
    ) -> list[ResultWarning]:
        warnings = [
            ResultWarning(
                code="read_count_is_not_cell_abundance",
                message=(
                    "原始测序读数及据此计算的相对丰度属于测序信号，不是对细胞丰度的直接测量。"
                ),
            )
        ]
        if matching_asv_count == 0:
            warnings.append(
                ResultWarning(
                    code="taxon_not_found",
                    message="没有 ASV 匹配请求的分类单元。",
                )
            )
        warnings.extend(self._unavailable_sample_warnings(unavailable_samples))
        if zero_library_samples:
            warnings.append(
                ResultWarning(
                    code="zero_read_library",
                    message="样本总测序读数为零时，相对丰度没有定义。",
                    details={"sample_ids": zero_library_samples},
                )
            )
        return warnings

    @staticmethod
    def _unavailable_sample_warnings(samples: list[str]) -> list[ResultWarning]:
        if not samples:
            return []
        return [
            ResultWarning(
                code="samples_missing_marker_excluded",
                message="不含所请求标记数据的背景样本未参与本次分析。",
                details={"sample_ids": samples, "sample_count": len(samples)},
            )
        ]
