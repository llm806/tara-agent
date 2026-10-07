"""验证有界编排、科学结果保留、远程 Trace 及跨任务组合。"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from tara_agent.agent.conversation import build_analysis_reference
from tara_agent.agent.gateway import AgentToolError
from tara_agent.agent.graph import TaraAgent
from tara_agent.agent.models import ModelStreamDelta, RouteDecision, ToolName
from tara_agent.agent.multistep import (
    AnalysisDecision,
    AnalysisLimits,
    WorkflowSelection,
    bounded_evidence,
)
from tara_agent.agent.provider import AgentModelError, MultiToolPlanRequired
from tara_agent.agent.runtime import PersistentAgentRun
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.persistence.repositories import StartedRun

from .test_agent_runtime import RuntimeRepository
from .test_data_service import service_settings as service_settings
from .test_matou_function_data import dataset as dataset
from .test_matou_remote_tools import matou_setup as matou_setup


class SequenceModel:
    name = "bounded-analysis-test"

    def __init__(self, scenario="function", failure=None):
        self.scenario = scenario
        self.failure = failure
        self.rounds = 0

    async def route(self, question, context, tools):
        return RouteDecision(kind="analysis", rationale="需要组合现有工具。")

    async def select_workflow(self, question, context, tools):
        goals = (
            ["查询样本", "查看样本背景"]
            if self.scenario == "core"
            else ["查询样本", "计算候选功能谱", "计算筛选条件下的候选功能谱"]
        )
        return WorkflowSelection(workflow="multi_step", rationale="目标需要多个调用。", goals=goals)

    async def next_analysis_step(self, question, context, tools, steps, budget):
        self.rounds += 1
        if not steps:
            name = "find_function_samples" if self.scenario == "function" else "find_samples"
            query = {"assay": "MetaG", "limit": 20} if self.scenario == "function" else {"limit": 1}
            return AnalysisDecision(
                action="tool",
                rationale="先查询真实样本。",
                call={"tool_name": name, "arguments": {"query": query}},
            )
        if self.failure == "plan_error":
            raise AgentModelError("规划模型暂时不可用")
        if self.failure == "plan_timeout":
            await asyncio.sleep(1)
        if self.failure == "repeat":
            return AnalysisDecision(
                action="tool",
                rationale="重复查询。",
                call={"tool_name": steps[0]["tool_name"], "arguments": steps[0]["arguments"]},
            )
        if self.failure == "clarify":
            return AnalysisDecision(
                action="clarify", rationale="需要准确范围。", message="请选择具体样本。"
            )
        if self.scenario == "core":
            if len(steps) == 1:
                sample = steps[0]["result"]["items"][0]["sample_id_pangaea"]
                return AnalysisDecision(
                    action="tool",
                    rationale="使用查询结果中的真实编号。",
                    call={"tool_name": "get_sample_info", "arguments": {"sample_id": sample}},
                )
        elif len(steps) < 3:
            sample = next(s for s in steps[0]["result"]["items"] if s["observed_gene_records"] > 0)
            query = {
                "assay": steps[0]["result"]["assay"],
                "sample_name": sample["sample_name"],
                "top_n": 10,
            }
            if len(steps) == 2:
                query["max_i_evalue"] = 1e-5
            return AnalysisDecision(
                action="tool",
                rationale="同一样本在明确候选条件下计算。",
                call={"tool_name": "function_profile", "arguments": {"query": query}},
            )
        return AnalysisDecision(
            action="finish",
            rationale="各项目标都有工具结果支持。",
            goal_checks=[
                {
                    "goal_id": i,
                    "status": "completed",
                    "evidence_steps": [i],
                    "reason": "对应工具已返回结果。",
                }
                for i in range(1, len(steps) + 1)
            ],
        )

    async def stream_answer(self, question, plan, result):
        assert result["workflow"] == "multi_step"
        if self.failure == "answer_error":
            raise AgentModelError("汇总模型暂时不可用")
        yield ModelStreamDelta(kind="answer", content="仅解释已完成的工具结果。")


class CountingGateway:
    def __init__(self, remote, failure=None):
        self.remote = remote
        self.failure = failure
        self.calls = []

    async def list_tools(self):
        tools = await self.remote.list_tools()
        if self.failure == "unavailable":
            return [t for t in tools if t.name != ToolName.FUNCTION_PROFILE]
        return tools

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if len(self.calls) == 2:
            if self.failure == "tool_error":
                raise AgentToolError("样本来源验证失败")
            if self.failure == "malformed_result":
                return {"observations": []}
            if self.failure == "tool_timeout":
                await asyncio.sleep(1)
        return await self.remote.call(name, arguments)


def make_agent(matou_setup, *, scenario="function", failure=None, limits=None):
    _, local, service = matou_setup
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=service))
    gateway = CountingGateway(remote, failure)
    model = SequenceModel(scenario, failure)
    return TaraAgent(model, gateway, limits=limits), gateway, model


@pytest.mark.anyio
async def test_remote_sensitivity_workflow_preserves_science_and_real_trace(matou_setup):
    agent, gateway, _ = make_agent(matou_setup)
    repository = RuntimeRepository()
    run = StartedRun(
        session_id=uuid4(),
        trace_id=uuid4(),
        user_message_id=uuid4(),
        assistant_message_id=uuid4(),
        started_at=datetime.now(UTC),
    )
    execution = PersistentAgentRun(
        agent=agent,
        repository=repository,
        run=run,
        question="任选一个 MetaG 样本，分别计算两种候选条件。",
    )
    events = [event async for event in execution.stream()]
    response = events[-1].response
    assert repository.completed and not repository.failed
    assert response.tool is None
    assert response.result["status"] == "completed"
    assert [name.value for name, _ in gateway.calls] == [
        "find_function_samples",
        "function_profile",
        "function_profile",
    ]
    first, second = [s["result"] for s in response.result["analysis_steps"][1:]]
    assert first["context"]["sample_name"] == second["context"]["sample_name"] == "G-A"
    assert first["taxon_value_sum"] == second["taxon_value_sum"] == 1
    assert first["annotated_signal_fraction"] == pytest.approx(0.9)
    assert second["annotated_signal_fraction"] == pytest.approx(0.9)
    assert first["observations"][0]["value_sum"] == pytest.approx(0.9)
    assert second["observations"][0]["value_sum"] == pytest.approx(0.6)
    assert first["value_interpretation"] == "provided_values_export_unit_unconfirmed"
    assert first["metadata"]["provenance"]["filters"]["source_files"]
    spans = list(repository.spans.values())
    roots = [s for s in spans if s.parent_span_id is None]
    assert len(roots) == 1
    assert all(s.status == "completed" for s in spans)
    span_by_id = {s.id: s for s in spans}
    assert all(s.parent_span_id in span_by_id for s in spans if s.parent_span_id)
    nodes = [s for s in spans if s.span_kind == "node"]
    assert len([s for s in nodes if s.attributes["langgraph_node"] == "multi_execute"]) == 3
    tools = [s for s in spans if s.span_kind == "tool"]
    assert len(tools) == 3 and all(span_by_id[s.parent_span_id].span_kind == "node" for s in tools)
    assert any(s.span_kind == "data" for s in spans)
    assert len([e for e in events if e.step and e.step.stage == "execute"]) == 3
    assert len(response.charts) == 2
    ref = build_analysis_reference(run.trace_id, response.model_dump(mode="json"))
    assert ref.result_summary["workflow"] == "multi_step"
    assert len(ref.result_summary["analysis_steps"]) == 3
    assert ref.tool_arguments == gateway.calls[-1][1]


@pytest.mark.anyio
async def test_workflow_generalizes_to_core_sample_context(matou_setup):
    agent, gateway, _ = make_agent(matou_setup, scenario="core")
    previous = agent.model.next_analysis_step

    async def labelled_decision(*args):
        decision = await previous(*args)
        if decision.action == "finish":
            return decision.model_copy(update={"final_result_steps": [2]})
        return decision

    agent.model.next_analysis_step = labelled_decision
    response = await agent.run("任选一个 Tara 样本，查看环境。")
    assert response.result["status"] == "completed"
    assert [n for n, _ in gateway.calls] == [ToolName.FIND_SAMPLES, ToolName.GET_SAMPLE_INFO]
    steps = response.result["analysis_steps"]
    assert [s["result_role"] for s in steps] == ["intermediate", "final"]
    assert response.answer.startswith("【最终结果】")
    assert steps[1]["arguments"]["sample_id"] == steps[0]["result"]["items"][0]["sample_id_pangaea"]


@pytest.mark.anyio
async def test_single_planner_multiple_calls_reroute_without_dropping_queries(matou_setup):
    class MisclassifiedModel(SequenceModel):
        async def select_workflow(self, question, context, tools):
            return WorkflowSelection(workflow="single_step", rationale="误判为单步")

        async def plan(self, question, context, tools):
            raise MultiToolPlanRequired(None)

        async def next_analysis_step(self, question, context, tools, steps, budget):
            if len(steps) < 2:
                return await super().next_analysis_step(question, context, tools, steps, budget)
            return AnalysisDecision(
                action="finish",
                rationale="两项查询都完成。",
                goal_checks=[
                    {
                        "goal_id": 1,
                        "status": "completed",
                        "evidence_steps": [1, 2],
                        "reason": "样本查询和背景信息均有工具证据。",
                    }
                ],
            )

    agent, gateway, _ = make_agent(matou_setup, scenario="core")
    agent.model = MisclassifiedModel(scenario="core")
    response = await agent.run("先查样本，再查询该样本的采样背景。")
    assert response.result["status"] == "completed"
    assert [name for name, _ in gateway.calls] == [ToolName.FIND_SAMPLES, ToolName.GET_SAMPLE_INFO]
    assert len(response.result["analysis_steps"]) == 2
    assert any(step.title == "切换多步骤分析" for step in response.steps)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,expected_calls,reason",
    [
        ("repeat", 1, "重复工具调用"),
        ("unavailable", 1, "不可用"),
        ("plan_error", 1, "规划未完成"),
        ("tool_error", 2, "不自动重试"),
        ("malformed_result", 2, "不自动重试"),
        ("answer_error", 3, "汇总未完成"),
        ("clarify", 1, "请选择具体样本"),
    ],
)
async def test_partial_results_survive_failure_without_retry(
    matou_setup, failure, expected_calls, reason
):
    agent, gateway, _ = make_agent(matou_setup, failure=failure)
    response = await agent.run("测试停止边界")
    assert response.result["status"] == "partial"
    assert reason in response.result["stop_reason"]
    assert len(gateway.calls) == expected_calls
    assert response.result["analysis_steps"][0]["result"]["total"] == 2
    assert any(w.code == "analysis_incomplete" for w in response.warnings)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limits,reason",
    [
        (AnalysisLimits(max_tool_calls=1), "工具调用预算"),
        (AnalysisLimits(max_plan_turns=1), "规划轮数预算"),
    ],
)
async def test_budget_terminates_loop_without_discarding_results(matou_setup, limits, reason):
    agent, gateway, _ = make_agent(matou_setup, limits=limits)
    response = await agent.run("测试调用和轮数边界")
    assert len(gateway.calls) == 1
    assert response.result["status"] == "partial"
    assert reason in response.result["stop_reason"]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["plan_timeout", "tool_timeout"])
async def test_time_budget_stops_pending_action(matou_setup, failure):
    agent, gateway, _ = make_agent(
        matou_setup, failure=failure, limits=AnalysisLimits(max_seconds=0.5)
    )
    response = await agent.run("测试时间边界")
    assert response.result["status"] == "partial"
    assert len(response.result["analysis_steps"]) == 1
    assert len(gateway.calls) <= 2
    assert response.answer and response.sources


def test_model_evidence_has_hard_budget_and_explicit_truncation():
    steps = [
        {
            "step_id": n,
            "tool_name": "find_samples",
            "result": {
                "items": [{"sample_id": f"real-{i}", "notes": "长内容" * 2000} for i in range(50)],
                "page": {"total": 1000},
            },
        }
        for n in range(12)
    ]
    evidence = bounded_evidence(steps)
    assert len(json.dumps(evidence, ensure_ascii=False)) <= 24000
    assert "truncated" in json.dumps(evidence)
    assert steps[0]["result"]["items"][0]["sample_id"] == "real-0"


def test_six_step_function_task_keeps_all_comparison_statistics():
    import copy

    families = ["PF00504", "PF03382", "PF00313"]
    summaries = [
        {
            "pfam_accession": family,
            "complete_correspondences": 318,
            "spearman_rho": 0.25,
            "median_fraction_difference": 0.001,
            "metag_iqr": 0.01,
            "metat_iqr": 0.02,
            "metag_population_cv": 0.5,
            "metat_population_cv": 0.6,
        }
        for family in families
    ]
    metadata = {
        "provenance": {
            "filters": {
                "manifest_sha256": "a" * 64,
                "study_manifest_sha256": "b" * 64,
                "source_files": {
                    str(i): {"path": "source/" * 30, "sha256": "c" * 64} for i in range(9)
                },
            }
        }
    }
    steps = [
        {
            "step_id": 0,
            "tool_name": "find_function_samples",
            "result": {"items": [{"sample_name": "original-MetaT"}] * 100, "page": {"total": 581}},
        },
        {
            "step_id": 1,
            "tool_name": "function_atlas",
            "result": {
                "sample_count": 581,
                "observations": [{"sample_name": "real"}] * 58100,
                "ranks": [{"pfam_accession": f"PF{i:05}"} for i in range(100)],
                "metadata": metadata,
            },
        },
        {
            "step_id": 2,
            "tool_name": "compare_function_signals",
            "result": {
                "matched_sampling_keys": 318,
                "summaries": summaries,
                "points": [{"sampling_key": "actual-code"}] * 954,
                "metadata": metadata,
            },
        },
        *[
            {
                "step_id": i + 3,
                "tool_name": "retrieve_gene_sequences",
                "result": {
                    "query": {"pfam_accession": family, "limit": 2},
                    "total": 4000,
                    "sequences": [
                        {"geneID": 2519, "header": "MATOU-v1.5.2519", "sequence": "ACGT" * 500}
                    ]
                    * 2,
                    "metadata": metadata,
                },
            }
            for i, family in enumerate(families)
        ],
    ]
    original = copy.deepcopy(steps)
    evidence = bounded_evidence(steps)
    assert len(json.dumps(evidence, ensure_ascii=False)) <= 24000
    assert evidence[2]["result"]["summaries"] == summaries
    assert evidence[1]["result"]["full_result_list_counts"]["ranks"] == 100
    assert evidence[1]["result"]["full_result_list_counts"]["observations"] == 58100
    assert evidence[3]["result"]["full_result_list_counts"]["sequences"] == 2
    assert evidence[2]["result"]["matched_sampling_keys"] == 318
    assert (
        evidence[2]["result"]["metadata"]["provenance"]["filters"]["study_manifest_sha256"]
        == "b" * 64
    )
    assert steps == original


@pytest.mark.anyio
async def test_finish_without_evidence_is_partial(matou_setup):
    agent, gateway, model = make_agent(matou_setup)

    async def finish(*args):
        return AnalysisDecision(action="finish", rationale="没有执行工具。")

    model.next_analysis_step = finish
    response = await agent.run("不可冒充完成")
    assert not gateway.calls
    assert response.result["status"] == "partial"
    assert "尚无工具证据" in response.answer


@pytest.mark.anyio
async def test_invalid_arguments_are_observed_and_corrected_before_any_tool_call(matou_setup):
    agent, gateway, model = make_agent(matou_setup)
    original = model.next_analysis_step
    seen_feedback = []

    async def corrected(question, context, tools, steps, budget):
        if not budget["validation_feedback"]:
            return AnalysisDecision(
                action="tool",
                rationale="查询真实样本。",
                call={
                    "tool_name": "find_function_samples",
                    "arguments": {"query": {"assay": "MetaG", "limit": 0}},
                },
            )
        seen_feedback.extend(budget["validation_feedback"])
        return await original(question, context, tools, steps, budget)

    model.next_analysis_step = corrected
    repository = RuntimeRepository()
    run = StartedRun(
        session_id=uuid4(),
        trace_id=uuid4(),
        user_message_id=uuid4(),
        assistant_message_id=uuid4(),
        started_at=datetime.now(UTC),
    )
    execution = PersistentAgentRun(
        agent=agent,
        repository=repository,
        run=run,
        question="任选一个 MetaG 样本，分析两种候选条件。",
    )
    events = [event async for event in execution.stream()]
    response = events[-1].response
    assert response.result["status"] == "completed"
    assert len(gateway.calls) == 3
    assert gateway.calls[0][1]["query"] == {"assay": "MetaG", "limit": 20}
    feedback = seen_feedback[0]
    assert feedback["kind"] == "invalid_arguments"
    assert feedback["issues"][0]["path"] == "query.limit"
    assert response.result["validation_feedback"] == [feedback]
    assert any(s.title == "检查未通过，重新规划" for s in response.steps)
    assert len(response.result["goal_checks"]) == 3
    spans = list(repository.spans.values())
    assert repository.completed and not repository.failed
    assert len([s for s in spans if s.parent_span_id is None]) == 1
    assert len([s for s in spans if s.span_kind == "tool"]) == 3
    plans = [
        s for s in spans if s.span_kind == "node" and s.attributes["langgraph_node"] == "multi_plan"
    ]
    assert len(plans) == 5
    assert any(s.output_data and s.output_data.get("replan") for s in plans)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limits,reason",
    [
        (AnalysisLimits(max_corrections=0), "纠正次数"),
        (AnalysisLimits(max_plan_turns=1), "规划轮数"),
    ],
)
async def test_correction_respects_disabled_recovery_and_planning_budget(
    matou_setup, limits, reason
):
    agent, gateway, model = make_agent(matou_setup, limits=limits)

    async def invalid(*args):
        return AnalysisDecision(
            action="tool",
            rationale="参数无效。",
            call={
                "tool_name": "find_function_samples",
                "arguments": {"query": {"assay": "MetaG", "limit": 0}},
            },
        )

    model.next_analysis_step = invalid
    response = await agent.run("测试纠正边界。")
    assert not gateway.calls
    assert response.result["status"] == "partial"
    assert reason in response.result["stop_reason"]


@pytest.mark.anyio
@pytest.mark.parametrize("repeat", [False, True])
async def test_invalid_calls_have_bounded_corrections_and_no_scientific_execution(
    matou_setup, repeat
):
    agent, gateway, model = make_agent(matou_setup)
    attempts = 0

    async def invalid(question, context, tools, steps, budget):
        nonlocal attempts
        attempts += 1
        return AnalysisDecision(
            action="tool",
            rationale="无效的查询参数。",
            call={
                "tool_name": "find_function_samples",
                "arguments": {"query": {"assay": "MetaG", "limit": 0 if repeat else -attempts}},
            },
        )

    model.next_analysis_step = invalid
    response = await agent.run("测试无进展和纠正次数。")
    assert not gateway.calls
    assert response.result["status"] == "partial"
    assert attempts == (2 if repeat else 3)
    assert ("已拒绝" if repeat else "纠正次数") in response.result["stop_reason"]
    assert len(response.result["validation_feedback"]) <= 2


@pytest.mark.anyio
async def test_domain_cross_field_validation_is_feedback_not_remote_retry(matou_setup):
    agent, gateway, model = make_agent(matou_setup, scenario="core")
    original = model.next_analysis_step

    async def corrected(question, context, tools, steps, budget):
        if not budget["validation_feedback"]:
            return AnalysisDecision(
                action="tool",
                rationale="条件不一致。",
                call={
                    "tool_name": "find_samples",
                    "arguments": {"query": {"temperature_min": 30, "temperature_max": 10}},
                },
            )
        assert "temperature_min" in budget["validation_feedback"][0]["issues"][0]["message"]
        return await original(question, context, tools, steps, budget)

    model.next_analysis_step = corrected
    response = await agent.run("任选一个 Tara 样本，查看背景。")
    assert response.result["status"] == "completed"
    assert len(gateway.calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["missing_goal", "fabricated_step", "no_evidence"])
async def test_false_completion_must_be_replanned_against_fixed_goals(matou_setup, fault):
    agent, gateway, model = make_agent(matou_setup, scenario="core")
    original = model.next_analysis_step

    async def checked(question, context, tools, steps, budget):
        if len(steps) == 2 and not budget["validation_feedback"]:
            checks = [
                {"goal_id": 1, "status": "completed", "evidence_steps": [1], "reason": "有查询。"},
                {"goal_id": 2, "status": "completed", "evidence_steps": [2], "reason": "有背景。"},
            ]
            if fault == "missing_goal":
                checks.pop()
            else:
                checks[1]["evidence_steps"] = [999] if fault == "fabricated_step" else []
            return AnalysisDecision(
                action="finish", rationale="检查未通过的完成声明。", goal_checks=checks
            )
        return await original(question, context, tools, steps, budget)

    model.next_analysis_step = checked
    response = await agent.run("任选一个 Tara 样本，查看背景。")
    assert response.result["status"] == "completed"
    assert len(gateway.calls) == 2
    assert response.result["validation_feedback"][0]["kind"] == "invalid_completion"
    assert [c["evidence_steps"] for c in response.result["goal_checks"]] == [[1], [2]]


@pytest.mark.anyio
async def test_blocked_goal_keeps_independent_completed_result_as_partial(matou_setup):
    agent, gateway, model = make_agent(matou_setup, scenario="core")
    original = model.next_analysis_step

    async def partial(question, context, tools, steps, budget):
        if not steps:
            return await original(question, context, tools, steps, budget)
        return AnalysisDecision(
            action="finish",
            rationale="独立查询完成，另一个目标缺少可靠映射。",
            goal_checks=[
                {"goal_id": 1, "status": "completed", "evidence_steps": [1], "reason": "有查询。"},
                {"goal_id": 2, "status": "blocked", "reason": "缺少可靠样本映射。"},
            ],
        )

    model.next_analysis_step = partial
    response = await agent.run("测试前提只阻断对应目标。")
    assert len(gateway.calls) == 1
    assert response.result["status"] == "partial"
    assert "可靠样本映射" in response.result["stop_reason"]
    assert response.result["analysis_steps"][0]["result"]["items"]
    assert response.result["goal_checks"][1]["status"] == "blocked"
