"""构建长度受控的会话上下文。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from tara_agent.agent.context import build_result_summary
from tara_agent.agent.models import (
    AnalysisReference,
    ConversationContext,
    ConversationMessage,
    RouteDecision,
    RouteKind,
    ToolName,
)

MAX_CONTEXT_MESSAGES = 8
MAX_CONTEXT_CHARACTERS = 6_000
MAX_MESSAGE_CHARACTERS = 2_000
MAX_ANALYSIS_REFERENCE_CHARACTERS = 12_000

_GENERIC_CJK_FEATURES = frozenset(
    {
        "一下",
        "已有",
        "分析",
        "历史",
        "可以",
        "如何",
        "当前",
        "样本",
        "我们",
        "数据",
        "是否",
        "查看",
        "结果",
        "继续",
        "进行",
        "这个",
        "问题",
    }
)
_GENERIC_ASCII_FEATURES = frozenset({"agent", "tara", "the", "with"})


def build_conversation_context(
    messages: Iterable[ConversationMessage],
    analysis_references: Iterable[AnalysisReference] = (),
    *,
    question: str = "",
) -> ConversationContext:
    """保留近期消息，并按相关性和字符预算选择历史分析摘要。"""

    candidates = list(messages)[-MAX_CONTEXT_MESSAGES:]
    selected: list[ConversationMessage] = []
    remaining = MAX_CONTEXT_CHARACTERS

    for message in reversed(candidates):
        if remaining <= 0:
            break
        content = message.content.strip()
        if not content:
            continue
        content = content[: min(MAX_MESSAGE_CHARACTERS, remaining)]
        selected.append(message.model_copy(update={"content": content}))
        remaining -= len(content)

    selected.reverse()
    return ConversationContext(
        messages=selected,
        analysis_references=select_analysis_references(
            question,
            selected,
            analysis_references,
        ),
    )


def conversation_context_payload(context: ConversationContext) -> dict[str, Any]:
    """生成模型可见的上下文，只在分析摘要中暴露可引用的 Trace ID。"""

    return {
        "messages": [
            {"role": message.role, "content": message.content}
            for message in context.messages
        ],
        "analysis_references": [
            reference.model_dump(mode="json")
            for reference in context.analysis_references
        ],
    }


def select_analysis_references(
    question: str,
    messages: list[ConversationMessage],
    references: Iterable[AnalysisReference],
) -> list[AnalysisReference]:
    """优先选择当前问题和近期讨论相关的摘要，直到上下文预算用完。"""

    candidates = list(references)
    if not candidates:
        return []

    current_features = _search_features(question)
    topic_features = _search_features("\n".join(message.content for message in messages))
    linked_recency = {
        message.trace_id: index
        for index, message in enumerate(messages, start=1)
        if message.trace_id is not None
    }

    ranked: list[tuple[tuple[int, int, int, int], AnalysisReference]] = []
    for recency, reference in enumerate(candidates, start=1):
        reference_features = _search_features(_reference_text(reference))
        current_overlap = len(current_features & reference_features)
        topic_overlap = len(topic_features & reference_features)
        linked = linked_recency.get(reference.trace_id, 0)
        ranked.append(
            (
                (current_overlap, linked, topic_overlap, recency),
                reference,
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)

    selected: list[AnalysisReference] = []
    remaining = MAX_ANALYSIS_REFERENCE_CHARACTERS
    for _, reference in ranked:
        size = _reference_size(reference)
        if size > remaining:
            continue
        selected.append(reference)
        remaining -= size
    return selected


def build_analysis_reference(
    trace_id: UUID,
    response_data: dict[str, Any],
) -> AnalysisReference | None:
    """从持久化响应中提取可安全复用的分析摘要。"""

    question = response_data.get("question")
    tool = response_data.get("tool")
    result = response_data.get("result")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(tool, dict) or not isinstance(result, dict):
        return None

    try:
        tool_name = ToolName(tool.get("name"))
    except ValueError:
        return None
    arguments = tool.get("arguments")
    if not isinstance(arguments, dict):
        return None

    answer = response_data.get("answer")
    warnings = response_data.get("warnings")
    sources = response_data.get("sources")
    return AnalysisReference(
        trace_id=trace_id,
        question=question.strip()[:2_000],
        answer=answer.strip()[:1_000] if isinstance(answer, str) else "",
        tool_name=tool_name,
        tool_arguments=arguments,
        result_summary=build_result_summary(tool_name, result),
        warnings=_warning_messages(warnings),
        sources=_string_items(sources),
    )


def planning_context(
    context: ConversationContext,
    decision: RouteDecision,
) -> ConversationContext:
    """向分析规划提供近期对话和路由明确选中的分析摘要。"""

    if decision.kind is not RouteKind.ANALYSIS:
        return ConversationContext()
    references_by_id = {
        reference.trace_id: reference for reference in context.analysis_references
    }
    return ConversationContext(
        messages=context.messages,
        analysis_references=[
            references_by_id[trace_id]
            for trace_id in decision.analysis_reference_ids
        ],
    )


def filter_analysis_references(
    context: ConversationContext,
    decision: RouteDecision,
) -> RouteDecision:
    """只保留本次上下文中实际提供给模型的分析摘要引用。"""

    if not decision.analysis_reference_ids:
        return decision
    available_ids = {reference.trace_id for reference in context.analysis_references}
    valid_ids = [
        trace_id
        for trace_id in decision.analysis_reference_ids
        if trace_id in available_ids
    ]
    if valid_ids == decision.analysis_reference_ids:
        return decision
    return decision.model_copy(update={"analysis_reference_ids": valid_ids})


def _warning_messages(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    messages = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip()[:300])
    return messages


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:300] for item in value[:20] if isinstance(item, str) and item]


def _reference_text(reference: AnalysisReference) -> str:
    return "\n".join(
        (
            reference.question,
            reference.answer,
            reference.tool_name.value,
            json.dumps(reference.tool_arguments, ensure_ascii=False, sort_keys=True),
            json.dumps(reference.result_summary, ensure_ascii=False, sort_keys=True),
            "\n".join(reference.warnings),
            "\n".join(reference.sources),
        )
    )


def _reference_size(reference: AnalysisReference) -> int:
    serialized = json.dumps(
        reference.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return len(serialized) + 1


def _search_features(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    features = {
        term
        for term in re.findall(r"[a-z0-9][a-z0-9_.:-]{1,}", normalized)
        if term not in _GENERIC_ASCII_FEATURES
    }
    for sequence in re.findall(r"[\u4e00-\u9fff]+", normalized):
        features.update(
            sequence[index : index + 2]
            for index in range(len(sequence) - 1)
            if sequence[index : index + 2] not in _GENERIC_CJK_FEATURES
        )
    return features
