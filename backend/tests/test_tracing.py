import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tara_agent.observability.contracts import ObservationUpdate
from tara_agent.observability.recorder import TraceRecorder
from tara_agent.persistence.repositories import SpanRecord


class RecordingRepository:
    def __init__(self) -> None:
        self.records: dict[object, SpanRecord] = {}
        self.created: list[SpanRecord] = []
        self.updated: list[SpanRecord] = []

    async def create_span(self, record: SpanRecord) -> None:
        self.records[record.id] = record
        self.created.append(record)

    async def update_span(self, record: SpanRecord) -> None:
        self.records[record.id] = record
        self.updated.append(record)


async def _exercise_trace_lifecycle() -> None:
    repository = RecordingRepository()
    trace_id = uuid4()
    recorder = TraceRecorder(repository, trace_id)
    started_at = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)

    root_id = await recorder.start_observation(
        "工作流",
        "workflow",
        started_at=started_at,
        details=ObservationUpdate(input_data={"question": "测试问题"}),
    )
    child_id = await recorder.start_observation(
        "查询样本",
        "tool",
        parent_span_id=root_id,
        started_at=started_at + timedelta(seconds=1),
        details=ObservationUpdate(tool_name="find_samples"),
    )

    assert [record.status for record in repository.created] == ["running", "running"]
    assert repository.records[child_id].parent_span_id == root_id
    assert repository.records[child_id].sequence_no == 1

    await recorder.finish_observation(
        child_id,
        ended_at=started_at + timedelta(seconds=3),
        details=ObservationUpdate(
            output_data={"sample_count": 2},
            sample_count=2,
        ),
    )
    child = repository.records[child_id]
    assert child.status == "completed"
    assert child.duration_ms == 2_000
    assert child.tool_name == "find_samples"
    assert child.sample_count == 2

    await recorder.fail_open_observations(
        error_code="TestError",
        error_message="测试失败",
        ended_at=started_at + timedelta(seconds=4),
    )
    root = repository.records[root_id]
    assert root.status == "failed"
    assert root.duration_ms == 4_000
    assert root.error_code == "TestError"
    assert repository.records[child_id].status == "completed"


def test_trace_recorder_persists_incremental_nested_lifecycle() -> None:
    asyncio.run(_exercise_trace_lifecycle())


async def _exercise_trace_safety_contract() -> None:
    repository = RecordingRepository()
    recorder = TraceRecorder(repository, uuid4())

    root_id = await recorder.start_observation(" 工作流 ", "workflow")
    child_id = await recorder.start_observation(
        "模型调用",
        "llm",
        parent_span_id=root_id,
        details=ObservationUpdate(
            input_data={"api_key": "private", "question": "test"},
            attributes={"first": 1},
        ),
    )
    await recorder.update_observation(
        child_id,
        ObservationUpdate(
            attributes={"second": 2},
            sample_ids=[f"sample-{index}" for index in range(105)],
        ),
    )

    child = repository.records[child_id]
    assert repository.records[root_id].name == "工作流"
    assert child.input_data == {"api_key": "[REDACTED]", "question": "test"}
    assert child.attributes == {
        "first": 1,
        "second": 2,
        "sample_ids_omitted": 5,
    }
    assert len(child.sample_ids) == 100

    with pytest.raises(ValueError, match="不支持的 Trace 节点类型"):
        await recorder.start_observation("未知节点", "unknown")
    with pytest.raises(ValueError, match="链路节点的父节点不存在"):
        await recorder.start_observation("孤立节点", "node", parent_span_id=uuid4())
    with pytest.raises(ValueError, match="只能有一个根节点"):
        await recorder.start_observation("第二个根节点", "workflow")


def test_trace_recorder_enforces_safe_observation_contract() -> None:
    asyncio.run(_exercise_trace_safety_contract())
