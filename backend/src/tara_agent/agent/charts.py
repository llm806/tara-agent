"""根据结构化工具结果生成可复现的图表数据。"""

from __future__ import annotations

from typing import Any

from tara_agent.agent.models import ChartKind, ChartSpec, ToolName


def build_charts(tool_name: ToolName, result: dict[str, Any]) -> list[ChartSpec]:
    if tool_name is ToolName.COMMUNITY_ANALYSIS:
        return _community_charts(result)
    if tool_name is ToolName.FUNCTION_ENVIRONMENT:
        points = [p for p in result.get("points", []) if p.get("status") == "observed"]
        return (
            [
                ChartSpec(
                    kind=ChartKind.SCATTER,
                    title=(
                        f"{result['query']['environment_variable']}与"
                        f"{result['query']['pfam_accession']} {result['query']['assay']}信号"
                    ),
                    x=[p["environment_value"] for p in points],
                    y=[p["target_signal"] for p in points],
                    labels=[p["sample_id"] for p in points],
                    x_label=result["query"]["environment_variable"],
                    y_label="目标功能样本内相对信号"
                    if result["query"]["signal"] == "relative"
                    else "已提供目标功能数值和",
                )
            ]
            if points
            else []
        )
    if tool_name is ToolName.FUNCTION_STUDY:
        return _function_study_charts(result)
    if tool_name is ToolName.FIND_SAMPLES:
        return _sample_map(result)
    if tool_name is ToolName.FIND_TAXA:
        return _occurrence_chart(result)
    if tool_name is ToolName.TAXON_ABUNDANCE:
        return _abundance_chart(result)
    if tool_name is ToolName.DIVERSITY_ANALYSIS:
        return _diversity_chart(result)
    if tool_name is ToolName.ENVIRONMENT_ASSOCIATION:
        return _association_chart(result)
    if tool_name is ToolName.FUNCTION_PROFILE:
        items = [
            i
            for i in result.get("observations", [])
            if i.get("fraction_of_observed_taxon_signal") is not None
        ][:20]
        if not items:
            return []
        return [
            ChartSpec(
                kind=ChartKind.BAR,
                title="候选 Pfam 信号份额（功能可共享基因）",
                x=[i["pfam_accession"] for i in items],
                y=[i["fraction_of_observed_taxon_signal"] for i in items],
                x_label="Pfam",
                y_label="已提供类群记录内份额",
            )
        ]
    if tool_name is ToolName.FUNCTION_ATLAS:
        items = result.get("ranks", [])[:20]
        return (
            [
                ChartSpec(
                    kind=ChartKind.BAR,
                    title="候选 Pfam 总体已观测贡献排名",
                    x=[i["pfam_accession"] for i in items],
                    y=[i["mean_observed_contribution"] for i in items],
                    x_label="Pfam",
                    y_label="原始样本等权平均已观测贡献",
                )
            ]
            if items
            else []
        )
    if tool_name is ToolName.COMPARE_FUNCTION_SIGNALS:
        charts = []
        for summary in result.get("summaries", []):
            family = summary["pfam_accession"]
            points = [
                p
                for p in result.get("points", [])
                if p["pfam_accession"] == family
                and p["metag_fraction"] is not None
                and p["metat_fraction"] is not None
            ]
            if points:
                charts.append(
                    ChartSpec(
                        kind=ChartKind.SCATTER,
                        title=f"{family} 采样编码对应的相对功能信号",
                        x=[p["metag_fraction"] for p in points],
                        y=[p["metat_fraction"] for p in points],
                        labels=[p["sampling_key"] for p in points],
                        x_label="MetaG 样本内份额",
                        y_label="MetaT 样本内份额",
                    )
                )
        return charts
    return []


