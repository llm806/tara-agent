"""在一次 LangGraph 任务内产生可持久化的执行事件。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from tara_agent.observability.contracts import ObservationKind, ObservationUpdate


class TraceObservationEvent(BaseModel):
    """业务边界发给持久化运行器的内部 Trace 生命周期事件。"""

    model_config = ConfigDict(extra="forbid")

    event: Literal["trace_observation"] = "trace_observation"
    observation_id: str
    parent_id: str
    name: str
    span_kind: ObservationKind
    phase: Literal["started", "completed", "failed"]
    occurred_at: datetime
    details: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _ExecutionTraceContext:
    writer: Any
    parent_id: str


_current_context: ContextVar[_ExecutionTraceContext | None] = ContextVar(
    "tara_execution_trace_context",
    default=None,
)


@contextmanager
def bind_execution_trace(*, parent_id: str | None, writer: Any):
    """绑定一次编排任务，供其内部操作继承准确父节点。"""

    if parent_id is None:
        yield
        return

    token = _current_context.set(
        _ExecutionTraceContext(
            writer=writer,
            parent_id=parent_id,
        )
    )
    try:
        yield
    finally:
        _current_context.reset(token)


class ObservationScope:
    """记录一个可嵌套操作；没有执行上下文时自动退化为空操作。"""

    def __init__(
        self,
        name: str,
        span_kind: ObservationKind,
        details: ObservationUpdate | None,
    ) -> None:
        self.name = name
        self.span_kind = span_kind
        self.start_details = details
        self.end_details: ObservationUpdate | None = None
        self.observation_id = str(uuid4())
        self._context = _current_context.get()
        self._token: Token[_ExecutionTraceContext | None] | None = None

    def __enter__(self) -> ObservationScope:
        if self._context is None:
            return self
        self._emit("started", details=self.start_details)
        self._token = _current_context.set(
            _ExecutionTraceContext(
                writer=self._context.writer,
                parent_id=self.observation_id,
            )
        )
        return self

    def finish(self, details: ObservationUpdate | None = None) -> None:
        """设置正常结束时写入的结果信息。"""

        self.end_details = details

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._context is None:
            return False
        if self._token is not None:
            _current_context.reset(self._token)
        if exc_value is None:
            self._emit("completed", details=self.end_details)
        else:
            self._emit(
                "failed",
                error_code=type(exc_value).__name__,
                error_message=str(exc_value) or "执行失败",
            )
        return False

    def _emit(
        self,
        phase: Literal["started", "completed", "failed"],
        *,
        details: ObservationUpdate | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self._context is None:
            return
        event = TraceObservationEvent(
            observation_id=self.observation_id,
            parent_id=self._context.parent_id,
            name=self.name,
            span_kind=self.span_kind,
            phase=phase,
            occurred_at=datetime.now(UTC),
            details=_update_mapping(details),
            error_code=error_code,
            error_message=error_message,
        )
        self._context.writer(event.model_dump(mode="json"))


def observe(
    name: str,
    span_kind: ObservationKind,
    details: ObservationUpdate | None = None,
) -> ObservationScope:
    """创建一个继承当前父节点的执行观测范围。"""

    return ObservationScope(name, span_kind, details)


def execution_parent_id() -> str | None:
    """将当前真实父节点传给远程分析，不创建独立 Trace。"""

    context = _current_context.get()
    return context.parent_id if context is not None else None


def replay_execution_observations(events: list[TraceObservationEvent]) -> None:
    """验证完整子树后转交同一 Recorder，防止远程节点越界或悬空。"""

    context = _current_context.get()
    if context is None:
        return
    parents = {context.parent_id}
    running: dict[str, tuple[str, str, ObservationKind]] = {}
    for event in events:
        if event.parent_id not in parents:
            raise ValueError("远程观测节点不属于当前执行树")
        identity = (event.parent_id, event.name, event.span_kind)
        if event.phase == "started":
            if event.observation_id in parents:
                raise ValueError("远程观测节点标识重复")
            running[event.observation_id] = identity
            parents.add(event.observation_id)
        elif running.pop(event.observation_id, None) != identity:
            raise ValueError("远程观测节点没有对应的开始事件")
    if running:
        raise ValueError("远程观测包含未结束的节点")
    for event in events:
        context.writer(event.model_dump(mode="json"))


def _update_mapping(update: ObservationUpdate | None) -> dict[str, Any] | None:
    if update is None:
        return None
    return {
        field.name: value
        for field in fields(update)
        if (value := getattr(update, field.name)) is not None
    }
