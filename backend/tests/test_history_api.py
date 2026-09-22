"""不依赖数据库验证会话级链路查询路由。"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tara_agent.api.app import create_app
from tara_agent.config import Settings


class FakeTraceRepository:
    def __init__(self, session_id: UUID) -> None:
        now = datetime.now(UTC)
        self.session_id = session_id
        self.trace = SimpleNamespace(
            id=uuid4(),
            session_id=session_id,
            correlation_id=None,
            workflow_name="tara_analysis",
            workflow_version="1",
            status="running",
            started_at=now,
            ended_at=None,
            duration_ms=None,
            retry_count=0,
            model_provider="test",
            model_name="test-model",
            markers=[],
            sample_count=None,
            data_sources=[],
            error_code=None,
            error_message=None,
            input_data={"question": "测试问题"},
            created_at=now,
            updated_at=now,
        )

    async def session_exists(self, session_id: UUID, *, user_id: str) -> bool:
        return session_id == self.session_id and user_id == "test-user"

    async def list_traces(
        self,
        *,
        user_id: str,
        session_id: UUID | None,
        limit: int,
        offset: int,
    ) -> tuple[list[object], int]:
        assert user_id == "test-user"
        assert limit > 0 and offset == 0
        items = [self.trace] if session_id in {None, self.session_id} else []
        return items, len(items)


@pytest.mark.anyio
async def test_trace_lists_support_canonical_session_route(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        dataset_dir=tmp_path / "raw",
        processed_data_dir=tmp_path / "processed",
        cors_origins=[],
        _env_file=None,
    )
    app = create_app(settings)
    session_id = uuid4()
    app.state.run_repository = FakeTraceRepository(session_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        legacy = await client.get("/api/v1/traces")
        canonical = await client.get(f"/api/v1/sessions/{session_id}/traces")
        missing = await client.get(f"/api/v1/sessions/{uuid4()}/traces")

    assert legacy.status_code == 200
    assert canonical.status_code == 200
    assert canonical.json()["items"][0]["question"] == "测试问题"
    assert missing.status_code == 404
