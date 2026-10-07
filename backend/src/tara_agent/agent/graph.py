"""带统一请求路由的 Tara Agent LangGraph 工作流。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Literal, TypedDict

from jsonschema.exceptions import SchemaError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from tara_agent.agent.charts import build_charts
from tara_agent.agent.conversation import (
    conversation_context_payload,
    filter_analysis_references,
    planning_context,
)
from tara_agent.agent.gateway import AgentToolError, ToolGateway
from tara_agent.agent.models import (
    AgentResponse,
    AgentStep,
    AgentStreamEvent,
    ChartSpec,
    ConversationContext,
    ModelUsage,
    RouteDecision,
    RouteKind,
    ToolDefinition,
    ToolName,
    ToolPlan,
    ToolTrace,
)
from tara_agent.agent.multistep import (
    AnalysisDecision,
    AnalysisLimits,
    WorkflowSelection,
    argument_issues,
    bounded_evidence,
    completion_issue,
    validate_result,
)
from tara_agent.agent.provider import AgentModel, AgentModelError, MultiToolPlanRequired
from tara_agent.agent.resources import (
    ANSWER_PROMPT_RESOURCE,
    CAPABILITY_PROFILE,
    CAPABILITY_RESOURCE,
    MULTI_PLANNER_PROMPT_RESOURCE,
    PLANNER_PROMPT_RESOURCE,
    ROUTER_PROMPT_RESOURCE,
    SELECTOR_PROMPT_RESOURCE,
    WORKFLOW_RESOURCE,
    AgentResource,
    tool_resources,
)
from tara_agent.agent.workflow import (
    WorkflowNodeDefinition,
    WorkflowTaskEvent,
    bind_workflow_trace,
)
from tara_agent.data.catalog import source_filename
from tara_agent.domain.contracts import ResultWarning
from tara_agent.observability.contracts import ObservationKind, ObservationUpdate
from tara_agent.observability.execution import TraceObservationEvent, observe


class AgentState(TypedDict, total=False):
    question: str
    context: ConversationContext
    route: RouteDecision
    tools: list[ToolDefinition]
    plan: ToolPlan
    result: dict[str, Any]
    reasoning: str
    answer: str
    charts: list[ChartSpec]
    steps: list[AgentStep]
    answer_usage: ModelUsage
    workflow: WorkflowSelection
    analysis_steps: list[dict]
    decision: AnalysisDecision
    deadline: float
    plan_turns: int
    stop_message: str
    feedback: list[dict]
    rejected_calls: list[str]
    replan: bool


ROUTE_NODE = WorkflowNodeDefinition("route", "识别请求与能力")
UNDERSTAND_NODE = WorkflowNodeDefinition("understand", "理解问题并选择工具")
EXECUTE_NODE = WorkflowNodeDefinition("execute", "执行科学分析")
ANSWER_NODE = WorkflowNodeDefinition("answer", "组织答案")
RESPOND_NODE = WorkflowNodeDefinition("respond", "回应请求")
SELECT_NODE = WorkflowNodeDefinition("select_workflow", "选择分析工作流")
MULTI_PLAN_NODE = WorkflowNodeDefinition("multi_plan", "检查证据并规划下一步")
MULTI_EXECUTE_NODE = WorkflowNodeDefinition("multi_execute", "执行多步骤分析工具")
MULTI_ANSWER_NODE = WorkflowNodeDefinition("multi_answer", "汇总多步骤分析")


class TaraAgent:
    """先路由用户请求，再执行受约束的回答或科学分析。"""

    workflow_name = "tara_agent_chat"
    workflow_title = "Tara Agent 工作流"

    def __init__(
        self, model: AgentModel, gateway: ToolGateway, *, limits: AnalysisLimits | None = None
    ) -> None:
        self.model = model
        self.gateway = gateway
        self.limits = limits or AnalysisLimits()
        self.node_definitions = {
            item.name: item
            for item in (
                ROUTE_NODE,
                UNDERSTAND_NODE,
                EXECUTE_NODE,
                ANSWER_NODE,
                RESPOND_NODE,
                SELECT_NODE,
                MULTI_PLAN_NODE,
                MULTI_EXECUTE_NODE,
                MULTI_ANSWER_NODE,
            )
        }
        self.graph = self._build_graph()

    @property
    def shared_resource_snapshot(self) -> list[dict[str, str]]:
        return [
            WORKFLOW_RESOURCE.trace_data(),
            ROUTER_PROMPT_RESOURCE.trace_data(),
            PLANNER_PROMPT_RESOURCE.trace_data(),
            ANSWER_PROMPT_RESOURCE.trace_data(),
            CAPABILITY_RESOURCE.trace_data(),
            SELECTOR_PROMPT_RESOURCE.trace_data(),
            MULTI_PLANNER_PROMPT_RESOURCE.trace_data(),
        ]

    async def run(
        self,
        question: str,
        context: ConversationContext | None = None,
    ) -> AgentResponse:
        state = await self.graph.ainvoke(
            self._initial_state(question, context),
            config={"recursion_limit": 2 * self.limits.max_plan_turns + 10},
        )
        return self._response(state)

    async def stream(
        self,
        question: str,
        context: ConversationContext | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        async for event in self.stream_execution(question, context):
            if isinstance(event, AgentStreamEvent):
                yield event

    async def stream_execution(
        self,
        question: str,
        context: ConversationContext | None = None,
    ) -> AsyncIterator[AgentStreamEvent | WorkflowTaskEvent | TraceObservationEvent]:
        """同时发送聊天事件和仅供持久化运行器使用的节点事件。"""

        state = self._initial_state(question, context)
        sent_steps: set[tuple] = set()
        async for part in self.graph.astream(
            state,
            stream_mode=["updates", "custom", "tasks"],
            version="v2",
            config={"recursion_limit": 2 * self.limits.max_plan_turns + 10},
        ):
            if part["type"] == "tasks":
                yield self._task_event(part["data"], part["ns"])
                continue
            if part["type"] == "custom":
                if part["data"].get("event") == "trace_observation":
                    yield TraceObservationEvent.model_validate(part["data"])
                    continue
                event = AgentStreamEvent.model_validate(part["data"])
                if event.step is not None:
                    sent_steps.add((event.step.stage, event.step.title, event.step.detail))
                yield event
                continue

            if part["type"] != "updates":
                continue
            for values in part["data"].values():
                state.update(values)
                steps = values.get("steps", [])
                if not steps:
                    continue
                key = (steps[-1].stage, steps[-1].title, steps[-1].detail)
                if key in sent_steps:
                    continue
                sent_steps.add(key)
                yield AgentStreamEvent(event="step", step=steps[-1])
        yield AgentStreamEvent(event="complete", response=self._response(state))

    def _task_event(
        self,
        data: dict[str, Any],
        namespace: tuple[str, ...],
    ) -> WorkflowTaskEvent:
        node_name = str(data["name"])
        definition = self.node_definitions.get(
            node_name,
            WorkflowNodeDefinition(node_name, node_name),
        )
        if "input" in data:
            phase = "started"
        elif data.get("error") is not None:
            phase = "failed"
        else:
            phase = "completed"
        error = data.get("error")
        return WorkflowTaskEvent(
            task_id=str(data["id"]),
            node_name=node_name,
            node_title=definition.title,
            phase=phase,
            namespace=tuple(namespace),
            input_data=data.get("input"),
            output_data=data.get("result"),
            error=str(error) if error is not None else None,
        )

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node(ROUTE_NODE.name, self._route)
        graph.add_node(UNDERSTAND_NODE.name, self._understand)
        graph.add_node(EXECUTE_NODE.name, self._execute)
        graph.add_node(ANSWER_NODE.name, self._answer)
        graph.add_node(RESPOND_NODE.name, self._respond)
        graph.add_node(SELECT_NODE.name, self._select_workflow)
        graph.add_node(MULTI_PLAN_NODE.name, self._multi_plan)
        graph.add_node(MULTI_EXECUTE_NODE.name, self._multi_execute)
        graph.add_node(MULTI_ANSWER_NODE.name, self._multi_answer)
        graph.add_edge(START, ROUTE_NODE.name)
        graph.add_conditional_edges(
            ROUTE_NODE.name,
            self._route_target,
            {
                "analysis": SELECT_NODE.name,
                "respond": RESPOND_NODE.name,
            },
        )
        graph.add_conditional_edges(
            SELECT_NODE.name,
            self._workflow_target,
            {
                "single_step": UNDERSTAND_NODE.name,
                "multi_step": MULTI_PLAN_NODE.name,
            },
        )
        graph.add_conditional_edges(
            MULTI_PLAN_NODE.name,
            self._multi_target,
            {
                "tool": MULTI_EXECUTE_NODE.name,
                "replan": MULTI_PLAN_NODE.name,
                "done": MULTI_ANSWER_NODE.name,
            },
        )
        graph.add_conditional_edges(
            MULTI_EXECUTE_NODE.name,
            self._multi_execute_target,
            {
                "continue": MULTI_PLAN_NODE.name,
                "done": MULTI_ANSWER_NODE.name,
            },
        )
        graph.add_edge(MULTI_ANSWER_NODE.name, END)
        graph.add_conditional_edges(
            UNDERSTAND_NODE.name,
            self._understand_target,
            {
                "single_step": EXECUTE_NODE.name,
                "multi_step": MULTI_PLAN_NODE.name,
            },
        )
        graph.add_edge(EXECUTE_NODE.name, ANSWER_NODE.name)
        graph.add_edge(ANSWER_NODE.name, END)
        graph.add_edge(RESPOND_NODE.name, END)
        return graph.compile()

    async def _select_workflow(self, state, runtime):
        selector = getattr(self.model, "select_workflow", None)
        if selector is None:
            return {
                "workflow": WorkflowSelection(
                    workflow="single_step", rationale="模型仅提供单步接口"
                )
            }
        deadline = time.monotonic() + self.limits.max_seconds
        with (
            bind_workflow_trace(runtime),
            observe(
                "选择分析执行方式",
                ObservationKind.LLM,
                ObservationUpdate(
                    input_data={
                        "question": state["question"],
                        "context": conversation_context_payload(
                            planning_context(state["context"], state["route"])
                        ),
                        "tools": [tool.model_dump(mode="json") for tool in state["tools"]],
                    },
                    attributes={
                        "resources": [
                            SELECTOR_PROMPT_RESOURCE.trace_data(),
                            WORKFLOW_RESOURCE.trace_data(),
                        ]
                    },
                    **self._model_details("workflow_selector"),
                ),
            ) as observation,
        ):
            selection = WorkflowSelection.model_validate(
                await asyncio.wait_for(
                    selector(
                        state["question"],
                        planning_context(state["context"], state["route"]),
                        state["tools"],
                    ),
                    timeout=self.limits.max_seconds,
                )
            )
            if selection.workflow == "multi_step" and not hasattr(self.model, "next_analysis_step"):
                raise ValueError("模型未提供多步骤规划接口")
            observation.finish(
                ObservationUpdate(
                    output_data=selection.model_dump(mode="json"), **_usage_details(selection.usage)
                )
            )
        workflow_label = (
            "多步骤科研分析" if selection.workflow == "multi_step" else "单步查询与计算"
        )
        step = AgentStep(
            stage="understand",
            title="选择分析工作流",
            detail=f"{workflow_label}：{selection.rationale}",
        )
        return {
            "workflow": selection,
            "analysis_steps": [],
            "plan_turns": 0,
            "feedback": [],
            "rejected_calls": [],
            "deadline": deadline,
            "steps": [*state["steps"], step],
        }

    def _workflow_target(self, state):
        return state["workflow"].workflow

    def _stop_multi(self, state, message):
        return {
            "stop_message": message,
            "decision": AnalysisDecision(
                action="stop",
                rationale=message[:300],
                message=message[:2000],
            ),
        }

    def _correct_multi(self, state, decision, issues, *, call_key=None):
        """只纠正执行前契约错误；反馈不是成功的科学证据，也不消耗工具调用。"""
        if len(state["feedback"]) >= self.limits.max_corrections:
            return self._stop_multi(state, "契约纠正次数已用尽；保留已完成结果。")
        feedback = {
            "plan_turn": state["plan_turns"] + 1,
            "kind": "invalid_arguments" if call_key else "invalid_completion",
            "issues": issues,
            "tool_name": decision.call.tool_name.value if decision.call else None,
        }
        if decision.call:
            feedback["arguments"] = bounded_evidence(
                [{"arguments": decision.call.arguments}], max_chars=2000
            )[0].get("arguments", {"truncated": True})
        step = AgentStep(
            stage="understand", title="检查未通过，重新规划", detail=issues[0]["message"]
        )
        return {
            "decision": decision,
            "replan": True,
            "plan_turns": state["plan_turns"] + 1,
            "feedback": [*state["feedback"], feedback],
            "rejected_calls": [*state["rejected_calls"], *([call_key] if call_key else [])],
            "steps": [*state["steps"], step],
        }

    async def _multi_plan(self, state, runtime):
        remaining = state["deadline"] - time.monotonic()
        if remaining <= 0 or state["plan_turns"] >= self.limits.max_plan_turns:
            return self._stop_multi(state, "分析时间或规划轮数预算已用尽；保留已完成结果。")
        budget = {
            "tool_calls": self.limits.max_tool_calls - len(state["analysis_steps"]),
            "plan_turns": self.limits.max_plan_turns - state["plan_turns"],
            "seconds": remaining,
            "corrections": self.limits.max_corrections - len(state["feedback"]),
            "goals": [
                {"goal_id": i, "description": goal}
                for i, goal in enumerate(state["workflow"].goals, 1)
            ],
            "validation_feedback": state["feedback"],
        }
        try:
            with (
                bind_workflow_trace(runtime),
                observe(
                    "检查分析证据并决定下一步",
                    ObservationKind.LLM,
                    ObservationUpdate(
                        input_data={
                            "question": state["question"],
                            "context": conversation_context_payload(
                                planning_context(state["context"], state["route"])
                            ),
                            "tools": [tool.model_dump(mode="json") for tool in state["tools"]],
                            "verified_steps": bounded_evidence(state["analysis_steps"]),
                            "remaining_budget": budget,
                        },
                        attributes={"resources": [MULTI_PLANNER_PROMPT_RESOURCE.trace_data()]},
                        **self._model_details("multi_planner"),
                    ),
                ) as observation,
            ):
                decision = AnalysisDecision.model_validate(
                    await asyncio.wait_for(
                        self.model.next_analysis_step(
                            state["question"],
                            planning_context(state["context"], state["route"]),
                            state["tools"],
                            state["analysis_steps"],
                            budget,
                        ),
                        timeout=remaining,
                    )
                )
                observation.finish(
                    ObservationUpdate(
                        output_data=decision.model_dump(mode="json"),
                        **_usage_details(decision.usage),
                    )
                )
        except (TimeoutError, AgentModelError, ValueError) as error:
            return self._stop_multi(
                state, f"下一步规划未完成，已停止并保留已完成结果：{error or type(error).__name__}"
            )
        if decision.action == "tool":
            if len(state["analysis_steps"]) >= self.limits.max_tool_calls:
                return self._stop_multi(state, "工具调用预算已用尽；保留已完成结果。")
            if decision.call.tool_name not in {t.name for t in state["tools"]}:
                return self._stop_multi(state, "计划请求了当前不可用的工具，已停止。")
            key = json.dumps(decision.call.model_dump(mode="json"), sort_keys=True)
            if any(key == step["call_key"] for step in state["analysis_steps"]):
                return self._stop_multi(state, "检测到重复工具调用，没有新的分析进展，已停止。")
            if key in state["rejected_calls"]:
                return self._stop_multi(state, "重复提交已拒绝的参数，没有纠正进展，已停止。")
            tool = next(t for t in state["tools"] if t.name == decision.call.tool_name)
            # Schema 错误属于工具契约故障，不能当成模型参数错误自动纠正。
            try:
                issues = argument_issues(tool, decision.call.arguments)
            except (ValueError, SchemaError) as error:
                return self._stop_multi(state, f"工具输入契约不可用，已停止：{error}")
            if issues:
                return self._correct_multi(state, decision, issues, call_key=key)
        if decision.action == "finish" and not state["analysis_steps"]:
            return self._stop_multi(state, "尚无工具证据，不能声称分析完成。")
        if decision.action == "finish":
            issue = completion_issue(
                state["workflow"].goals, decision.goal_checks, state["analysis_steps"]
            )
            if not set(decision.final_result_steps) <= {
                r["step_id"] for r in state["analysis_steps"]
            }:
                issue = "final_result_steps只能引用真实成功步骤，不能编造编号。"
            if issue:
                return self._correct_multi(
                    state, decision, [{"path": "goal_checks", "message": issue}]
                )
        updates = {"decision": decision, "replan": False, "plan_turns": state["plan_turns"] + 1}
        if decision.action in ("stop", "clarify"):
            updates["stop_message"] = decision.message
        step = AgentStep(
            stage="understand",
            title=f"规划第 {state['plan_turns'] + 1} 轮分析",
            detail=decision.rationale,
        )
        updates["steps"] = [*state["steps"], step]
        return updates

    def _multi_target(self, state):
        if state.get("replan") and not state.get("stop_message"):
            return "replan"
        return "tool" if state["decision"].action == "tool" else "done"

    async def _multi_execute(self, state, runtime):
        call = state["decision"].call
        plan = ToolPlan(
            tool_name=call.tool_name,
            arguments=call.arguments,
            rationale=state["decision"].rationale,
            usage=state["decision"].usage,
        )
        remaining = state["deadline"] - time.monotonic()
        if remaining <= 0:
            return self._stop_multi(state, "分析时间预算已用尽；保留已完成结果。")
        try:
            with (
                bind_workflow_trace(runtime),
                observe(
                    f"调用 MCP 工具 {plan.tool_name.value}",
                    ObservationKind.TOOL,
                    ObservationUpdate(
                        input_data=plan.arguments,
                        tool_name=plan.tool_name.value,
                        attributes={
                            "resources": [
                                _selected_tool_resource(state["tools"], plan).trace_data()
                            ]
                        },
                    ),
                ) as observation,
            ):
                result = validate_result(
                    plan.tool_name,
                    await asyncio.wait_for(
                        self.gateway.call(plan.tool_name, plan.arguments),
                        timeout=remaining,
                    ),
                )
                observation.finish(_tool_result_details(plan, result))
        except (AgentToolError, ValueError, TimeoutError) as error:
            return self._stop_multi(
                state, f"步骤未完成，已停止且不自动重试：{error or type(error).__name__}"
            )
        record = {
            "step_id": len(state["analysis_steps"]) + 1,
            "tool_name": plan.tool_name.value,
            "arguments": plan.arguments,
            "result": result,
            "call_key": json.dumps(call.model_dump(mode="json"), sort_keys=True),
        }
        step = AgentStep(
            stage="execute",
            title=f"完成分析步骤 {record['step_id']}",
            detail=f"已验证 {plan.tool_name.value} 的结果结构与来源信息。",
        )
        return {
            "plan": plan,
            "analysis_steps": [*state["analysis_steps"], record],
            "steps": [*state["steps"], step],
        }

    def _multi_execute_target(self, state):
        return "done" if state.get("stop_message") else "continue"

    async def _multi_answer(self, state, runtime):
        records = state["analysis_steps"]
        stop_message = state.get("stop_message")
        checks = state["decision"].goal_checks if state["decision"].action == "finish" else []
        incomplete_sections = [
            section
            for record in records
            if record.get("tool_name") in {ToolName.FUNCTION_STUDY, ToolName.COMMUNITY_ANALYSIS}
            for section in record["result"].get("sections", [])
            if section["status"] == "blocked"
        ]
        if incomplete_sections:
            stop_message = "；".join(
                f"{s['output']}未完成：{s['reason']}" for s in incomplete_sections
            )
        incomplete_results = [
            r for r in records if r["result"].get("status") in {"partial", "blocked"}
        ]
        if incomplete_results:
            stop_message = stop_message or "部分科学结果尚未完成，请查看对应项目状态和原因。"
        blocked = [check for check in checks if check.status == "blocked"]
        if blocked:
            stop_message = "；".join(
                f"目标 {check.goal_id} 未完成：{check.reason}" for check in blocked
            )
        if time.monotonic() >= state["deadline"]:
            stop_message = stop_message or "分析时间预算已用尽；已完成的工具结果保留在下方。"
        warnings = []
        sources = []
        for record in records:
            metadata = record["result"].get("metadata", {})
            warnings.extend(metadata.get("warnings", []))
            sources.extend(metadata.get("provenance", {}).get("source_datasets", []))
        if stop_message:
            warnings.append({"code": "analysis_incomplete", "message": stop_message})
        final_steps = set(state["decision"].final_result_steps)
        labelled_records = [
            {
                **{k: v for k, v in r.items() if k != "call_key"},
                "result_role": "final" if r["step_id"] in final_steps else "intermediate",
            }
            for r in records
        ]
        result = {
            "workflow": "multi_step",
            "status": "partial" if stop_message else "completed",
            "stop_reason": stop_message,
            "completion_reason": state["decision"].rationale,
            "goals": state["workflow"].goals,
            "goal_checks": [check.model_dump(mode="json") for check in checks],
            "validation_feedback": state["feedback"],
            "analysis_steps": labelled_records,
            "budgets": self.limits.model_dump(),
            "metadata": {
                "provenance": {"source_datasets": list(dict.fromkeys(sources))},
                "warnings": list({(w["code"], w["message"]): w for w in warnings}.values()),
            },
        }
        charts = []
        for record in labelled_records:
            for chart in build_charts(ToolName(record["tool_name"]), record["result"]):
                charts.append(
                    chart.model_copy(
                        update={
                            "title": f"步骤 {record['step_id']} · {chart.title}",
                            "result_role": record["result_role"],
                        }
                    )
                )
        if not records or time.monotonic() >= state["deadline"]:
            answer = "【最终结果（部分完成）】\n\n" + (stop_message or "分析未获得工具结果。")
            runtime.stream_writer(
                AgentStreamEvent(event="answer_delta", delta=answer).model_dump(mode="json")
            )
            return {"result": result, "answer": answer, "reasoning": "", "charts": charts}
        # 汇总只解释已有计算，禁止模型生成新的统计数值。
        updated = dict(state, result=result)
        try:
            response = await asyncio.wait_for(
                self._answer(updated, runtime),
                timeout=max(0.001, state["deadline"] - time.monotonic()),
            )
        except (TimeoutError, AgentModelError, ValueError) as error:
            message = (
                "【最终结果（部分完成）】\n\n"
                f"回答汇总未完成；已完成的工具结果保留在下方。原因：{error or type(error).__name__}"
            )
            result["status"] = "partial"
            result["stop_reason"] = message
            result["metadata"]["warnings"].append(
                {"code": "analysis_incomplete", "message": message}
            )
            runtime.stream_writer(
                AgentStreamEvent(event="answer_delta", delta=message).model_dump(mode="json")
            )
            response = {"answer": message, "reasoning": "", "charts": []}
        return {**response, "result": result, "charts": charts}

    def _initial_state(
        self,
        question: str,
        context: ConversationContext | None,
    ) -> AgentState:
        return {
            "question": question,
            "context": context or ConversationContext(),
            "steps": [],
        }

    async def _route(self, state: AgentState, runtime: Runtime) -> AgentState:
        tools = await self.gateway.list_tools()
        resources = [
            ROUTER_PROMPT_RESOURCE.trace_data(),
            CAPABILITY_RESOURCE.trace_data(),
            *[resource.trace_data() for resource in tool_resources(tools)],
        ]
        with (
            bind_workflow_trace(runtime),
            observe(
                "判断请求处理方式",
                ObservationKind.LLM,
                ObservationUpdate(
                    input_data={
                        "question": state["question"],
                        "conversation_context": conversation_context_payload(state["context"]),
                        "capability_profile": CAPABILITY_PROFILE,
                        "available_tools": [
                            {"name": tool.name.value, "description": tool.description}
                            for tool in tools
                        ],
                    },
                    attributes={"resources": resources},
                    **self._model_details("router"),
                ),
            ) as observation,
        ):
            decision = await self.model.route(
                state["question"],
                state["context"],
                tools,
            )
            decision = filter_analysis_references(state["context"], decision)
            observation.finish(
                ObservationUpdate(
                    output_data=decision.model_dump(mode="json", exclude={"usage"}),
                    **_usage_details(decision.usage),
                )
            )

        step = AgentStep(
            stage="route",
            title="识别请求类型",
            detail=_route_step_detail(decision),
        )
        return {
            "route": decision,
            "tools": tools,
            "steps": [*state.get("steps", []), step],
        }

    def _route_target(self, state: AgentState) -> Literal["analysis", "respond"]:
        if state["route"].kind is RouteKind.ANALYSIS:
            return "analysis"
        return "respond"

    async def _understand(self, state: AgentState, runtime: Runtime) -> AgentState:
        context = planning_context(state["context"], state["route"])
        with bind_workflow_trace(runtime):
            tools = state["tools"]
            with observe(
                "选择分析工具",
                ObservationKind.LLM,
                ObservationUpdate(
                    input_data={
                        "question": state["question"],
                        "conversation_context": conversation_context_payload(context),
                        "available_tools": [tool.model_dump(mode="json") for tool in tools],
                    },
                    attributes=_planning_attributes(state["route"], tools),
                    **self._model_details("planner"),
                ),
            ) as observation:
                try:
                    plan = await self.model.plan(state["question"], context, tools)
                except MultiToolPlanRequired as exc:
                    if not hasattr(self.model, "next_analysis_step"):
                        raise AgentModelError("本次请求包含多项分析，请分别提交查询。") from exc
                    # 没有执行任何候选调用；重新规划完整用户目标，不丢弃其中的查询。
                    observation.finish(
                        ObservationUpdate(
                            output_data={
                                "workflow": "multi_step",
                                "reason": "单步规划返回多个工具",
                            },
                            **_usage_details(exc.usage),
                        )
                    )
                    step = AgentStep(
                        stage="understand",
                        title="切换多步骤分析",
                        detail="本次包含多项查询，将逐项执行并核对完整目标。",
                    )
                    return {
                        "workflow": WorkflowSelection(
                            workflow="multi_step",
                            rationale="单步规划需要多个调用",
                            goals=[
                                "完成当前用户问题要求的全部查询与比较，保留各项样本、标记和统计条件"
                            ],
                        ),
                        "analysis_steps": [],
                        "plan_turns": 1,
                        "feedback": [],
                        "rejected_calls": [],
                        "deadline": state.get(
                            "deadline", time.monotonic() + self.limits.max_seconds
                        ),
                        "steps": [*state.get("steps", []), step],
                    }
                observation.finish(
                    ObservationUpdate(
                        output_data=plan.model_dump(mode="json", exclude={"usage"}),
                        **_usage_details(plan.usage),
                    )
                )
        step = AgentStep(
            stage="understand",
            title="理解问题并选择工具",
            detail=f"选择 {plan.tool_name.value}：{plan.rationale}",
        )
        return {"plan": plan, "steps": [*state.get("steps", []), step]}

    def _understand_target(self, state):
        return state["workflow"].workflow

    async def _execute(self, state: AgentState, runtime: Runtime) -> AgentState:
        with bind_workflow_trace(runtime):
            plan = state["plan"]
            with observe(
                f"调用 MCP 工具 {plan.tool_name.value}",
                ObservationKind.TOOL,
                ObservationUpdate(
                    input_data=plan.arguments,
                    tool_name=plan.tool_name.value,
                    attributes={
                        "resources": [_selected_tool_resource(state["tools"], plan).trace_data()]
                    },
                ),
            ) as observation:
                result = await self.gateway.call(plan.tool_name, plan.arguments)
                observation.finish(_tool_result_details(plan, result))
        step = AgentStep(
            stage="execute",
            title="执行科学分析",
            detail=f"已通过 MCP 调用白名单工具 {plan.tool_name.value}。",
        )
        return {"result": result, "steps": [*state.get("steps", []), step]}

    async def _answer(self, state: AgentState, runtime: Runtime) -> AgentState:
        writer = runtime.stream_writer
        step = AgentStep(
            stage="answer",
            title="组织可追踪答案",
            detail="根据工具结果生成回答、警告、来源和图表数据。",
        )
        writer(AgentStreamEvent(event="step", step=step).model_dump(mode="json"))

        reasoning_parts: list[str] = []
        heading = (
            "【最终结果（部分完成）】\n\n"
            if state["result"].get("status") in {"partial", "blocked"}
            else "【最终结果】\n\n"
        )
        answer_parts: list[str] = [heading]
        writer(AgentStreamEvent(event="answer_delta", delta=heading).model_dump(mode="json"))
        answer_usage = None
        with (
            bind_workflow_trace(runtime),
            observe(
                "生成分析回答",
                ObservationKind.LLM,
                ObservationUpdate(
                    input_data={
                        "question": state["question"],
                        "plan": state["plan"].model_dump(mode="json", exclude={"usage"}),
                        "tool_result": state["result"],
                    },
                    attributes={"resources": [ANSWER_PROMPT_RESOURCE.trace_data()]},
                    **self._model_details("answer"),
                ),
            ) as observation,
        ):
            async for delta in self.model.stream_answer(
                state["question"],
                state["plan"],
                state["result"],
            ):
                if delta.kind == "usage":
                    answer_usage = delta.usage
                    continue
                if delta.kind == "reasoning":
                    reasoning_parts.append(delta.content)
                    event = AgentStreamEvent(event="reasoning_delta", delta=delta.content)
                else:
                    answer_parts.append(delta.content)
                    event = AgentStreamEvent(event="answer_delta", delta=delta.content)
                writer(event.model_dump(mode="json"))

            reasoning = "".join(reasoning_parts).strip()
            answer = "".join(answer_parts).strip()
            observation.finish(
                ObservationUpdate(
                    output_data={"reasoning": reasoning, "answer": answer},
                    **_usage_details(answer_usage),
                )
            )

        charts = build_charts(state["plan"].tool_name, state["result"])
        return {
            "reasoning": reasoning,
            "answer": answer,
            "charts": charts,
            "answer_usage": answer_usage,
            "steps": [*state.get("steps", []), step],
        }

    async def _respond(self, state: AgentState, runtime: Runtime) -> AgentState:
        decision = state["route"]
        answer = decision.response or ""
        step = AgentStep(
            stage="respond",
            title=_response_step_title(decision.kind),
            detail=_response_step_detail(decision.kind),
        )
        runtime.stream_writer(AgentStreamEvent(event="step", step=step).model_dump(mode="json"))
        runtime.stream_writer(
            AgentStreamEvent(event="answer_delta", delta=answer).model_dump(mode="json")
        )
        return {
            "reasoning": "",
            "answer": answer,
            "result": {},
            "charts": [],
            "steps": [*state.get("steps", []), step],
        }

    def _response(self, state: AgentState) -> AgentResponse:
        route = state["route"]
        if route.kind is not RouteKind.ANALYSIS:
            return AgentResponse(
                question=state["question"],
                reasoning="",
                answer=state["answer"],
                model=self.model.name,
                route=route,
                steps=state["steps"],
                result={},
                charts=[],
                warnings=[],
                sources=[],
            )

        result = state["result"]
        plan = state.get("plan")
        metadata = result.get("metadata", {})
        provenance = metadata.get("provenance", {})
        warnings = [ResultWarning.model_validate(item) for item in metadata.get("warnings", [])]
        return AgentResponse(
            question=state["question"],
            reasoning=state.get("reasoning", ""),
            answer=state["answer"],
            model=self.model.name,
            route=route,
            tool=ToolTrace(
                name=plan.tool_name,
                arguments=plan.arguments,
                summary=plan.rationale,
                usage=plan.usage,
            )
            if plan is not None and result.get("workflow") != "multi_step"
            else None,
            steps=state["steps"],
            result=result,
            charts=state.get("charts", []),
            warnings=warnings,
            sources=[source_filename(str(item)) for item in provenance.get("source_datasets", [])],
            answer_usage=state.get("answer_usage"),
        )

    def _model_details(self, phase: str) -> dict[str, Any]:
        parameters = dict(getattr(self.model, "trace_parameters", {}))
        phase_parameters = parameters.get(phase, {})
        return {
            "model_provider": str(getattr(self.model, "provider", "unknown")),
            "model_name": self.model.name,
            "model_parameters": (phase_parameters if isinstance(phase_parameters, dict) else {}),
        }


def _usage_details(usage: ModelUsage | None) -> dict[str, int | None]:
    if usage is None:
        return {}
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _route_step_detail(decision: RouteDecision) -> str:
    labels = {
        RouteKind.DIRECT_ANSWER: "依据系统能力或已有分析回答。",
        RouteKind.ANALYSIS: "进入当前可用的数据分析流程。",
        RouteKind.CLARIFY: "需要补充关键信息后再继续。",
        RouteKind.UNSUPPORTED: "当前能力无法可靠完成该请求。",
    }
    return labels[decision.kind]


def _planning_attributes(
    decision: RouteDecision,
    tools: list[ToolDefinition],
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "resources": [
            PLANNER_PROMPT_RESOURCE.trace_data(),
            *[resource.trace_data() for resource in tool_resources(tools)],
        ]
    }
    if decision.analysis_reference_ids:
        attributes["analysis_reference_ids"] = [
            str(trace_id) for trace_id in decision.analysis_reference_ids
        ]
    return attributes


def _response_step_title(kind: RouteKind) -> str:
    if kind is RouteKind.CLARIFY:
        return "请求补充信息"
    if kind is RouteKind.UNSUPPORTED:
        return "说明能力边界"
    return "回答问题"


def _response_step_detail(kind: RouteKind) -> str:
    if kind is RouteKind.CLARIFY:
        return "提出一个继续处理所需的具体问题。"
    if kind is RouteKind.UNSUPPORTED:
        return "说明当前限制和可用能力。"
    return "依据系统能力或已有分析摘要直接回答。"


def _tool_result_details(plan: ToolPlan, result: dict[str, Any]) -> ObservationUpdate:
    metadata = result.get("metadata")
    provenance = metadata.get("provenance") if isinstance(metadata, dict) else None
    provenance = provenance if isinstance(provenance, dict) else {}
    marker = provenance.get("marker")
    source_datasets = provenance.get("source_datasets", [])
    filters = provenance.get("filters")
    return ObservationUpdate(
        output_data=result,
        tool_name=plan.tool_name.value,
        filters=filters if isinstance(filters, dict) else None,
        marker=str(marker) if marker is not None else None,
        sample_count=(
            provenance["sample_count"] if isinstance(provenance.get("sample_count"), int) else None
        ),
        data_sources=[{"filename": source_filename(str(dataset))} for dataset in source_datasets],
    )


def _selected_tool_resource(
    tools: list[ToolDefinition],
    plan: ToolPlan,
) -> AgentResource:
    resources = tool_resources(tools)
    for tool, resource in zip(tools, resources, strict=True):
        if tool.name is plan.tool_name:
            return resource
    raise ValueError(f"工具资源不存在: {plan.tool_name.value}")
