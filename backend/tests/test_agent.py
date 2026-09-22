from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tara_agent.agent import AgentModel
from tara_agent.agent.gateway import MCPToolGateway
from tara_agent.agent.graph import TaraAgent
from tara_agent.agent.models import (
    AnalysisReference,
    ConversationContext,
    ConversationMessage,
    ModelStreamDelta,
    ModelUsage,
    RouteDecision,
    ToolDefinition,
    ToolName,
    ToolPlan,
)
from tara_agent.agent.workflow import WorkflowTaskEvent
from tara_agent.data.preprocess import preprocess
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.mcp import create_server
from tara_agent.observability.execution import TraceObservationEvent


class RepresentativeModel(AgentModel):
    name = "test-model"

    def __init__(self) -> None:
        self.route_context = ConversationContext()
        self.plan_context = ConversationContext()
        self.plan_call_count = 0

    plans = {
        "找一个 Tara 样本": ToolPlan(
            tool_name="find_samples",
            arguments={"query": {"limit": 1}},
            rationale="需要筛选样本。",
        ),
        "查看 TARA_TEST_001": ToolPlan(
            tool_name="get_sample_info",
            arguments={"sample_id": "TARA_TEST_001"},
            rationale="需要查看单个样本。",
        ),
        "V4 中有哪些 Dinoflagellata ASV": ToolPlan(
            tool_name="find_taxa",
            arguments={"query": {"marker": "v4", "taxon": "Dinoflagellata"}},
            rationale="需要查询分类群。",
        ),
        "V4 Dinoflagellata 的相对丰度": ToolPlan(
            tool_name="taxon_abundance",
            arguments={"query": {"marker": "v4", "taxon": "Dinoflagellata"}},
            rationale="需要计算相对丰度。",
        ),
        "按极地分组比较 V9 多样性": ToolPlan(
            tool_name="diversity_analysis",
            arguments={"query": {"marker": "v9", "group_by": "polar"}},
            rationale="需要计算分组多样性。",
        ),
        "V4 Dinoflagellata 丰度和温度是否相关": ToolPlan(
            tool_name="environment_association",
            arguments={
                "query": {
                    "marker": "v4",
                    "taxon": "Dinoflagellata",
                    "environment_variable": "temperature",
                }
            },
            rationale="需要计算环境关联。",
        ),
        "只保留表层样本": ToolPlan(
            tool_name="find_samples",
            arguments={"query": {"depths": ["SRF"], "limit": 1}},
            rationale="续接既有样本查询并增加深度筛选。",
        ),
        "V9": ToolPlan(
            tool_name="diversity_analysis",
            arguments={"query": {"marker": "v9", "group_by": "polar"}},
            rationale="结合上一轮澄清内容比较 V9 多样性。",
        ),
    }

    async def route(
        self,
        question: str,
        context: ConversationContext,
        tools: list[ToolDefinition],
    ) -> RouteDecision:
        assert {tool.name for tool in tools} == set(ToolName)
        self.route_context = context
        if question in self.plans:
            analysis_reference_ids = []
            if question == "只保留表层样本" and context.analysis_references:
                analysis_reference_ids = [context.analysis_references[0].trace_id]
            return RouteDecision(
                kind="analysis",
                rationale="需要使用已接入数据。",
                capability_requirements=[
                    "diversity_analysis" if question == "V9" else "sample_query"
                ],
                analysis_reference_ids=analysis_reference_ids,
                usage=ModelUsage(input_tokens=60, output_tokens=10, total_tokens=70),
            )
        if question == "解释上次分析":
            reference = context.analysis_references[0]
            return RouteDecision(
                kind="direct_answer",
                rationale="已有分析摘要足以回答。",
                response=f"上次分析结论是：{reference.answer}",
                analysis_reference_ids=[reference.trace_id],
            )
        if question == "回顾前面的分析":
            reference = context.analysis_references[0]
            return RouteDecision(
                kind="direct_answer",
                rationale="回顾已有分析。",
                response=reference.answer,
                analysis_reference_ids=[uuid4(), reference.trace_id],
            )
        decisions = {
            "这个系统能做什么？": RouteDecision(
                kind="direct_answer",
                rationale="属于系统能力咨询。",
                response="可以查询 Tara 样本与分类群，并进行丰度、多样性和环境关联分析。",
            ),
            "帮我分析一下": RouteDecision(
                kind="clarify",
                rationale="缺少分析目标。",
                response="你希望分析哪些数据，以及想回答什么问题？",
            ),
            "按极地状态比较多样性": RouteDecision(
                kind="clarify",
                rationale="缺少标记。",
                response="请选择 V4 或 V9。",
            ),
            "Tara 目前在哪里航行？": RouteDecision(
                kind="unsupported",
                rationale="需要当前系统不具备的实时联网信息。",
                response="当前无法联网查询 Tara 的实时航行位置。",
            ),
        }
        return decisions[question]

    async def plan(
        self,
        question: str,
        context: ConversationContext,
        tools: list[ToolDefinition],
    ) -> ToolPlan:
        assert {tool.name for tool in tools} == set(ToolName)
        self.plan_context = context
        self.plan_call_count += 1
        return self.plans[question].model_copy(
            update={
                "usage": ModelUsage(
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                )
            }
        )

    async def stream_answer(
        self,
        question: str,
        plan: ToolPlan,
        result: dict[str, Any],
    ):
        assert result
        answer = f"已使用 {plan.tool_name.value} 回答：{question}"
        midpoint = len(answer) // 2
        yield ModelStreamDelta(kind="reasoning", content="先检查工具结果。")
        yield ModelStreamDelta(kind="answer", content=answer[:midpoint])
        yield ModelStreamDelta(kind="answer", content=answer[midpoint:])
        yield ModelStreamDelta(
            kind="usage",
            usage=ModelUsage(
                input_tokens=200,
                output_tokens=40,
                total_tokens=240,
                reasoning_tokens=10,
            ),
        )


