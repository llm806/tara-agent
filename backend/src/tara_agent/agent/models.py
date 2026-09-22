"""用于规划、调用轨迹、图表和聊天回复的已校验数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tara_agent.domain.contracts import ResultWarning


class ToolName(StrEnum):
    FIND_SAMPLES = "find_samples"
    GET_SAMPLE_INFO = "get_sample_info"
    FIND_TAXA = "find_taxa"
    TAXON_ABUNDANCE = "taxon_abundance"
    DIVERSITY_ANALYSIS = "diversity_analysis"
    ENVIRONMENT_ASSOCIATION = "environment_association"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ToolName
    description: str
    input_schema: dict[str, Any]


class ModelUsage(BaseModel):
    """模型服务商返回的实际 Token 用量。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class ConversationMessage(BaseModel):
    """提供给本次执行的单条历史对话。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2_000)
    trace_id: UUID | None = None


class AnalysisReference(BaseModel):
    """供后续请求引用的一次已完成分析摘要。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: UUID
    question: str = Field(min_length=1, max_length=2_000)
    answer: str = Field(max_length=1_000)
    tool_name: ToolName
    tool_arguments: dict[str, Any]
    result_summary: dict[str, Any]
    warnings: list[str] = Field(default_factory=list, max_length=20)
    sources: list[str] = Field(default_factory=list, max_length=20)


class ConversationContext(BaseModel):
    """经过长度限制的会话上下文。"""

    model_config = ConfigDict(extra="forbid")

    messages: list[ConversationMessage] = Field(default_factory=list, max_length=8)
    analysis_references: list[AnalysisReference] = Field(default_factory=list)


class RouteKind(StrEnum):
    DIRECT_ANSWER = "direct_answer"
    ANALYSIS = "analysis"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class AnalysisCapability(StrEnum):
    SAMPLE_QUERY = "sample_query"
    SAMPLE_DETAILS = "sample_details"
    TAXON_QUERY = "taxon_query"
    TAXON_ABUNDANCE = "taxon_abundance"
    DIVERSITY_ANALYSIS = "diversity_analysis"
    ENVIRONMENT_ASSOCIATION = "environment_association"


class RouteDecision(BaseModel):
    """统一请求入口产生的结构化判断。"""

    model_config = ConfigDict(extra="forbid")

    kind: RouteKind
    rationale: str = Field(min_length=1, max_length=300)
    response: str | None = Field(default=None, max_length=2_000)
    capability_requirements: list[AnalysisCapability] = Field(
        default_factory=list,
        max_length=10,
    )
    analysis_reference_ids: list[UUID] = Field(default_factory=list)
    usage: ModelUsage | None = None

    @field_validator("analysis_reference_ids")
    @classmethod
    def deduplicate_analysis_references(cls, values: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def validate_response(self) -> RouteDecision:
        if self.kind is RouteKind.ANALYSIS:
            self.response = None
            return self
        if self.response is None or not self.response.strip():
            raise ValueError("非分析路由必须包含面向用户的回复")
        if self.kind is not RouteKind.DIRECT_ANSWER:
            self.analysis_reference_ids = []
        self.response = self.response.strip()
        return self


class ToolPlan(BaseModel):
    """模型选定的一次受限 MCP 工具调用。"""

    model_config = ConfigDict(extra="forbid")

    tool_name: ToolName
    arguments: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=300)
    usage: ModelUsage | None = None


@dataclass(frozen=True, slots=True)
class ModelStreamDelta:
    """一段与模型服务商无关的思考或答案文本增量。"""

    kind: Literal["reasoning", "answer", "usage"]
    content: str = ""
    usage: ModelUsage | None = None


class AgentStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal["route", "understand", "execute", "answer", "respond"]
    title: str
    detail: str


class ChartKind(StrEnum):
    SAMPLE_MAP = "sample_map"
    BAR = "bar"
    SCATTER = "scatter"


class ChartSpec(BaseModel):
    """由前端渲染的简洁且与传输方式无关的图表数据契约。"""

    model_config = ConfigDict(extra="forbid")

    kind: ChartKind
    title: str
    x: list[str | float]
    y: list[float]
    labels: list[str] = Field(default_factory=list)
    x_label: str
    y_label: str


class ToolTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ToolName
    arguments: dict[str, Any]
    summary: str
    usage: ModelUsage | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    reasoning: str
    answer: str
    model: str
    route: RouteDecision | None = None
    tool: ToolTrace | None = None
    steps: list[AgentStep]
    result: dict[str, Any]
    charts: list[ChartSpec]
    warnings: list[ResultWarning]
    sources: list[str]
    answer_usage: ModelUsage | None = None
    session_id: UUID | None = None
    trace_id: UUID | None = None
    message_id: UUID | None = None


class AgentStreamEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal[
        "run_started",
        "step",
        "reasoning_delta",
        "answer_delta",
        "complete",
        "error",
    ]
    step: AgentStep | None = None
    delta: str | None = None
    response: AgentResponse | None = None
    error: str | None = None
    session_id: UUID | None = None
    trace_id: UUID | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2_000)
    session_id: UUID | None = None

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must not be blank")
        return normalized
