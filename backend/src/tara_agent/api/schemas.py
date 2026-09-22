"""服务状态、会话与链路查询的 HTTP 响应结构。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DatasetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    filename: str
    ready: bool
    exists: bool
    size_bytes: int = Field(ge=0)
    column_count: int = Field(ge=0)
    sample_column_count: int = Field(ge=0)
    missing_columns: list[str]
    duplicate_columns: list[str]
    invalid_sample_columns: list[str]
    error: str | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    data_ready: bool
    database_ready: bool
    agent_ready: bool
    model: str
    datasets: list[DatasetStatus]


class QuestionSuggestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    question: str


class QuestionSuggestionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[QuestionSuggestionResponse]


class PageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, populate_by_name=True)

    id: UUID
    title: str | None
    status: str
    metadata: dict[str, Any] = Field(validation_alias="extra")
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    pinned_at: datetime | None


class MessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, populate_by_name=True)

    id: UUID
    session_id: UUID
    trace_id: UUID | None
    parent_message_id: UUID | None
    sequence_no: int = Field(ge=0)
    role: str
    message_type: str
    status: str
    content: str
    content_parts: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(validation_alias="extra")
    agent_response: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SessionSummary]
    page: PageInfo


class SessionDetail(SessionSummary):
    messages: list[MessageResponse]


class SessionDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_ids: list[UUID] = Field(min_length=1)

    @field_validator("session_ids")
    @classmethod
    def remove_duplicates(cls, value: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(value))


class SessionDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted_ids: list[UUID]
    deleted_count: int = Field(ge=0)


class SessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=80)
    pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("会话名称不能为空")
        return normalized

    @model_validator(mode="after")
    def require_change(self) -> "SessionUpdateRequest":
        if self.title is None and self.pinned is None:
            raise ValueError("至少需要修改一项会话信息")
        return self


class TraceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    session_id: UUID
    question: str | None = None
    correlation_id: str | None
    workflow_name: str
    workflow_version: str | None
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    retry_count: int
    model_provider: str | None
    model_name: str | None
    markers: list[str]
    sample_count: int | None
    data_sources: list[dict[str, Any]]
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class TraceSpanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    trace_id: UUID
    parent_span_id: UUID | None
    sequence_no: int
    name: str
    span_kind: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    retry_count: int
    input_data: dict[str, Any] | None
    output_data: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    error_data: dict[str, Any] | None
    model_provider: str | None
    model_name: str | None
    model_parameters: dict[str, Any]
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    context_length: int | None
    tool_name: str | None
    filters: dict[str, Any]
    marker: str | None
    sample_count: int | None
    sample_ids: list[str]
    data_sources: list[dict[str, Any]]
    artifact_refs: list[dict[str, Any]]
    attributes: dict[str, Any]


class TraceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TraceSummary]
    page: PageInfo


class TraceDetail(TraceSummary):
    model_parameters: dict[str, Any]
    input_data: dict[str, Any] | None
    output_data: dict[str, Any] | None
    error_data: dict[str, Any] | None
    attributes: dict[str, Any]
    spans: list[TraceSpanResponse]
