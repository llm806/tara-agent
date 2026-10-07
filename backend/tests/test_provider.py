from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from tara_agent.agent.models import (
    AnalysisReference,
    ConversationContext,
    ConversationMessage,
    ToolDefinition,
    ToolName,
    ToolPlan,
)
from tara_agent.agent.multistep import AnalysisDecision
from tara_agent.agent.provider import AgentModelError, DeepSeekChatModel, MultiToolPlanRequired
from tara_agent.config import Settings


class FakeCompletions:
    def __init__(self, tool_calls: list[Any], usage: Any = None) -> None:
        self.tool_calls = tool_calls
        self.usage = usage
        self.request: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.request = kwargs
        message = SimpleNamespace(tool_calls=self.tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=self.usage,
        )


class FakeStreamingCompletions:
    def __init__(
        self,
        deltas: list[tuple[str | None, str | None]],
        usage: Any = None,
        finish_reason: str | None = None,
    ) -> None:
        self.deltas = deltas
        self.usage = usage
        self.finish_reason = finish_reason
        self.request: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.request = kwargs

        async def chunks():
            for reasoning, content in self.deltas:
                delta = SimpleNamespace(content=content, reasoning_content=reasoning)
                yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)
            if self.finish_reason is not None:
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=None, reasoning_content=None),
                            finish_reason=self.finish_reason,
                        )
                    ],
                    usage=None,
                )
            if self.usage is not None:
                yield SimpleNamespace(choices=[], usage=self.usage)

        return chunks()


def tool_call(name: str, arguments: str) -> Any:
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(function=function)


def model_with(completions: FakeCompletions) -> DeepSeekChatModel:
    model = DeepSeekChatModel(Settings(deepseek_api_key="test-key"))
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    return model


def streaming_model_with(completions: FakeStreamingCompletions) -> DeepSeekChatModel:
    model = DeepSeekChatModel(Settings(deepseek_api_key="test-key"))
    model.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    return model


@pytest.mark.anyio
async def test_planner_uses_required_function_call_with_mcp_schema() -> None:
    completions = FakeCompletions([tool_call("find_samples", '{"query":{"limit":1}}')])
    model = model_with(completions)
    tools = [
        ToolDefinition(
            name=ToolName.FIND_SAMPLES,
            description="Find bounded Tara samples.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "object",
                        "default": {},
                        "examples": [{"limit": 20}],
                    }
                },
                "required": ["query"],
            },
        )
    ]

    plan = await model.plan("找一个 Tara 样本", ConversationContext(), tools)

    assert plan.tool_name is ToolName.FIND_SAMPLES
    assert plan.arguments == {"query": {"limit": 1}}
    assert completions.request["tool_choice"] == "required"
    assert completions.request["extra_body"] == {"thinking": {"type": "disabled"}}
    function = completions.request["tools"][0]["function"]
    assert function["name"] == "find_samples"
    assert function["parameters"] == {
        "type": "object",
        "properties": {"query": {"type": "object"}},
        "required": ["query"],
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "calls",
    [
        [],
        [tool_call("find_samples", "not-json")],
        [tool_call("run_sql", '{"sql":"select 1"}')],
        [tool_call("function_study", '{"query":{}}')],
    ],
)
async def test_planner_rejects_unusable_function_calls(calls: list[Any]) -> None:
    model = model_with(FakeCompletions(calls))

    with pytest.raises(AgentModelError):
        await model.plan("test", ConversationContext(), [])


@pytest.mark.anyio
async def test_multiple_white_list_calls_require_multi_step_instead_of_dropping_one():
    model = model_with(FakeCompletions([
        tool_call("get_sample_info", '{"sample_id":"TARA_TEST_001"}'),
        tool_call("taxon_abundance", '{"query":{"marker":"v9","taxon":"Bacillariophyta"}}'),
    ]))
    tools = [ToolDefinition(name=name, description="测试工具", input_schema={})
             for name in (ToolName.GET_SAMPLE_INFO, ToolName.TAXON_ABUNDANCE)]
    with pytest.raises(MultiToolPlanRequired):
        await model.plan("查询背景和组成", ConversationContext(), tools)


