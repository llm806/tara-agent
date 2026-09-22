"""统一记录 Agent 执行期间的 Trace 节点。"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from tara_agent.observability.contracts import (
    DEFAULT_TRACE_DATA_LIMITS,
    ObservationKind,
    ObservationStatus,
    ObservationUpdate,
    sanitize_trace_mapping,
    sanitize_trace_text,
)
from tara_agent.persistence.repositories import SpanRecord


class TraceRepository(Protocol):
    """Trace Recorder 依赖的最小持久化接口。"""

    async def create_span(self, record: SpanRecord) -> None: ...

    async def update_span(self, record: SpanRecord) -> None: ...


class TraceRecorder:
    """以同一套生命周期写入本地 Trace，供后续观测适配器复用。"""

    def __init__(self, repository: TraceRepository, trace_id: UUID) -> None:
        self.repository = repository
        self.trace_id = trace_id
        self._next_sequence = 0
        self._records: dict[UUID, SpanRecord] = {}
        self._root_id: UUID | None = None

    async def start_observation(
        self,
        name: str,
        span_kind: ObservationKind | str,
        *,
        parent_span_id: UUID | None = None,
        started_at: datetime | None = None,
        details: ObservationUpdate | None = None,
    ) -> UUID:
        """创建运行中的节点并立即持久化。"""

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Trace 节点名称不能为空")
        if len(normalized_name) > 200:
            raise ValueError("Trace 节点名称不能超过 200 个字符")
        try:
            kind = ObservationKind(span_kind)
        except ValueError as error:
            raise ValueError(f"不支持的 Trace 节点类型: {span_kind}") from error
        if parent_span_id is not None and parent_span_id not in self._records:
            raise ValueError(f"链路节点的父节点不存在: {parent_span_id}")
        if parent_span_id is None and self._root_id is not None:
            raise ValueError("一个 Trace 只能有一个根节点")

        observation_id = uuid4()
        record = SpanRecord(
            id=observation_id,
            trace_id=self.trace_id,
            parent_span_id=parent_span_id,
            sequence_no=self._next_sequence,
            name=normalized_name,
            span_kind=kind.value,
            started_at=started_at or datetime.now(UTC),
        )
        self._next_sequence += 1
        if details is not None:
            record = _merge_update(record, _sanitize_update(details))
        await self.repository.create_span(record)
        self._records[observation_id] = record
        if parent_span_id is None:
            self._root_id = observation_id
        return observation_id

    async def update_observation(
        self,
        observation_id: UUID,
        details: ObservationUpdate,
    ) -> None:
        """在不改变节点状态的情况下补充执行信息。"""

        record = _merge_update(self._record(observation_id), _sanitize_update(details))
        await self.repository.update_span(record)
        self._records[observation_id] = record

    async def finish_observation(
        self,
        observation_id: UUID,
        *,
        ended_at: datetime | None = None,
        details: ObservationUpdate | None = None,
    ) -> None:
        """结束节点，并保存耗时和最终结果。"""

        record = self._record(observation_id)
        if record.status != "running":
            if details is not None:
                await self.update_observation(observation_id, details)
            return
        if details is not None:
            record = _merge_update(record, _sanitize_update(details))
        end = ended_at or datetime.now(UTC)
        record = replace(
            record,
            status=ObservationStatus.COMPLETED.value,
            ended_at=end,
            duration_ms=_duration_ms(record.started_at, end),
        )
        await self.repository.update_span(record)
        self._records[observation_id] = record

    async def fail_observation(
        self,
        observation_id: UUID,
        *,
        error_code: str,
        error_message: str,
        ended_at: datetime | None = None,
    ) -> None:
        """把仍在运行的节点标记为失败。"""

        record = self._record(observation_id)
        if record.status != "running":
            return
        end = ended_at or datetime.now(UTC)
        record = replace(
            record,
            status=ObservationStatus.FAILED.value,
            ended_at=end,
            duration_ms=_duration_ms(record.started_at, end),
            error_code=error_code,
            error_message=sanitize_trace_text(error_message),
        )
        await self.repository.update_span(record)
        self._records[observation_id] = record

    async def fail_open_observations(
        self,
        *,
        error_code: str,
        error_message: str,
        ended_at: datetime | None = None,
    ) -> None:
        """由内向外关闭本次执行中尚未结束的节点。"""

        end = ended_at or datetime.now(UTC)
        open_records = sorted(
            (record for record in self._records.values() if record.status == "running"),
            key=lambda record: record.sequence_no,
            reverse=True,
        )
        first_error: Exception | None = None
        for record in open_records:
            try:
                await self.fail_observation(
                    record.id,
                    error_code=error_code,
                    error_message=error_message,
                    ended_at=end,
                )
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _record(self, observation_id: UUID) -> SpanRecord:
        try:
            return self._records[observation_id]
        except KeyError as error:
            raise KeyError(f"未知的 Trace 节点：{observation_id}") from error


def _merge_update(record: SpanRecord, update: ObservationUpdate) -> SpanRecord:
    values = {
        field.name: value
        for field in fields(update)
        if (value := getattr(update, field.name)) is not None
    }
    for name in ("model_parameters", "filters", "attributes"):
        if name in values:
            values[name] = {**getattr(record, name), **values[name]}
    return replace(record, **values)


def _sanitize_update(update: ObservationUpdate) -> ObservationUpdate:
    attributes = (
        sanitize_trace_mapping(update.attributes)
        if update.attributes is not None
        else None
    )
    sample_ids = update.sample_ids
    if sample_ids is not None and len(sample_ids) > DEFAULT_TRACE_DATA_LIMITS.max_collection_items:
        omitted = len(sample_ids) - DEFAULT_TRACE_DATA_LIMITS.max_collection_items
        sample_ids = sample_ids[: DEFAULT_TRACE_DATA_LIMITS.max_collection_items]
        attributes = {
            **(attributes or {}),
            "sample_ids_omitted": omitted,
        }

    return replace(
        update,
        input_data=sanitize_trace_mapping(update.input_data),
        output_data=sanitize_trace_mapping(update.output_data),
        error_data=sanitize_trace_mapping(update.error_data),
        error_message=(
            sanitize_trace_text(update.error_message)
            if update.error_message is not None
            else None
        ),
        model_parameters=(
            sanitize_trace_mapping(update.model_parameters)
            if update.model_parameters is not None
            else None
        ),
        filters=(
            sanitize_trace_mapping(update.filters)
            if update.filters is not None
            else None
        ),
        sample_ids=sample_ids,
        data_sources=_sanitize_mapping_list(update.data_sources),
        artifact_refs=_sanitize_mapping_list(update.artifact_refs),
        attributes=attributes,
    )


def _sanitize_mapping_list(
    values: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if values is None:
        return None
    limit = DEFAULT_TRACE_DATA_LIMITS.max_collection_items
    return [sanitize_trace_mapping(value) or {} for value in values[:limit]]


def _duration_ms(started_at: datetime, ended_at: datetime) -> int:
    return max(0, round((ended_at - started_at).total_seconds() * 1_000))
