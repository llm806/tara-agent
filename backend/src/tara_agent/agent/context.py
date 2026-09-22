"""构造回答模型所需的紧凑上下文。"""

from __future__ import annotations

from typing import Any

from tara_agent.agent.models import ToolName

DETAIL_FIELDS_BY_TOOL: dict[ToolName, frozenset[str]] = {
    ToolName.FIND_SAMPLES: frozenset({"items"}),
    ToolName.FIND_TAXA: frozenset({"asvs", "sample_occurrences"}),
    ToolName.TAXON_ABUNDANCE: frozenset({"observations"}),
    ToolName.DIVERSITY_ANALYSIS: frozenset({"observations"}),
    ToolName.ENVIRONMENT_ASSOCIATION: frozenset({"points"}),
}
PAGINATION_FIELDS = frozenset({"page", "asv_page", "sample_page", "point_page"})


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
    return {
        str(key): (
            {"total": value.get("total")}
            if key in PAGINATION_FIELDS and isinstance(value, dict)
            else _compact_value(value)
        )
        for key, value in result.items()
        if key not in detail_fields
    }


def _compact_value(value: Any) -> Any:
    """限制模型上下文大小，同时不改变结构化 API 结果。"""

    if isinstance(value, dict):
        return {str(key): _compact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact_value(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > 300:
        return f"{value[:300]}..."
    return value