@pytest.mark.anyio
async def test_answer_streams_reasoning_and_content_as_separate_deltas() -> None:
    completions = FakeStreamingCompletions(
        [("先检查数据。", None), (None, "结论"), (None, "：可靠。")]
    )
    model = streaming_model_with(completions)
    plan = ToolPlan(
        tool_name="find_samples",
        arguments={"query": {"limit": 1}},
        rationale="查找样本。",
    )

    chunks = [
        chunk
        async for chunk in model.stream_answer(
            "找一个样本",
            plan,
            {
                "items": [{"sample_id": str(index)} for index in range(25)],
                "page": {"offset": 0, "limit": 100, "total": 50},
            },
        )
    ]

    assert [(chunk.kind, chunk.content) for chunk in chunks] == [
        ("reasoning", "先检查数据。"),
        ("answer", "结论"),
        ("answer", "：可靠。"),
    ]
    assert completions.request["stream"] is True
    assert completions.request["stream_options"] == {"include_usage": True}
    assert "reasoning_effort" not in completions.request
    assert completions.request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert model.trace_parameters["answer"]["thinking"] == "disabled"
    assert "reasoning_effort" not in model.trace_parameters["answer"]
    assert completions.request["max_tokens"] == 65536
    assert completions.request["max_tokens"] == model.trace_parameters["answer"]["max_tokens"]
    user_content = json.loads(completions.request["messages"][1]["content"])
    assert user_content["question"] == "找一个样本"
    assert user_content["tool_name"] == "find_samples"
    assert "items" not in user_content["tool_result"]
    assert user_content["tool_result"]["page"] == {"offset": 0, "limit": 100, "total": 50}
    assert user_content["tool_result"]["returned_sample_scope"]["truncated"] is True
    assert "truncated_items" not in completions.request["messages"][1]["content"]


@pytest.mark.anyio
async def test_model_usage_is_returned_without_estimation() -> None:
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        prompt_tokens_details=SimpleNamespace(cached_tokens=20),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=10),
    )
    planner = FakeCompletions(
        [tool_call("find_samples", '{"query":{"limit":1}}')],
        usage=usage,
    )
    tools = [ToolDefinition(name=ToolName.FIND_SAMPLES, description="样本查询", input_schema={})]
    plan = await model_with(planner).plan("找一个样本", ConversationContext(), tools)
    assert plan.usage is not None
    assert plan.usage.model_dump() == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cached_tokens": 20,
        "reasoning_tokens": 10,
    }

    streaming = FakeStreamingCompletions([(None, "结论")], usage=usage)
    chunks = [
        chunk
        async for chunk in streaming_model_with(streaming).stream_answer(
            "找一个样本",
            plan,
            {"items": []},
        )
    ]
    assert chunks[-1].kind == "usage"
    assert chunks[-1].usage == plan.usage


@pytest.mark.anyio
async def test_answer_rejects_stream_without_content() -> None:
    model = streaming_model_with(FakeStreamingCompletions([("只有推理", None)]))
    plan = ToolPlan(
        tool_name="find_samples",
        arguments={"query": {"limit": 1}},
        rationale="查找样本。",
    )

    with pytest.raises(AgentModelError, match="empty answer"):
        _ = [
            chunk
            async for chunk in model.stream_answer(
                "找一个样本",
                plan,
                {"items": []},
            )
        ]


@pytest.mark.anyio
async def test_answer_rejects_token_truncation_even_with_partial_content():
    model = streaming_model_with(
        FakeStreamingCompletions([(None, "已完成。工作")], finish_reason="length")
    )
    plan = ToolPlan(tool_name="find_samples", arguments={"query": {}}, rationale="查询。")
    with pytest.raises(AgentModelError, match="incomplete"):
        _ = [chunk async for chunk in model.stream_answer("问题", plan, {})]


