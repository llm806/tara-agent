"""标记独立的群落分析：原样本计算指标后两级汇总，避免混合reads。"""

import math
from collections import defaultdict

import numpy as np
import polars as pl
from scipy.stats import entropy

from tara_agent.analysis.community_models import CommunityQuery, CommunityResult
from tara_agent.analysis.ecology_statistics import adjust_bh, association, envfit, finite, nmds
from tara_agent.analysis.function_pls import fit_pls
from tara_agent.analysis.taxonomy import taxonomy_predicate
from tara_agent.data.research_context import ResearchContext, station_key
from tara_agent.domain.contracts import DataProvenance, ResultMetadata, ResultWarning
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import observe

SIZES = ("0.8-5/2000", "3/5-20", "20-180", "180-2000")
SIZE_MAP = {
    "0.8-5": SIZES[0],
    "0.8->": SIZES[0],
    "0.8-20": SIZES[0],
    "0.8-2000": SIZES[0],
    "3-20": SIZES[1],
    "5-20": SIZES[1],
    **{v: v for v in SIZES},
}
PAPER_VARIABLES = [
    "Temperature",
    "NH4toDIN.5m",
    "Ammonium.5m",
    "NO2.5m",
    "NO3.5m",
    "Iron.5m",
    "Si",
    "PO4",
    "ChlorophyllA",
    "abslat",
]
METRICS = [
    "relative_abundance",
    "taxon_read_count",
    "shannon_index",
    "exp_shannon",
    "observed_asv_richness",
]


def average_rows(rows, keys, fields):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(k) for k in keys)].append(row)
    output = []
    for key, members in groups.items():
        values = {
            k: (
                math.fsum(r[k] for r in members) / len(members)
                if all(finite(r.get(k)) for r in members)
                else None
            )
            for k in fields
        }
        output.append(
            {
                **dict(zip(keys, key, strict=True)),
                **values,
                "sample_ids": sorted({s for r in members for s in r["sample_ids"]}),
            }
        )
    return output


