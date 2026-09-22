from uuid import uuid4

import pytest
from pydantic import ValidationError

from tara_agent.agent.conversation import (
    build_analysis_reference,
    build_conversation_context,
    conversation_context_payload,
    filter_analysis_references,
    planning_context,
)
from tara_agent.agent.models import (
    AnalysisReference,
    ConversationContext,
    ConversationMessage,
    RouteDecision,
)


def test_conversation_context_keeps_recent_messages_within_limits() -> None:
    messages = [
        ConversationMessage(
            role="user" if index % 2 == 0 else "assistant",
            content=f"{index}:" + "x" * 1_000,
            trace_id=uuid4(),
        )
        for index in range(10)
    ]

    context = build_conversation_context(messages)

    assert len(context.messages) == 6
    assert context.messages[0].content.startswith("4:")
    assert context.messages[-1].content.startswith("9:")
    assert sum(len(message.content) for message in context.messages) <= 6_000


def test_model_context_only_exposes_trace_ids_for_analysis_references() -> None:
    reference = _analysis_reference("已完成的分析")
    context = ConversationContext(
        messages=[
            ConversationMessage(
                role="assistant",
                content="普通问答",
                trace_id=uuid4(),
            )
        ],
        analysis_references=[reference],
    )

    payload = conversation_context_payload(context)

    assert payload["messages"] == [{"role": "assistant", "content": "普通问答"}]
    assert payload["analysis_references"][0]["trace_id"] == str(reference.trace_id)


def test_conversation_context_uses_budget_instead_of_fixed_reference_count() -> None:
    references = [_analysis_reference(f"第 {index} 次分析") for index in range(8)]

    context = build_conversation_context(
        [],
        references,
        question="比较这些历史分析",
    )

    assert len(context.analysis_references) == 8
    assert {item.trace_id for item in context.analysis_references} == {
        item.trace_id for item in references
    }


def test_conversation_context_prioritizes_relevant_older_analysis() -> None:
    relevant = _analysis_reference("分析 V4 Bacillariophyta 丰度")
    unrelated = [_analysis_reference(f"第 {index} 次样本查询") for index in range(6)]

    context = build_conversation_context(
        [],
        [relevant, *unrelated],
        question="继续讨论 Bacillariophyta 的结果",
    )

    assert context.analysis_references[0] == relevant


def test_conversation_context_prioritizes_trace_linked_by_recent_messages() -> None:
    linked = _analysis_reference("较早的温度分析")
    latest = _analysis_reference("最近的多样性分析")
    messages = [
        ConversationMessage(
            role="assistant",
            content="温度分析已经完成。",
            trace_id=linked.trace_id,
        )
    ]

    context = build_conversation_context(
        messages,
        [linked, latest],
        question="继续解释这个结果",
    )

    assert context.analysis_references[0] == linked


def test_current_question_outweighs_an_unrelated_recent_trace() -> None:
    relevant = _analysis_reference("分析 V9 Bacillariophyta 多样性")
    linked = _analysis_reference("查询地中海温度")
    messages = [
        ConversationMessage(
            role="assistant",
            content="温度查询已经完成。",
            trace_id=linked.trace_id,
        )
    ]

    context = build_conversation_context(
        messages,
        [relevant, linked],
        question="回到 V9 Bacillariophyta 的分析",
    )

    assert context.analysis_references[0] == relevant


def test_conversation_context_limits_references_by_character_budget() -> None:
    references = [
        _analysis_reference(f"分析 {index}").model_copy(update={"answer": "x" * 1_000})
        for index in range(30)
    ]

    context = build_conversation_context([], references, question="回顾历史结果")

    assert 3 < len(context.analysis_references) < len(references)


def test_non_analysis_route_requires_user_facing_response() -> None:
    with pytest.raises(ValidationError, match="非分析路由必须包含"):
        RouteDecision(kind="clarify", rationale="缺少分析目标。")


def test_analysis_route_discards_user_facing_response() -> None:
    decision = RouteDecision(
        kind="analysis",
        rationale="可以使用现有数据处理。",
        response="不应展示",
    )

    assert decision.response is None


