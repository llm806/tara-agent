"""基于 DeepSeek 的问题规划与答案生成，提供可测试的接口。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from openai import AsyncOpenAI, OpenAIError

from tara_agent.agent.context import build_answer_model_input
from tara_agent.agent.conversation import conversation_context_payload
from tara_agent.agent.models import (
    ConversationContext,
    ModelStreamDelta,
    ModelUsage,
    RouteDecision,
    ToolDefinition,
    ToolPlan,
)
from tara_agent.agent.prompts import (
    ANSWER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
)
from tara_agent.agent.resources import CAPABILITY_PROFILE
from tara_agent.config import Settings


class AgentModelError(RuntimeError):
    """语言模型返回无法使用的回复时抛出。"""


class AgentModel(Protocol):
    @property
    def name(self) -> str: ...

    async def route(
        self,
        question: str,
        context: ConversationContext,
        tools: list[ToolDefinition],
    ) -> RouteDecision: ...

    async def plan(
        self,
        question: str,
        context: ConversationContext,
        tools: list[ToolDefinition],
    ) -> ToolPlan: ...

    def stream_answer(
        self,
        question: str,
        plan: ToolPlan,
        result: dict[str, Any],
    ) -> AsyncIterator[ModelStreamDelta]: ...


class DeepSeekChatModel:
    """使用 DeepSeek 兼容 OpenAI 的 Chat Completions API。"""

    def __init__(self, settings: Settings) -> None:
        if settings.deepseek_api_key is None:
            raise ValueError("DEEPSEEK_API_KEY is required")
        self._name = settings.deepseek_model
        self.reasoning_effort = settings.deepseek_reasoning_effort
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            timeout=settings.deepseek_timeout_seconds,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def provider(self) -> str:
        return "deepseek"

    @property
    def trace_parameters(self) -> dict[str, Any]:
        """返回不含密钥、可用于链路追踪的模型请求配置。"""

        return {
            "router": {
                "max_tokens": 1_000,
                "thinking": "disabled",
            },
            "planner": {
                "max_tokens": 1_200,
                "thinking": "disabled",
            },
            "answer": {
                "max_tokens": 4_000,
                "reasoning_effort": self.reasoning_effort,
                "thinking": "enabled",
            },
        }

    async def route(
        self,
        question: str,
        context: ConversationContext,
        tools: list[ToolDefinition],
    ) -> RouteDecision:
        user_content = json.dumps(
            {
                "question": question,
                "conversation_context": conversation_context_payload(context),
                "capability_profile": CAPABILITY_PROFILE,
                "available_tools": [
                    {"name": tool.name.value, "description": tool.description}
                    for tool in tools
                ],
            },
            ensure_ascii=False,
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.name,
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                tools=[
                    _route_function(
                        [str(reference.trace_id) for reference in context.analysis_references]
                    )
                ],
                tool_choice="required",
                max_tokens=1_000,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except OpenAIError as exc:
            raise AgentModelError("DeepSeek routing request failed") from exc

        tool_calls = response.choices[0].message.tool_calls or []
        if len(tool_calls) != 1 or tool_calls[0].function.name != "route_request":
            raise AgentModelError("DeepSeek must return one route_request call")
        try:
            values = json.loads(tool_calls[0].function.arguments)
            decision = RouteDecision.model_validate(values)
        except (AttributeError, TypeError, ValueError) as exc:
            raise AgentModelError("DeepSeek returned an invalid route decision") from exc
        return decision.model_copy(
            update={"usage": _model_usage(getattr(response, "usage", None))}
        )

    async def plan(
        self,
        question: str,
        context: ConversationContext,
        tools: list[ToolDefinition],
    ) -> ToolPlan:
        function_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name.value,
                    "description": tool.description,
                    "parameters": _schema_for_model(tool.input_schema),
                },
            }
            for tool in tools
        ]
        try:
            response = await self.client.chat.completions.create(
                model=self.name,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "question": question,
                                "conversation_context": conversation_context_payload(context),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                tools=function_tools,
                tool_choice="required",
                max_tokens=1_200,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except OpenAIError as exc:
            raise AgentModelError("DeepSeek planning request failed") from exc

        tool_calls = response.choices[0].message.tool_calls or []
        if len(tool_calls) != 1:
            raise AgentModelError("DeepSeek must select exactly one tool")
        tool_call = tool_calls[0]
        try:
            arguments = json.loads(tool_call.function.arguments)
            return ToolPlan(
                tool_name=tool_call.function.name,
                arguments=arguments,
                rationale=f"问题需要调用 {tool_call.function.name}。",
                usage=_model_usage(getattr(response, "usage", None)),
            )
        except (TypeError, ValueError) as exc:
            raise AgentModelError("DeepSeek returned an invalid tool plan") from exc

    async def stream_answer(
        self,
        question: str,
        plan: ToolPlan,
        result: dict[str, Any],
    ) -> AsyncIterator[ModelStreamDelta]:
        user_content = json.dumps(
            build_answer_model_input(question, plan.tool_name, result),
            ensure_ascii=False,
        )
        try:
            stream = await self.client.chat.completions.create(
                model=self.name,
                messages=[
                    {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=4_000,
                reasoning_effort=self.reasoning_effort,
                stream=True,
                stream_options={"include_usage": True},
                extra_body={"thinking": {"type": "enabled"}},
            )
            received_content = False
            async for chunk in stream:
                usage = _model_usage(getattr(chunk, "usage", None))
                if usage is not None:
                    yield ModelStreamDelta(kind="usage", usage=usage)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield ModelStreamDelta(kind="reasoning", content=reasoning)
                content = delta.content
                if content:
                    received_content = True
                    yield ModelStreamDelta(kind="answer", content=content)
        except OpenAIError as exc:
            raise AgentModelError("DeepSeek answer request failed") from exc
        if not received_content:
            raise AgentModelError("DeepSeek returned an empty answer")


def _schema_for_model(value: Any) -> Any:
    """移除生成提示，同时保留 MCP 校验契约。"""

    if isinstance(value, dict):
        return {
            key: _schema_for_model(item)
            for key, item in value.items()
            if key not in {"default", "examples"}
        }
    if isinstance(value, list):
        return [_schema_for_model(item) for item in value]
    return value


def _route_function(analysis_reference_ids: list[str]) -> dict[str, Any]:
    reference_schema: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string", "format": "uuid"},
        "description": "本次请求实际使用的已有分析 trace_id。",
    }
    if analysis_reference_ids:
        reference_schema["items"]["enum"] = analysis_reference_ids
    else:
        reference_schema["maxItems"] = 0

    return {
        "type": "function",
        "function": {
            "name": "route_request",
            "description": "判断当前用户请求应由 Tara Agent 如何处理。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "direct_answer",
                            "analysis",
                            "clarify",
                            "unsupported",
                        ],
                    },
                    "rationale": {"type": "string", "minLength": 1, "maxLength": 300},
                    "response": {"type": "string", "maxLength": 2_000},
                    "capability_requirements": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "sample_query",
                                "sample_details",
                                "taxon_query",
                                "taxon_abundance",
                                "diversity_analysis",
                                "environment_association",
                            ],
                        },
                        "maxItems": 10,
                    },
                    "analysis_reference_ids": reference_schema,
                },
                "required": [
                    "kind",
                    "rationale",
                    "response",
                    "capability_requirements",
                    "analysis_reference_ids",
                ],
            },
        },
    }


def _model_usage(value: Any) -> ModelUsage | None:
    """把兼容 OpenAI 的用量对象转换为稳定的内部结构。"""

    if value is None:
        return None
    prompt_details = getattr(value, "prompt_tokens_details", None)
    completion_details = getattr(value, "completion_tokens_details", None)
    return ModelUsage(
        input_tokens=value.prompt_tokens,
        output_tokens=value.completion_tokens,
        total_tokens=value.total_tokens,
        cached_tokens=getattr(prompt_details, "cached_tokens", None),
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", None),
    )