def _function_study_charts(result):
    ranks = result.get("function_ranks", [])
    if not ranks:
        return []
    source = "作者参考重算" if result["query"]["source"] == "paper_reference" else "当前数据"
    charts = [
        ChartSpec(
            kind=ChartKind.HORIZONTAL_BAR,
            title=f"图9a对照 · {source} · Top {len(ranks)} 功能相对转录贡献",
            x=[r["function_name"] for r in ranks],
            y=[r["relative_contribution"] for r in ranks],
            labels=[r["function_id"] for r in ranks],
            x_label="全部硅藻基因信号中的平均比例"
            if result["query"].get("normalization") == "taxon_total"
            else "保留功能总相对信号中的比例",
            y_label="功能（按排名）",
        )
    ]
    for key, figure, label in (
        ("size_distribution", "9b", "粒径"),
        ("ocean_distribution", "9c", "海区"),
    ):
        rows = result.get(key, [])
        if not any(r["allocation_fraction"] is not None for r in rows):
            continue
        groups = list(dict.fromkeys(r["group"] for r in rows))
        indexed = {(r["function_id"], r["group"]): r["allocation_fraction"] for r in rows}
        charts.append(
            ChartSpec(
                kind=ChartKind.STACKED_BAR,
                title=f"图{figure}对照 · {source} · Top {len(ranks)} 功能{label}分配",
                x=[r["function_name"] for r in ranks],
                y=[],
                labels=[],
                x_label="已映射信号内分配比例",
                y_label="功能（同图9a顺序）",
                series=[
                    {
                        "name": group,
                        "values": [indexed.get((r["function_id"], group)) for r in ranks],
                    }
                    for group in groups
                ],
            )
        )
    for p in result.get("pls_results", []):
        if p["status"] != "completed":
            continue
        coords = p["coordinates"]
        charts.append(
            ChartSpec(
                kind=ChartKind.CORRELATION_CIRCLE,
                title=(
                    f"图{p['figure']}对照 · {source} · {p['target']} "
                    f"环境PLS相关圆（{p['sample_count']}组）"
                ),
                x=[r["component_1"] for r in coords],
                y=[r["component_2"] for r in coords],
                labels=[r["variable"] for r in coords],
                x_label="成分1相关",
                y_label="成分2相关",
                series=[
                    {
                        "name": role,
                        "indices": [i for i, r in enumerate(coords) if r["role"] == role],
                    }
                    for role in ("environment", "response")
                ],
            )
        )
    return charts


def _community_charts(result):
    charts = []
    query = result["query"]
    outputs = query["outputs"]
    for marker in query["markers"]:
        groups = [r for r in result["groups"] if r["marker"] == marker]
        for fraction in dict.fromkeys(r["size_fraction"] for r in groups):
            rows = [r for r in groups if r["size_fraction"] == fraction]
            surface = [
                r
                for r in rows
                if r["depth"] == "SRF"
                and r.get("relative_abundance") is not None
                and r.get("event_latitude") is not None
                and r.get("event_longitude") is not None
            ]
            if "distribution" in outputs and surface:
                charts.append(
                    ChartSpec(
                        kind=ChartKind.SAMPLE_MAP,
                        title=f"图2a/S6对照 · {marker.upper()} · {fraction} μm表层群落相对丰度",
                        x=[r["event_longitude"] for r in surface],
                        y=[r["event_latitude"] for r in surface],
                        labels=[
                            f"{r['station']} SRF；相对丰度={r['relative_abundance']:.5g}；"
                            f"exp(Shannon)={r['exp_shannon']}"
                            for r in surface
                        ],
                        x_label="经度",
                        y_label="纬度",
                        series=[
                            {
                                "name": "abundance",
                                "values": [r["relative_abundance"] for r in surface],
                            },
                            {
                                "name": "diversity",
                                "values": [r["exp_shannon"] for r in surface],
                            },
                        ],
                    )
                )
            for depth in dict.fromkeys(r["depth"] for r in rows):
                for field, label, output in (
                    ("relative_abundance", "群落相对丰度", "distribution"),
                    ("exp_shannon", "exp(Shannon)有效ASV数", "diversity"),
                ):
                    points = [
                        r
                        for r in rows
                        if r["depth"] == depth
                        and r.get(field) is not None
                        and r.get("event_latitude") is not None
                    ]
                    if output in outputs and points:
                        charts.append(
                            ChartSpec(
                                kind=ChartKind.SCATTER,
                                title=(
                                    f"图2b/c与S6对照 · {marker.upper()} · {depth} · "
                                    f"{fraction} μm · {label}随纬度变化"
                                ),
                                x=[r["event_latitude"] for r in points],
                                y=[r[field] for r in points],
                                labels=[r["sampling_group"] for r in points],
                                x_label="纬度（°）",
                                y_label=label,
                            )
                        )
        correlations = [r for r in result["associations"] if r["marker"] == marker]
        if correlations:
            variables = list(dict.fromkeys(r["variable"] for r in correlations))
            responses = list(dict.fromkeys(r["response"] for r in correlations))
            indexed = {(r["variable"], r["response"]): r for r in correlations}
            charts.append(
                ChartSpec(
                    kind=ChartKind.HEATMAP,
                    title=f"图3a对照 · {marker.upper()} · 群落丰度和Shannon与环境Spearman关联",
                    x=responses,
                    y=[],
                    labels=variables,
                    x_label="响应",
                    y_label="环境变量",
                    series=[
                        {
                            "name": v,
                            "values": [indexed[v, y]["rho"] for y in responses],
                            "text": [
                                f"n={indexed[v, y]['sample_count']}；"
                                f"p={indexed[v, y]['p_value']}；BH q={indexed[v, y]['p_adjusted']}"
                                for y in responses
                            ],
                        }
                        for v in variables
                    ],
                )
            )
    for model in result["pls_results"]:
        if model["status"] == "completed":
            rows = model["coordinates"]
            charts.append(
                ChartSpec(
                    kind=ChartKind.CORRELATION_CIRCLE,
                    title=f"图3a对照 · {model['target']} · 环境PLS（{model['sample_count']}组）",
                    x=[r["component_1"] for r in rows],
                    y=[r["component_2"] for r in rows],
                    labels=[r["variable"] for r in rows],
                    x_label="成分1相关",
                    y_label="成分2相关",
                    series=[
                        {
                            "name": role,
                            "indices": [i for i, r in enumerate(rows) if r["role"] == role],
                        }
                        for role in ("environment", "response")
                    ],
                )
            )
    for model in result["ordinations"]:
        if not model["converged"]:
            continue
        rows = model["scores"]
        vectors = [
            r
            for r in model["environment_fit"]
            if r["status"] == "completed" and r["p_adjusted"] < 0.05
        ]
        charts.append(
            ChartSpec(
                kind=ChartKind.ORDINATION,
                title=(
                    f"图3b对照 · {model['marker'].upper()} · "
                    f"四粒径NMDS（Stress1={model['stress']:.4g}）"
                ),
                x=[r["NMDS1"] for r in rows],
                y=[r["NMDS2"] for r in rows],
                labels=[r["sampling_group"] for r in rows],
                x_label="NMDS1",
                y_label="NMDS2",
                series=[
                    {"name": r["variable"], "x": [0, r["axis_1"]], "y": [0, r["axis_2"]]}
                    for r in vectors
                ],
            )
        )
    return charts