class CommunityService:
    def __init__(self, reader, research=None):
        self.reader = reader
        self.research = research or ResearchContext(reader.processed_dir)

    def analyze(self, query: CommunityQuery):
        with observe(
            "群落结构与环境关系分析",
            ObservationKind.SERVICE,
            ObservationUpdate(input_data=query.model_dump(mode="json")),
        ):
            return self._analyze(query)

    def _analyze(self, query):
        from tara_agent.analysis.compute_models import EnvironmentVariable

        context = self.reader.load_sample_context().to_dicts()
        if query.sample_ids and not set(query.sample_ids) <= {
            r["sample_id_pangaea"] for r in context
        }:
            raise ValueError("存在未知样本编号")
        variables = query.environment_variables or (
            PAPER_VARIABLES
            if query.environment_source == "paper"
            else [
                "temperature",
                "chla",
                "nitrite",
                "phosphate",
                "nitrate_nitrite",
                "silicate",
                "abs_lat",
            ]
        )
        allowed = (
            [*PAPER_VARIABLES, "Salinity", "salinity"]
            if query.environment_source == "paper"
            else [v.value for v in EnvironmentVariable]
        )
        if not set(variables) <= set(allowed):
            raise ValueError("环境变量不在所选来源白名单中")
        environment_error = None
        env = {}
        if query.environment_source == "paper" and set(query.outputs) & {
            "association",
            "ordination",
        }:
            try:
                frame = self.research.frame(
                    "environment", generation=self.reader.manifest.generation
                )
                if not set(variables) <= set(frame.columns):
                    raise ValueError("作者环境表缺少请求变量")
                env = {(station_key(r["station"]), r["depth"]): r for r in frame.to_dicts()}
            except ValueError as error:
                environment_error = str(error)
        (
            observations,
            groups,
            excluded,
            sections,
            correlations,
            models,
            compositions,
            ordinations,
        ) = [], [], [], [], [], [], [], []
        for marker in query.markers:
            candidates = set(self.reader.marker_sample_ids(marker))
            selected = []
            for row in context:
                sid = row["sample_id_pangaea"]
                if query.sample_ids is not None and sid not in query.sample_ids:
                    continue
                if (
                    sid not in candidates
                    or row["depth"] not in query.depths
                    or row["size_fraction"] not in SIZE_MAP
                ):
                    excluded.append(
                        dict(
                            marker=marker.value,
                            sample_id=sid,
                            reason="标记不可用或水层/粒径不在范围",
                        )
                    )
                    continue
                selected.append(row)
            samples = sorted(r["sample_id_pangaea"] for r in selected)

            def section(output, complete, reason=None, _marker=marker):
                sections.append(
                    dict(
                        figure=f"{_marker.value}:{output}",
                        output=output,
                        status="completed" if complete else "blocked",
                        reason=reason,
                    )
                )

            if not samples:
                for output in query.outputs:
                    section(output, False, "筛选范围内无样本")
                continue
            # ASV保留条件在选定原始测序样本内计算；不使用全表total/spread冒充子集统计。
            metadata = self.reader.scan_asv_metadata(marker)
            euk = metadata.filter(taxonomy_predicate("Eukaryota", query.match_mode)).select(
                "amplicon"
            )
            target = metadata.filter(
                taxonomy_predicate(query.taxon, query.match_mode)
                & taxonomy_predicate("Eukaryota", query.match_mode)
            ).select("amplicon")
            counts = self.reader.collect(
                self.reader.scan_abundance(marker, samples).join(target, on="amplicon"),
                name="读取目标群落原始样本矩阵",
                artifacts=[f"{marker.value}_abundance", f"{marker.value}_metadata"],
                filters=query.model_dump(mode="json"),
                marker=marker,
                streaming=True,
            )
            target_matrix = counts.select(samples).to_numpy()
            keep = (target_matrix.sum(axis=1, dtype=np.uint64) >= query.min_total_reads) & (
                (target_matrix > 0).sum(axis=1) >= query.min_sample_occurrence
            )
            target_matrix = target_matrix[keep]
            denominators = {}
            for start in range(0, len(samples), 32):
                batch = samples[start : start + 32]
                totals = self.reader.collect(
                    self.reader.scan_abundance(marker, batch)
                    .join(euk, on="amplicon")
                    .select(pl.col(batch).sum()),
                    name="读取全部真核生物读数分母",
                    artifacts=[f"{marker.value}_abundance", f"{marker.value}_metadata"],
                    marker=marker,
                )
                denominators.update(totals.row(0, named=True))
            by_id = {r["sample_id_pangaea"]: r for r in selected}
            marker_rows = []
            for i, sid in enumerate(samples):
                row = by_id[sid]
                values = target_matrix[:, i]
                total = int(values.sum(dtype=np.uint64))
                shannon = float(entropy(values)) if total > 0 else None
                record = {
                    "marker": marker.value,
                    "sample_id": sid,
                    "sample_ids": [sid],
                    **{
                        k: row[k]
                        for k in (
                            "station",
                            "depth",
                            "size_fraction",
                            "event_latitude",
                            "event_longitude",
                            "ocean_region",
                        )
                    },
                    "taxon_read_count": total,
                    "eukaryotic_read_count": denominators[sid],
                    "relative_abundance": total / denominators[sid] if denominators[sid] else None,
                    "shannon_index": shannon,
                    "exp_shannon": math.exp(shannon) if shannon is not None else None,
                    "observed_asv_richness": int((values > 0).sum()),
                    "retained_asv_count": int(keep.sum()),
                }
                for v in variables:
                    record[v] = (
                        (
                            env.get((station_key(row["station"]), row["depth"]), {}).get(v)
                            if env
                            else None
                        )
                        if query.environment_source == "paper"
                        else row.get(v)
                    )
                if "abslat" in variables:
                    record["abslat"] = (
                        abs(row["event_latitude"]) if finite(row["event_latitude"]) else None
                    )
                marker_rows.append(record)
            observations.extend(marker_rows)
            keys = [
                "marker",
                "station",
                "depth",
                "size_fraction",
                "event_latitude",
                "event_longitude",
                "ocean_region",
            ]
            averaged = average_rows(marker_rows, keys, [*METRICS, *variables])
            for row in averaged:
                row["size_fraction"] = SIZE_MAP[row["size_fraction"]]
            averaged = average_rows(averaged, keys, [*METRICS, *variables])
            for r in averaged:
                r["sampling_group"] = (
                    f"{marker.value}:{r['station']}:{r['depth']}:{r['size_fraction']}"
                )
            groups.extend(averaged)
            for output in query.outputs:
                if output in {"distribution", "diversity"}:
                    section(output, bool(averaged))
            if "association" in query.outputs:
                if environment_error:
                    section("association", False, environment_error)
                else:
                    for response in ("relative_abundance", "shannon_index"):
                        for variable in variables:
                            correlations.append(
                                {
                                    "marker": marker.value,
                                    **association(
                                        averaged,
                                        variable,
                                        response,
                                        permutations=query.permutations,
                                        seed=query.seed,
                                    ),
                                }
                            )
                    model_rows = [
                        {
                            k: r.get(k)
                            for k in [
                                "sampling_group",
                                *variables,
                                "relative_abundance",
                                "shannon_index",
                            ]
                        }
                        for r in averaged
                    ]
                    model = fit_pls(
                        model_rows,
                        variables,
                        ["relative_abundance", "shannon_index"],
                        target=marker.value + "群落",
                        figure="3a",
                    )
                    models.append(model)
                    complete_stats = all(
                        r["status"] == "completed"
                        for r in correlations
                        if r["marker"] == marker.value
                    )
                    section(
                        "association",
                        model.status == "completed" and complete_stats,
                        model.reason or (None if complete_stats else "部分相关变量不可估计"),
                    )
            if "ordination" in query.outputs:
                wide = defaultdict(dict)
                # 多次采样先取均值，避免同一站位、水层、粒径的记录相互覆盖。
                fraction_rows = average_rows(
                    averaged,
                    ["marker", "station", "depth", "size_fraction"],
                    ["relative_abundance", *variables],
                )
                for row in fraction_rows:
                    wide[(row["station"], row["depth"])][row["size_fraction"]] = row
                complete = []
                for key, fractions in wide.items():
                    base = dict(
                        marker=marker.value,
                        station=key[0],
                        depth=key[1],
                        sampling_group=f"{marker.value}:{key[0]}:{key[1]}",
                    )
                    if set(fractions) != set(SIZES) or any(
                        not finite(fractions[s]["relative_abundance"]) for s in SIZES
                    ):
                        excluded.append({**base, "reason": "四粒径不完整，未补零"})
                        continue
                    row = {
                        **base,
                        **{s: fractions[s]["relative_abundance"] for s in SIZES},
                        **{
                            v: math.fsum(fractions[s][v] for s in SIZES) / len(SIZES)
                            if all(finite(fractions[s].get(v)) for s in SIZES)
                            else None
                            for v in variables
                        },
                    }
                    compositions.append(row)
                    if sum(row[s] for s in SIZES) > 0:
                        complete.append(row)
                    else:
                        excluded.append({**base, "reason": "四粒径分布全零，Bray–Curtis未定义"})
                try:
                    if len(complete) > 500:
                        raise ValueError("NMDS超过500组资源保护上限，请明确缩小样本范围")
                    fitted = nmds(
                        [[r[s] for s in SIZES] for r in complete],
                        seed=query.seed,
                        starts=query.nmds_starts,
                        max_iterations=query.nmds_max_iterations,
                    )
                    coords = fitted.pop("coordinates")
                    distances = fitted.pop("distances")
                    fit = (
                        []
                        if environment_error
                        else envfit(
                            coords,
                            complete,
                            variables,
                            permutations=query.permutations,
                            seed=query.seed,
                        )
                    )
                    ordinations.append(
                        {
                            "marker": marker.value,
                            "status": "completed"
                            if fitted["converged"] and not environment_error
                            else "partial",
                            **fitted,
                            "sample_count": len(complete),
                            "environment_reason": environment_error,
                            "scores": [
                                {**r, "NMDS1": float(coords[i, 0]), "NMDS2": float(coords[i, 1])}
                                for i, r in enumerate(complete)
                            ],
                            "environment_fit": fit,
                            "distance_rows": [
                                {
                                    "sampling_group": r["sampling_group"],
                                    **{
                                        other["sampling_group"]: float(distances[i, j])
                                        for j, other in enumerate(complete)
                                    },
                                }
                                for i, r in enumerate(complete)
                            ],
                        }
                    )
                    ok = (
                        fitted["converged"]
                        and not environment_error
                        and all(r["status"] == "completed" for r in fit)
                    )
                    section(
                        "ordination",
                        ok,
                        environment_error
                        or (
                            f"NMDS在{query.nmds_max_iterations}次迭代预算内未收敛"
                            if not fitted["converged"]
                            else "环境拟合有受阻变量" if not ok else None
                        ),
                    )
                except ValueError as error:
                    section("ordination", False, str(error))
        for row, p in zip(
            correlations, adjust_bh([r["p_value"] for r in correlations]), strict=True
        ):
            row["p_adjusted"] = p
        done = sum(s["status"] == "completed" for s in sections)
        self.research.ensure_unchanged()
        return CommunityResult(
            query=query,
            status="completed" if done == len(sections) else "partial" if done else "blocked",
            sections=sections,
            observations=observations,
            groups=groups,
            associations=correlations,
            pls_results=models,
            size_composition=compositions,
            ordinations=ordinations,
            excluded_samples=excluded,
            metadata=ResultMetadata(
                provenance=DataProvenance(
                    source_datasets=[
                        "context_general",
                        "context_stat",
                        *[f"18s_{m.value}" for m in query.markers],
                    ],
                    sample_count=len(observations),
                    excluded_sample_count=len(excluded),
                    filters={
                        "query": query.model_dump(mode="json"),
                        "generation": self.reader.manifest.generation,
                        "research_manifest_sha256": self.research.sha256,
                        "environment_source": query.environment_source,
                        "units": self.research.manifest.get("units", {})
                        if self.research.manifest
                        else {},
                        "aggregation": (
                            "original_sample_metrics_then_exact_fraction_mean"
                            "_then_equivalent_fraction_mean"
                        ),
                        "asv_filter_scope": "selected_original_samples_per_marker",
                        "denominator": "all_eukaryotic_reads_before_target_filter",
                        "zero_target_diversity": "undefined_not_zero",
                        "method_reference": "10.1038/s41467-025-58027-7",
                    },
                ),
                warnings=[
                    ResultWarning(
                        code="method_scope",
                        message="V4/V9独立计算；测序信号不等于细胞数；Shannon未稀释。零目标库多样性未定义。",
                    ),
                    ResultWarning(
                        code="statistical_scope",
                        message=(
                            "相关采用双侧置换检验和BH校正；PLS仅描述。"
                            "站位/水层/粒径观测并非必然独立，未经设计验证不能推断因果。"
                        ),
                    ),
                    ResultWarning(
                        code="ordination_method",
                        message=(
                            "NMDS采用固定种子单调SMACOF、Bray–Curtis和Stress1；"
                            "不声称与vegan metaMDS的随机坐标、自动转换或论文stress完全一致。"
                        ),
                    ),
                ],
            ),
        )