@pytest.fixture
def representative_model() -> RepresentativeModel:
    return RepresentativeModel()


@pytest.fixture
def tara_agent(
    preprocessable_dataset_dir: Path,
    tmp_path: Path,
    representative_model: RepresentativeModel,
) -> TaraAgent:
    processed_dir = tmp_path / "processed"
    preprocess(
        preprocessable_dataset_dir,
        processed_dir,
        enforce_expected_shape=False,
    )
    server = create_server(ProcessedDataReader(processed_dir))
    return TaraAgent(representative_model, MCPToolGateway(server))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("question", "expected_tool", "expected_chart"),
    [
        ("找一个 Tara 样本", ToolName.FIND_SAMPLES, "sample_map"),
        ("查看 TARA_TEST_001", ToolName.GET_SAMPLE_INFO, None),
        ("V4 中有哪些 Dinoflagellata ASV", ToolName.FIND_TAXA, "bar"),
        ("V4 Dinoflagellata 的相对丰度", ToolName.TAXON_ABUNDANCE, "bar"),
        ("按极地分组比较 V9 多样性", ToolName.DIVERSITY_ANALYSIS, "bar"),
        ("V4 Dinoflagellata 丰度和温度是否相关", ToolName.ENVIRONMENT_ASSOCIATION, "scatter"),
    ],
)
async def test_representative_questions_follow_expected_tool_trace(
    tara_agent: TaraAgent,
    question: str,
    expected_tool: ToolName,
    expected_chart: str | None,
) -> None:
    response = await tara_agent.run(question)

    assert response.tool.name is expected_tool
    assert [step.stage for step in response.steps] == [
        "route",
        "understand",
        "execute",
        "answer",
    ]
    assert response.result
    assert response.model == "test-model"
    assert [chart.kind for chart in response.charts] == (
        [expected_chart] if expected_chart else []
    )


def test_plan_rejects_non_whitelisted_tool() -> None:
    with pytest.raises(ValidationError):
        ToolPlan.model_validate(
            {
                "tool_name": "run_sql",
                "arguments": {"sql": "select *"},
                "rationale": "not allowed",
            }
        )


@pytest.mark.anyio
async def test_stream_exposes_steps_answer_deltas_then_complete(tara_agent: TaraAgent) -> None:
    events = [event async for event in tara_agent.stream("找一个 Tara 样本")]

    assert [event.event for event in events] == [
        "step",
        "step",
        "step",
        "step",
        "reasoning_delta",
        "answer_delta",
        "answer_delta",
        "complete",
    ]
    assert events[-1].response is not None
    assert events[-1].response.tool.name is ToolName.FIND_SAMPLES
    streamed_reasoning = "".join(
        event.delta or "" for event in events if event.event == "reasoning_delta"
    )
    streamed_answer = "".join(
        event.delta or "" for event in events if event.event == "answer_delta"
    )
    assert streamed_reasoning == events[-1].response.reasoning
    assert streamed_answer == events[-1].response.answer


@pytest.mark.anyio
async def test_execution_stream_exposes_langgraph_task_lifecycle(
    tara_agent: TaraAgent,
) -> None:
    events = [
        event
        async for event in tara_agent.stream_execution("找一个 Tara 样本")
    ]
    tasks = [event for event in events if isinstance(event, WorkflowTaskEvent)]
    observations = [
        event for event in events if isinstance(event, TraceObservationEvent)
    ]

    assert [(event.node_name, event.phase) for event in tasks] == [
        ("route", "started"),
        ("route", "completed"),
        ("understand", "started"),
        ("understand", "completed"),
        ("execute", "started"),
        ("execute", "completed"),
        ("answer", "started"),
        ("answer", "completed"),
    ]
    for started, completed in zip(tasks[::2], tasks[1::2], strict=True):
        assert started.task_id == completed.task_id
        assert started.input_data is not None
        assert completed.output_data is not None

    assert [(event.span_kind, event.phase) for event in observations] == [
        ("llm", "started"),
        ("llm", "completed"),
        ("llm", "started"),
        ("llm", "completed"),
        ("tool", "started"),
        ("service", "started"),
        ("data", "started"),
        ("data", "completed"),
        ("service", "completed"),
        ("tool", "completed"),
        ("llm", "started"),
        ("llm", "completed"),
    ]
    task_ids = {event.task_id for event in tasks if event.phase == "started"}
    operation_ids = {event.observation_id for event in observations}
    assert all(
        event.parent_id in task_ids | operation_ids for event in observations
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("question", "expected_kind"),
    [
        ("这个系统能做什么？", "direct_answer"),
        ("帮我分析一下", "clarify"),
        ("Tara 目前在哪里航行？", "unsupported"),
    ],
)
async def test_non_analysis_routes_do_not_call_data_tools(
    tara_agent: TaraAgent,
    representative_model: RepresentativeModel,
    question: str,
    expected_kind: str,
) -> None:
    response = await tara_agent.run(question)

    assert response.route is not None
    assert response.route.kind == expected_kind
    assert response.tool is None
    assert response.answer == response.route.response
    assert response.result == {}
    assert [step.stage for step in response.steps] == ["route", "respond"]
    assert representative_model.plan_call_count == 0


