"""把 Agent 流式执行转换为可持久化的会话和链路记录。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tara_agent import __version__
from tara_agent.agent.conversation import (
    build_analysis_reference,
    build_conversation_context,
)
from tara_agent.agent.graph import TaraAgent
from tara_agent.agent.models import AgentResponse, AgentStreamEvent, ConversationMessage
from tara_agent.agent.workflow import WorkflowTaskEvent
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import TraceObservationEvent
from tara_agent.observability.recorder import TraceRecorder
from tara_agent.persistence.repositories import AgentRunRepository, StartedRun


class PersistentAgentRunner:
    """启动持久化请求，并为同步与流式接口复用同一执行路径。"""

    def __init__(self, agent: TaraAgent, repository: AgentRunRepository) -> None:
        self.agent = agent
        self.repository = repository

    async def start(
        self, question: str, user_id: str, session_id: UUID | None = None
    ) -> PersistentAgentRun:
        model_provider = str(getattr(self.agent.model, "provider", "unknown"))
        model_parameters = dict(getattr(self.agent.model, "trace_parameters", {}))
        run = await self.repository.start_run(
            user_id=user_id,
            question=question,
            session_id=session_id,
            workflow_name=self.agent.workflow_name,
            workflow_version=__version__,
            model_provider=model_provider,
            model_name=self.agent.model.name,
            model_parameters=model_parameters,
        )
        return PersistentAgentRun(
            agent=self.agent,
            repository=self.repository,
            run=run,
            question=question,
        )

    async def run(
        self, question: str, user_id: str, session_id: UUID | None = None
    ) -> AgentResponse:
        execution = await self.start(question, user_id, session_id)
        response: AgentResponse | None = None
        async for event in execution.stream():
            if event.event == "complete":
                response = event.response
        if response is None:
            raise RuntimeError("Agent 执行结束但未返回完整响应")
        return response


class PersistentAgentRun:
    """一次已经分配 session_id 和 trace_id 的 Agent 执行。"""

    def __init__(
        self,
        *,
        agent: TaraAgent,
        repository: AgentRunRepository,
        run: StartedRun,
        question: str,
    ) -> None:
        self.agent = agent
        self.repository = repository
        self.run_record = run
        self.question = question
        analysis_references = []
        for trace in run.analysis_traces:
            reference = build_analysis_reference(trace.trace_id, trace.response_data)
            if reference is not None:
                analysis_references.append(reference)
        self.context = build_conversation_context(
            (
                ConversationMessage(
                    role=message.role,
                    content=message.content.strip()[:2_000],
                    trace_id=message.trace_id,
                )
                for message in run.context_messages
                if message.role in {"user", "assistant"} and message.content.strip()
            ),
            analysis_references,
            question=question,
        )

    async def stream(self) -> AsyncIterator[AgentStreamEvent]:
        recorder = TraceRecorder(self.repository, self.run_record.trace_id)
        root_id = await recorder.start_observation(
            self.agent.workflow_title,
            ObservationKind.WORKFLOW,
            started_at=self.run_record.started_at,
            details=ObservationUpdate(
                input_data={
                    "question": self.question,
                    "conversation_context": self.context.model_dump(mode="json"),
                },
                attributes={
                    "workflow_name": self.agent.workflow_name,
                    "resources": self.agent.shared_resource_snapshot,
                },
            ),
        )
        task_span_ids: dict[str, UUID] = {}
        operation_span_ids: dict[str, UUID] = {}
        yield AgentStreamEvent(
            event="run_started",
            session_id=self.run_record.session_id,
            trace_id=self.run_record.trace_id,
        )

        try:
            async for event in self.agent.stream_execution(self.question, self.context):
                now = datetime.now(UTC)
                if isinstance(event, WorkflowTaskEvent):
                    await self._record_workflow_task(
                        recorder,
                        root_id,
                        task_span_ids,
                        event,
                        now,
                    )
                    continue
                if isinstance(event, TraceObservationEvent):
                    await self._record_operation(
                        recorder,
                        task_span_ids,
                        operation_span_ids,
                        event,
                    )
                    continue

                if event.event != "complete" or event.response is None:
                    yield event
                    continue

                response = event.response.model_copy(
                    update={
                        "session_id": self.run_record.session_id,
                        "trace_id": self.run_record.trace_id,
                        "message_id": self.run_record.assistant_message_id,
                    }
                )
                await recorder.finish_observation(
                    root_id,
                    ended_at=now,
                    details=ObservationUpdate(
                        output_data={
                            "route": (
                                response.route.kind.value if response.route is not None else None
                            ),
                            "tool_name": (
                                response.tool.name.value if response.tool is not None else None
                            ),
                            "answer": response.answer,
                        }
                    ),
                )
                await self._complete(response, now)
                yield event.model_copy(update={"response": response})
                return

            raise RuntimeError("Agent 在返回完整响应前结束")
        except BaseException as error:
            await self._record_failure(recorder, error)
            raise

    async def _record_failure(
        self,
        recorder: TraceRecorder,
        error: BaseException,
    ) -> None:
        """尽量关闭全部失败记录，同时保留触发失败的原始异常。"""

        ended_at = datetime.now(UTC)
        error_code = type(error).__name__
        error_message = str(error) or "Agent 执行被中断"
        cleanup_errors: list[Exception] = []

        try:
            await recorder.fail_open_observations(
                error_code=error_code,
                error_message=error_message,
                ended_at=ended_at,
            )
        except Exception as cleanup_error:
            cleanup_errors.append(cleanup_error)

        try:
            await self.repository.fail_run(
                self.run_record,
                ended_at=ended_at,
                duration_ms=_duration_ms(self.run_record.started_at, ended_at),
                error_code=error_code,
                error_message=error_message,
            )
        except Exception as cleanup_error:
            cleanup_errors.append(cleanup_error)

        for cleanup_error in cleanup_errors:
            error.add_note(
                f"记录 Agent 失败状态时发生 {type(cleanup_error).__name__}: {cleanup_error}"
            )

    async def _record_workflow_task(
        self,
        recorder: TraceRecorder,
        root_id: UUID,
        task_span_ids: dict[str, UUID],
        event: WorkflowTaskEvent,
        occurred_at: datetime,
    ) -> None:
        if event.phase == "started":
            if event.task_id in task_span_ids:
                raise RuntimeError(f"LangGraph 任务重复开始: {event.task_id}")
            task_span_ids[event.task_id] = await recorder.start_observation(
                event.node_title,
                ObservationKind.NODE,
                parent_span_id=root_id,
                started_at=occurred_at,
                details=ObservationUpdate(
                    input_data=_trace_mapping(event.input_data),
                    attributes={
                        "langgraph_task_id": event.task_id,
                        "langgraph_node": event.node_name,
                        "langgraph_namespace": list(event.namespace),
                    },
                ),
            )
            return

        try:
            span_id = task_span_ids.pop(event.task_id)
        except KeyError as error:
            raise RuntimeError(f"LangGraph 任务缺少开始事件: {event.task_id}") from error

        if event.phase == "completed":
            await recorder.finish_observation(
                span_id,
                ended_at=occurred_at,
                details=ObservationUpdate(
                    output_data=_trace_mapping(event.output_data),
                ),
            )
            return

        await recorder.fail_observation(
            span_id,
            ended_at=occurred_at,
            error_code="WorkflowNodeError",
            error_message=event.error or "LangGraph 节点执行失败",
        )

    async def _record_operation(
        self,
        recorder: TraceRecorder,
        task_span_ids: dict[str, UUID],
        operation_span_ids: dict[str, UUID],
        event: TraceObservationEvent,
    ) -> None:
        if event.phase == "started":
            parent_span_id = operation_span_ids.get(event.parent_id)
            if parent_span_id is None:
                parent_span_id = task_span_ids.get(event.parent_id)
            if parent_span_id is None:
                raise RuntimeError(f"执行操作的父节点不存在: {event.parent_id}")
            if event.observation_id in operation_span_ids:
                raise RuntimeError(f"Trace 操作重复开始: {event.observation_id}")
            operation_span_ids[event.observation_id] = await recorder.start_observation(
                event.name,
                event.span_kind,
                parent_span_id=parent_span_id,
                started_at=event.occurred_at,
                details=_observation_update(event.details),
            )
            return

        try:
            span_id = operation_span_ids.pop(event.observation_id)
        except KeyError as error:
            raise RuntimeError(
                f"Trace 操作缺少开始事件: {event.observation_id}"
            ) from error

        if event.phase == "completed":
            await recorder.finish_observation(
                span_id,
                ended_at=event.occurred_at,
                details=_observation_update(event.details),
            )
            return

        await recorder.fail_observation(
            span_id,
            ended_at=event.occurred_at,
            error_code=event.error_code or "OperationError",
            error_message=event.error_message or "执行失败",
        )

    async def _complete(
        self,
        response: AgentResponse,
        ended_at: datetime,
    ) -> None:
        provenance = _provenance(response.result)
        marker = provenance.get("marker")
        if marker is None and response.tool is not None:
            marker = _marker_from_arguments(response.tool.arguments)
        markers = [str(marker)] if marker else []
        sample_count = _nonnegative_int(provenance.get("sample_count"))
        data_sources = [{"filename": item} for item in response.sources]
        response_data = response.model_dump(mode="json", exclude_none=True)

        await self.repository.complete_run(
            self.run_record,
            ended_at=ended_at,
            duration_ms=_duration_ms(self.run_record.started_at, ended_at),
            answer=response.answer,
            reasoning=response.reasoning,
            response_data=response_data,
            markers=markers,
            sample_count=sample_count,
            data_sources=data_sources,
        )


def _duration_ms(started_at: datetime, ended_at: datetime) -> int:
    return max(0, round((ended_at - started_at).total_seconds() * 1_000))


def _provenance(result: dict[str, Any]) -> dict[str, Any]:
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    provenance = metadata.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _marker_from_arguments(arguments: dict[str, Any]) -> str | None:
    query = arguments.get("query")
    if isinstance(query, dict) and query.get("marker") is not None:
        return str(query["marker"])
    marker = arguments.get("marker")
    return str(marker) if marker is not None else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _trace_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"value": value}


def _observation_update(value: dict[str, Any] | None) -> ObservationUpdate | None:
    return ObservationUpdate(**value) if value is not None else None
