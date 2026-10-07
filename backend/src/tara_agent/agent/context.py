"""构造回答模型所需的紧凑上下文。"""

from __future__ import annotations

from typing import Any

from tara_agent.agent.models import ToolName

DETAIL_FIELDS_BY_TOOL: dict[ToolName, frozenset[str]] = {
    ToolName.COMMUNITY_ANALYSIS: frozenset(
        {
            "observations",
            "groups",
            "size_composition",
            "ordinations",
            "excluded_samples",
            "pls_results",
        }
    ),
    ToolName.FUNCTION_ENVIRONMENT: frozenset({"points", "excluded_samples"}),
    ToolName.FIND_SAMPLES: frozenset({"items"}),
    ToolName.FIND_TAXA: frozenset({"asvs", "sample_occurrences"}),
    ToolName.TAXON_ABUNDANCE: frozenset({"observations", "composition"}),
    ToolName.DIVERSITY_ANALYSIS: frozenset({"observations"}),
    ToolName.ENVIRONMENT_ASSOCIATION: frozenset({"points"}),
    ToolName.FIND_FUNCTION_SAMPLES: frozenset({"items"}),
    ToolName.FUNCTION_ATLAS: frozenset({"observations", "sample_summaries"}),
    ToolName.FUNCTION_STUDY: frozenset(
        {
            "size_distribution",
            "ocean_distribution",
            "target_signals",
            "excluded_samples",
            "pls_results",
        }
    ),
    ToolName.COMPARE_FUNCTION_SIGNALS: frozenset({"points", "excluded_samples"}),
    ToolName.RETRIEVE_GENE_SEQUENCES: frozenset({"sequences"}),
}
PAGINATION_FIELDS = frozenset({"page", "asv_page", "sample_page", "point_page", "group_page"})


def build_answer_model_input(
    question: str,
    tool_name: ToolName,
    result: dict[str, Any],
) -> dict[str, Any]:
    """返回回答模型与 Trace 共同使用的实际用户输入。"""

    return {
        "question": question,
        "tool_name": tool_name.value,
        "tool_result": build_result_summary(tool_name, result),
    }


def build_result_summary(
    tool_name: ToolName,
    result: dict[str, Any],
) -> dict[str, Any]:
    """移除由界面展示的明细，仅保留回答所需的摘要。"""

    detail_fields = DETAIL_FIELDS_BY_TOOL.get(tool_name, frozenset())
    summary = {
        str(key): (
            {field: value.get(field) for field in ("total", "offset", "limit") if field in value}
            if key in PAGINATION_FIELDS and isinstance(value, dict)
            else _compact_value(value)
        )
        for key, value in result.items()
        if key not in detail_fields
    }
    if tool_name in (
        ToolName.FIND_SAMPLES,
        ToolName.FIND_FUNCTION_SAMPLES,
        ToolName.TAXON_ABUNDANCE,
    ):
        field = "observations" if tool_name is ToolName.TAXON_ABUNDANCE else "items"
        rows = result.get(field, [])
        # 保留真实编号及返回顺序，供“第一个样本”“这几个样本”等追问引用。
        identity_fields = (
            "sample_id_pangaea",
            "sample_id",
            "sample_name",
            "station",
            "ocean_region",
            "depth",
            "size_fraction",
            "relative_abundance",
            "observed_gene_records",
        )
        summary["returned_sample_records"] = [
            {key: row[key] for key in identity_fields if key in row} for row in rows[:20]
        ]
        summary["returned_sample_scope"] = {
            "returned_count": len(rows),
            "retained_count": min(len(rows), 20),
            "truncated": len(rows) > 20,
            "note": "仅为当前返回页；不能当作全部样本，省略记录不得猜测编号。",
        }
    if tool_name is ToolName.FIND_TAXA:
        # 分类归属必须给模型真实注释证据；序列和完整明细仍留在界面。
        summary["returned_taxonomy_annotations"] = [
            {key: row.get(key) for key in ("amplicon", "taxonomy", "confidence")}
            for row in result.get("asvs", [])[:20]
        ]
        summary["taxonomy_annotation_scope"] = "最多20条当前返回记录，不代表全部分类"
    if tool_name in {ToolName.FUNCTION_STUDY, ToolName.COMMUNITY_ANALYSIS}:
        summary["pls_results"] = [
            {k: _compact_value(v) for k, v in row.items() if k not in {"input_rows", "scores"}}
            for row in result.get("pls_results", [])
        ]
        summary["full_result_list_counts"] = {
            k: len(v) for k, v in result.items() if isinstance(v, list)
        }
    if tool_name is ToolName.COMMUNITY_ANALYSIS:
        # 两标记×响应×环境变量可能超过默认20行；不能整块遗漏V9统计。
        summary["associations"] = [
            {k: _compact_value(v) for k, v in row.items()} for row in result.get("associations", [])
        ]
        summary["ordinations"] = [
            {k: _compact_value(v) for k, v in r.items() if k not in {"scores", "distance_rows"}}
            for r in result.get("ordinations", [])
        ]
    return summary


def _compact_value(value: Any) -> Any:
    """限制模型上下文大小，同时不改变结构化 API 结果。"""

    if isinstance(value, dict):
        return {str(key): _compact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_value(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > 300:
        return f"{value[:300]}..."
    return value