def _sample_map(result: dict[str, Any]) -> list[ChartSpec]:
    items = result.get("items", [])
    if not items:
        return []
    return [
        ChartSpec(
            kind=ChartKind.SAMPLE_MAP,
            title="样本分布",
            x=[float(item["event_longitude"]) for item in items],
            y=[float(item["event_latitude"]) for item in items],
            labels=[str(item["sample_id_pangaea"]) for item in items],
            x_label="经度",
            y_label="纬度",
        )
    ]


def _occurrence_chart(result: dict[str, Any]) -> list[ChartSpec]:
    items = result.get("sample_occurrences", [])[:20]
    return _bar_chart(
        items,
        value_key="read_count",
        title="分类群测序 reads 排名",
        y_label="Raw read count",
    )


def _abundance_chart(result: dict[str, Any]) -> list[ChartSpec]:
    if result.get("group_by") == "station":
        charts = []
        for key, label in (
            ("mean_relative_abundance", "均值"),
            ("max_relative_abundance", "最大值"),
        ):
            items = sorted(
                [row for row in result.get("groups", []) if row.get(key) is not None],
                key=lambda row: (-row[key], row["station"]),
            )[:20]
            if items:
                charts.append(
                    ChartSpec(
                        kind=ChartKind.BAR,
                        title=f"本次返回站点的相对丰度{label}",
                        x=[row["station"] for row in items],
                        y=[row[key] for row in items],
                        x_label="站点",
                        y_label=f"样本相对丰度{label}",
                    )
                )
        return charts
    if result.get("taxonomic_rank"):
        return []
    observations = result.get("observations", [])
    items = sorted(
        observations,
        key=lambda item: item.get("relative_abundance") or 0,
        reverse=True,
    )[:20]
    return _bar_chart(
        items,
        value_key="relative_abundance",
        title="本次返回样本的分类群相对丰度",
        y_label="Relative abundance",
    )


def _diversity_chart(result: dict[str, Any]) -> list[ChartSpec]:
    items = [
        item for item in result.get("observations", []) if item.get("shannon_index") is not None
    ][:20]
    return _bar_chart(
        items,
        value_key="shannon_index",
        title="样本 Shannon 多样性",
        y_label="Shannon index",
    )


def _association_chart(result: dict[str, Any]) -> list[ChartSpec]:
    points = result.get("points", [])
    if not points:
        return []
    variable = str(result.get("environment_variable", "environment"))
    return [
        ChartSpec(
            kind=ChartKind.SCATTER,
            title=f"{variable} 与相对丰度",
            x=[float(point["environment_value"]) for point in points],
            y=[float(point["relative_abundance"]) for point in points],
            labels=[str(point["sample_id"]) for point in points],
            x_label=variable,
            y_label="Relative abundance",
        )
    ]


def _bar_chart(
    items: list[dict[str, Any]],
    *,
    value_key: str,
    title: str,
    y_label: str,
) -> list[ChartSpec]:
    plotted = [item for item in items if item.get(value_key) is not None]
    if not plotted:
        return []
    return [
        ChartSpec(
            kind=ChartKind.BAR,
            title=title,
            x=[str(item["sample_id"]) for item in plotted],
            y=[float(item[value_key]) for item in plotted],
            x_label="Sample ID",
            y_label=y_label,
        )
    ]