@pytest.mark.anyio
async def test_new_analysis_keeps_messages_without_inheriting_analysis_references(
    tara_agent: TaraAgent,
    representative_model: RepresentativeModel,
) -> None:
    context = ConversationContext(
        messages=[
            ConversationMessage(role="user", content="先找一个样本"),
            ConversationMessage(role="assistant", content="找到了一个样本"),
        ]
    )

    await tara_agent.run("找一个 Tara 样本", context)

    assert representative_model.route_context == context
    assert representative_model.plan_context.messages == context.messages
    assert representative_model.plan_context.analysis_references == []


@pytest.mark.anyio
async def test_analysis_request_uses_only_the_selected_analysis_reference(
    tara_agent: TaraAgent,
    representative_model: RepresentativeModel,
) -> None:
    selected_trace_id = uuid4()
    context = ConversationContext(
        messages=[
            ConversationMessage(role="user", content="先找样本"),
            ConversationMessage(role="assistant", content="找到了样本"),
        ],
        analysis_references=[
            AnalysisReference(
                trace_id=selected_trace_id,
                question="先找样本",
                answer="找到了样本",
                tool_name="find_samples",
                tool_arguments={"query": {"limit": 1}},
                result_summary={"page": {"total": 2}},
            )
        ],
    )

    response = await tara_agent.run("只保留表层样本", context)

    assert response.route is not None
    assert response.route.analysis_reference_ids == [selected_trace_id]
    assert representative_model.plan_context.analysis_references == (
        context.analysis_references
    )


@pytest.mark.anyio
async def test_existing_analysis_can_be_discussed_without_new_tool(
    tara_agent: TaraAgent,
    representative_model: RepresentativeModel,
) -> None:
    reference = AnalysisReference(
        trace_id=uuid4(),
        question="先找样本",
        answer="找到两个样本。",
        tool_name="find_samples",
        tool_arguments={"query": {}},
        result_summary={"page": {"total": 2}},
    )
    context = ConversationContext(analysis_references=[reference])

    response = await tara_agent.run("解释上次分析", context)

    assert response.route is not None
    assert response.route.kind == "direct_answer"
    assert response.route.analysis_reference_ids == [reference.trace_id]
    assert response.answer == "上次分析结论是：找到两个样本。"
    assert response.tool is None
    assert representative_model.plan_call_count == 0


@pytest.mark.anyio
async def test_invalid_analysis_reference_does_not_fail_the_request(
    tara_agent: TaraAgent,
) -> None:
    reference = AnalysisReference(
        trace_id=uuid4(),
        question="筛选 SRF 样本",
        answer="筛选得到 118 个样本。",
        tool_name="find_samples",
        tool_arguments={"query": {"depths": ["SRF"]}},
        result_summary={"page": {"total": 118}},
    )

    response = await tara_agent.run(
        "回顾前面的分析",
        ConversationContext(analysis_references=[reference]),
    )

    assert response.route is not None
    assert response.route.analysis_reference_ids == [reference.trace_id]
    assert response.answer == reference.answer


@pytest.mark.anyio
async def test_clarification_response_uses_recent_conversation_messages(
    tara_agent: TaraAgent,
    representative_model: RepresentativeModel,
) -> None:
    clarification_trace_id = uuid4()
    context = ConversationContext(
        messages=[
            ConversationMessage(
                role="user",
                content="按极地状态比较多样性",
                trace_id=clarification_trace_id,
            ),
            ConversationMessage(
                role="assistant",
                content="请选择 V4 或 V9。",
                trace_id=clarification_trace_id,
            ),
        ]
    )

    response = await tara_agent.run("V9", context)

    assert response.route is not None
    assert response.route.analysis_reference_ids == []
    assert [
        message.content for message in representative_model.plan_context.messages
    ] == ["按极地状态比较多样性", "请选择 V4 或 V9。"]
    assert representative_model.plan_context.analysis_references == []
    assert response.tool is not None
    assert response.tool.name == ToolName.DIVERSITY_ANALYSIS