@pytest.mark.anyio
async def test_router_uses_context_capabilities_and_structured_decision() -> None:
    reference_id = UUID("5a1f2381-ad65-486c-9003-6e902df8ac76")
    completions = FakeCompletions(
        [
            tool_call(
                "route_request",
                json.dumps(
                    {
                        "kind": "analysis",
                        "rationale": "用户在续接样本查询。",
                        "response": "",
                        "capability_requirements": ["sample_query"],
                        "analysis_reference_ids": ["5a1f2381-ad65-486c-9003-6e902df8ac76"],
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    )
    model = model_with(completions)
    context = ConversationContext(
        messages=[
            ConversationMessage(
                role="user",
                content="先找地中海样本",
                trace_id=uuid4(),
            )
        ],
        analysis_references=[
            AnalysisReference(
                trace_id=reference_id,
                question="找地中海样本",
                answer="找到两个样本。",
                tool_name="find_samples",
                tool_arguments={"query": {"ocean_region": "Mediterranean"}},
                result_summary={"page": {"total": 2}},
            )
        ],
    )

    decision = await model.route("只保留表层的", context, [])

    assert decision.kind == "analysis"
    assert [str(item) for item in decision.analysis_reference_ids] == [
        "5a1f2381-ad65-486c-9003-6e902df8ac76"
    ]
    user_content = json.loads(completions.request["messages"][1]["content"])
    assert user_content["conversation_context"]["messages"][0]["content"] == ("先找地中海样本")
    assert "trace_id" not in user_content["conversation_context"]["messages"][0]
    assert user_content["capability_profile"]["product"] == "Tara Agent"
    assert completions.request["tools"][0]["function"]["name"] == "route_request"
    reference_schema = completions.request["tools"][0]["function"]["parameters"]["properties"][
        "analysis_reference_ids"
    ]
    assert "maxItems" not in reference_schema
    assert reference_schema["items"]["enum"] == [str(reference_id)]


@pytest.mark.anyio
async def test_router_disallows_analysis_references_when_context_has_none() -> None:
    completions = FakeCompletions(
        [
            tool_call(
                "route_request",
                json.dumps(
                    {
                        "kind": "direct_answer",
                        "rationale": "回答系统能力问题。",
                        "response": "可以查询样本。",
                        "capability_requirements": [],
                        "analysis_reference_ids": [],
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    )
    model = model_with(completions)

    await model.route("能做什么？", ConversationContext(), [])

    reference_schema = completions.request["tools"][0]["function"]["parameters"]["properties"][
        "analysis_reference_ids"
    ]
    assert reference_schema["maxItems"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "calls",
    [
        [tool_call("unknown_route", "{}")],
        [tool_call("route_request", "not-json")],
        [
            tool_call(
                "route_request",
                json.dumps(
                    {
                        "kind": "clarify",
                        "rationale": "缺少条件。",
                        "response": "",
                        "capability_requirements": [],
                        "analysis_reference_ids": [],
                    },
                    ensure_ascii=False,
                ),
            )
        ],
    ],
)
async def test_router_rejects_invalid_structured_decisions(calls: list[Any]) -> None:
    model = model_with(FakeCompletions(calls))

    with pytest.raises(AgentModelError):
        await model.route("test", ConversationContext(), [])


@pytest.mark.anyio
async def test_multistep_selector_and_planner_use_structured_evidence():
    selection = FakeCompletions(
        [
            tool_call(
                "select_analysis_workflow",
                json.dumps(
                    {
                        "workflow": "multi_step",
                        "rationale": "需要查询后计算。",
                        "goals": ["查询样本", "计算候选功能谱"],
                    }
                ),
            )
        ]
    )
    model = model_with(selection)
    assert (
        await model.select_workflow("综合分析", ConversationContext(), [])
    ).workflow == "multi_step"
    assert selection.request["tool_choice"] == "required"
    planner = FakeCompletions(
        [
            tool_call(
                "analysis_next_step",
                json.dumps(
                    {
                        "action": "tool",
                        "rationale": "查询返回了准确编号。",
                        "call": {
                            "tool_name": "function_profile",
                            "arguments": {"query": {"assay": "MetaT", "sample_name": "real-name"}},
                        },
                    }
                ),
            )
        ]
    )
    model = model_with(planner)
    result = await model.next_analysis_step(
        "继续",
        ConversationContext(),
        [],
        [{"step_id": 1, "result": {"items": [{"sample_name": "real-name"}], "total": 581}}],
        {"tool_calls": 5, "seconds": 100},
    )
    assert isinstance(result, AnalysisDecision)
    content = json.loads(planner.request["messages"][1]["content"])
    assert content["verified_steps"][0]["result"]["items"][0]["sample_name"] == "real-name"
    assert content["remaining_budget"]["tool_calls"] == 5
    assert "usage" not in planner.request["tools"][0]["function"]["parameters"]["properties"]
    assert result.usage is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "values",
    [
        {"action": "tool", "rationale": "没有调用"},
        {
            "action": "finish",
            "rationale": "非法调用",
            "call": {"tool_name": "find_samples", "arguments": {}},
        },
        {
            "action": "tool",
            "rationale": "任意代码",
            "call": {"tool_name": "run_python", "arguments": {}},
        },
        {"action": "clarify", "rationale": "没有具体问题"},
    ],
)
async def test_multistep_provider_rejects_invalid_decisions(values):
    model = model_with(FakeCompletions([tool_call("analysis_next_step", json.dumps(values))]))
    with pytest.raises(AgentModelError):
        await model.next_analysis_step("test", ConversationContext(), [], [], {})
