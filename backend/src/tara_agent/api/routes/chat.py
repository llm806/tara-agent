"""可追踪的聊天与服务器发送事件接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from tara_agent.agent.gateway import AgentToolError
from tara_agent.agent.graph import TaraAgent
from tara_agent.agent.models import AgentResponse, AgentStreamEvent, ChatRequest
from tara_agent.agent.provider import AgentModelError
from tara_agent.agent.runtime import PersistentAgentRun, PersistentAgentRunner
from tara_agent.api.dependencies import CurrentUserDependency
from tara_agent.persistence.repositories import SessionNotFoundError

router = APIRouter(prefix="/chat", tags=["agent"])


def _agent(request: Request) -> TaraAgent:
    agent: TaraAgent | None = request.app.state.agent
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent is unavailable. Check processed data and DEEPSEEK_API_KEY.",
        )
    return agent


def _persistent_agent(request: Request) -> PersistentAgentRunner | None:
    runner: PersistentAgentRunner | None = request.app.state.persistent_agent
    return runner


def _allow_ephemeral_execution(request: Request) -> bool:
    return request.app.state.settings.environment == "test"


@router.post("", response_model=AgentResponse, response_model_exclude_none=True)
async def chat(
    payload: ChatRequest,
    request: Request,
    user: CurrentUserDependency,
) -> AgentResponse:
    """执行一次完整的 Tara Agent 工作流。"""

    try:
        runner = _persistent_agent(request)
        if runner is not None:
            return await runner.run(payload.question, user.id, payload.session_id)
        if _allow_ephemeral_execution(request):
            return await _agent(request).run(payload.question)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库持久化服务不可用。",
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定的会话不存在或已经归档。",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库暂时不可用。",
        ) from exc
    except AgentToolError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except AgentModelError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@router.post("/stream", response_class=StreamingResponse)
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    user: CurrentUserDependency,
) -> StreamingResponse:
    """通过 SSE 流式传输工作流步骤和最终回复。"""

    agent = _agent(request)
    execution: PersistentAgentRun | None = None
    runner = _persistent_agent(request)
    try:
        if runner is not None:
            execution = await runner.start(payload.question, user.id, payload.session_id)
        elif not _allow_ephemeral_execution(request):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="数据库持久化服务不可用。",
            )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定的会话不存在或已经归档。",
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库暂时不可用。",
        ) from exc

    async def events() -> AsyncIterator[str]:
        try:
            stream = (
                execution.stream()
                if execution is not None
                else agent.stream(payload.question)
            )
            async for event in stream:
                yield _sse(event)
        except (AgentToolError, AgentModelError) as exc:
            yield _sse(
                AgentStreamEvent(
                    event="error",
                    error=str(exc),
                    session_id=(execution.run_record.session_id if execution else None),
                    trace_id=(execution.run_record.trace_id if execution else None),
                )
            )
        except SQLAlchemyError:
            yield _sse(
                AgentStreamEvent(
                    event="error",
                    error="数据库暂时不可用。",
                    session_id=(execution.run_record.session_id if execution else None),
                    trace_id=(execution.run_record.trace_id if execution else None),
                )
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: AgentStreamEvent) -> str:
    data = event.model_dump_json(exclude_none=True)
    return f"event: {event.event}\ndata: {data}\n\n"
