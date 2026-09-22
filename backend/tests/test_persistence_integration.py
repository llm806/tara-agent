import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from tara_agent.config import Settings
from tara_agent.persistence import AgentTrace, ChatMessage, ChatSession, Database, TraceSpan, User
from tara_agent.persistence.repositories import AgentRunRepository

pytestmark = pytest.mark.integration


async def _exercise_database() -> None:
    database = Database(Settings())
    session_id = uuid4()
    trace_id = uuid4()
    user_id = str(uuid4())

    try:
        await database.ping()

        async with database.session() as session:
            session.add(
                User(
                    id=user_id,
                    email=f"{user_id}@example.com",
                    display_name="集成测试用户",
                    password_hash="not-a-real-password-hash",
                )
            )
            await session.flush()
            session.add(
                ChatSession(
                    id=session_id,
                    user_id=user_id,
                    title="数据库往返测试",
                )
            )
            await session.flush()
            session.add(
                AgentTrace(
                    id=trace_id,
                    session_id=session_id,
                    workflow_name="integration-test",
                    status="completed",
                    model_provider="deepseek",
                    model_name="deepseek-flash",
                    markers=["V4"],
                    sample_count=1,
                )
            )
            await session.flush()
            session.add(
                ChatMessage(
                    session_id=session_id,
                    trace_id=trace_id,
                    sequence_no=0,
                    role="user",
                    content="测试消息",
                )
            )
            session.add(
                TraceSpan(
                    trace_id=trace_id,
                    sequence_no=0,
                    name="调用分析工具",
                    span_kind="tool",
                    status="completed",
                    tool_name="find_samples",
                    marker="V4",
                    sample_count=1,
                    sample_ids=["TARA_TEST_SAMPLE"],
                    filters={"depth": "SRF"},
                    data_sources=[{"filename": "context_general.tsv"}],
                )
            )
            for index in range(5):
                session.add(
                    AgentTrace(
                        session_id=session_id,
                        workflow_name="analysis-history-test",
                        status="completed",
                        model_provider="deepseek",
                        model_name="deepseek-flash",
                        output_data={
                            "question": f"历史分析 {index}",
                            "answer": "分析完成。",
                            "tool": {
                                "name": "find_samples",
                                "arguments": {"query": {"limit": 1}},
                            },
                            "result": {"page": {"total": index + 1}},
                        },
                    )
                )

        repository = AgentRunRepository(database)
        started = await repository.start_run(
            user_id=user_id,
            question="综合历史分析",
            session_id=session_id,
            workflow_name="integration-test",
            workflow_version="1",
            model_provider="deepseek",
            model_name="deepseek-flash",
            model_parameters={},
        )
        assert len(started.analysis_traces) == 5

        async with database.session() as session:
            trace = await session.get(AgentTrace, trace_id)
            assert trace is not None
            assert trace.markers == ["V4"]
            assert trace.sample_count == 1

            span = await session.scalar(
                select(TraceSpan).where(TraceSpan.trace_id == trace_id)
            )
            assert span is not None
            assert span.filters == {"depth": "SRF"}

            parent = await session.get(ChatSession, session_id)
            assert parent is not None
            await session.delete(parent)

        async with database.session() as session:
            trace_count = await session.scalar(
                select(func.count()).select_from(AgentTrace).where(AgentTrace.id == trace_id)
            )
            span_count = await session.scalar(
                select(func.count()).select_from(TraceSpan).where(TraceSpan.trace_id == trace_id)
            )
            assert trace_count == 0
            assert span_count == 0
    finally:
        async with database.session() as session:
            await session.execute(delete(User).where(User.id == user_id))
        await database.dispose()


def test_database_round_trip_and_cascade() -> None:
    if os.environ.get("TARA_RUN_DATABASE_TESTS") != "1":
        pytest.skip("设置 TARA_RUN_DATABASE_TESTS=1 后执行本地 PostgreSQL 集成测试")

    asyncio.run(_exercise_database())