def test_non_grounded_routes_discard_analysis_references() -> None:
    decision = RouteDecision(
        kind="clarify",
        rationale="需要确认标记。",
        response="请选择 V4 或 V9。",
        analysis_reference_ids=[uuid4()],
    )

    assert decision.analysis_reference_ids == []


def test_direct_answer_can_record_used_analysis_references() -> None:
    reference = _analysis_reference("已有分析")
    context = ConversationContext(analysis_references=[reference])
    decision = RouteDecision(
        kind="direct_answer",
        rationale="已有摘要足以回答。",
        response="已有结果显示共找到一个样本。",
        analysis_reference_ids=[reference.trace_id],
    )

    validated = filter_analysis_references(context, decision)

    assert validated.analysis_reference_ids == [reference.trace_id]


def test_route_decision_allows_all_references_needed_by_current_request() -> None:
    trace_ids = [uuid4() for _ in range(5)]

    decision = RouteDecision(
        kind="analysis",
        rationale="需要综合多次分析。",
        analysis_reference_ids=trace_ids,
    )

    assert decision.analysis_reference_ids == trace_ids


def test_analysis_reference_keeps_summary_and_removes_detail_rows() -> None:
    trace_id = uuid4()

    reference = build_analysis_reference(
        trace_id,
        {
            "question": "找地中海样本",
            "answer": "找到 2 个样本。",
            "tool": {
                "name": "find_samples",
                "arguments": {"query": {"ocean_region": "Mediterranean"}},
            },
            "result": {
                "items": [{"sample_id": "A"}, {"sample_id": "B"}],
                "page": {"offset": 0, "limit": 20, "total": 2},
            },
            "warnings": [{"message": "测试警告"}],
            "sources": ["context_general.tsv"],
        },
    )

    assert reference is not None
    assert reference.trace_id == trace_id
    assert reference.result_summary == {"page": {"total": 2}}
    assert reference.warnings == ["测试警告"]
    assert reference.sources == ["context_general.tsv"]


def test_planning_context_keeps_messages_and_selected_analyses() -> None:
    first = _analysis_reference("第一次分析")
    second = _analysis_reference("第二次分析")
    context = ConversationContext(
        messages=[
            ConversationMessage(role="user", content="比较这两次结果"),
            ConversationMessage(role="assistant", content="需要继续计算。"),
        ],
        analysis_references=[first, second],
    )
    decision = RouteDecision(
        kind="analysis",
        rationale="需要根据两次结果继续分析。",
        capability_requirements=["sample_query"],
        analysis_reference_ids=[second.trace_id, first.trace_id],
    )

    validated = filter_analysis_references(context, decision)
    selected = planning_context(context, validated)

    assert selected.messages == context.messages
    assert selected.analysis_references == [second, first]


def test_clarification_answer_uses_recent_messages_without_trace_relation() -> None:
    context = ConversationContext(
        messages=[
            ConversationMessage(role="user", content="按极地状态比较多样性"),
            ConversationMessage(role="assistant", content="请选择 V4 或 V9。"),
        ]
    )
    decision = RouteDecision(
        kind="analysis",
        rationale="用户补充了标记。",
        capability_requirements=["diversity_analysis"],
    )

    selected = planning_context(context, decision)

    assert selected.messages == context.messages
    assert selected.analysis_references == []


def test_analysis_discards_reference_outside_current_context() -> None:
    reference = _analysis_reference("当前分析")
    context = ConversationContext(analysis_references=[reference])
    decision = RouteDecision(
        kind="analysis",
        rationale="引用指定分析。",
        capability_requirements=["sample_query"],
        analysis_reference_ids=[uuid4(), reference.trace_id],
    )

    filtered = filter_analysis_references(context, decision)

    assert filtered.analysis_reference_ids == [reference.trace_id]


def _analysis_reference(question: str) -> AnalysisReference:
    return AnalysisReference(
        trace_id=uuid4(),
        question=question,
        answer="分析完成。",
        tool_name="find_samples",
        tool_arguments={"query": {}},
        result_summary={"page": {"total": 1}},
    )
