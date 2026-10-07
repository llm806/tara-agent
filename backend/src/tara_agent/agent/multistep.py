"""多步骤分析的受限决定、结果验证及模型上下文；不实现科学算法。"""

import json
from itertools import islice
from typing import Literal

from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tara_agent.agent.context import build_result_summary
from tara_agent.agent.models import ModelUsage, ToolDefinition, ToolName
from tara_agent.analysis.community_models import (
    CommunityQuery,
    CommunityResult,
    FunctionEnvironmentQuery,
    FunctionEnvironmentResult,
)
from tara_agent.analysis.compute_models import (
    DiversityQuery,
    DiversityResult,
    EnvironmentAssociationQuery,
    EnvironmentAssociationResult,
    TaxonAbundanceQuery,
    TaxonAbundanceResult,
)
from tara_agent.analysis.function_atlas_models import (
    AtlasQuery,
    AtlasResult,
    ComparisonResult,
    FunctionComparisonQuery,
    SequenceQuery,
    SequenceResult,
)
from tara_agent.analysis.function_query_models import (
    FunctionProfileQuery,
    FunctionSamplesQuery,
    FunctionSamplesResult,
)
from tara_agent.analysis.function_signal_models import FunctionSignalResult
from tara_agent.analysis.function_study_models import FunctionStudyQuery, FunctionStudyResult
from tara_agent.analysis.models import (
    FindSamplesQuery,
    FindSamplesResult,
    FindTaxaQuery,
    FindTaxaResult,
    SampleInfoResult,
)


class WorkflowSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: Literal["single_step", "multi_step"]
    rationale: str = Field(min_length=1, max_length=300)
    goals: list[str] = Field(default_factory=list, max_length=8)
    usage: ModelUsage | None = None

    @field_validator("goals")
    @classmethod
    def check_goals(cls, values):
        if any(not value.strip() or len(value) > 300 for value in values):
            raise ValueError("目标须为非空文字且不超过 300 字符")
        if len(values) != len(set(values)):
            raise ValueError("目标不能重复")
        return values

    @model_validator(mode="after")
    def require_multi_goals(self):
        if self.workflow == "multi_step" and not self.goals:
            raise ValueError("多步骤分析须列出用户目标")
        return self


class GoalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: int = Field(ge=1, le=8)
    status: Literal["completed", "blocked"]
    evidence_steps: list[int] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=300)


class NextToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: ToolName
    arguments: dict


class AnalysisDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["tool", "finish", "clarify", "stop"]
    rationale: str = Field(min_length=1, max_length=300)
    call: NextToolCall | None = None
    message: str | None = Field(default=None, max_length=2000)
    goal_checks: list[GoalCheck] = Field(default_factory=list, max_length=8)
    final_result_steps: list[int] = Field(default_factory=list, max_length=12)
    usage: ModelUsage | None = None

    @model_validator(mode="after")
    def check_action(self):
        if any(i < 1 for i in self.final_result_steps) or len(self.final_result_steps) != len(
            set(self.final_result_steps)
        ):
            raise ValueError("最终结果步骤编号须为正整数且不能重复")
        if self.final_result_steps and self.action != "finish":
            raise ValueError("仅完成决定可以标记最终结果步骤")
        if (self.action == "tool") != (self.call is not None):
            raise ValueError("仅 tool 决定必须且可以携带工具调用")
        if self.action in ("clarify", "stop") and not (self.message or "").strip():
            raise ValueError("澄清或停止需要明确说明")
        return self


class AnalysisLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_tool_calls: int = Field(default=6, ge=1, le=12)
    max_plan_turns: int = Field(default=8, ge=1, le=16)
    max_seconds: float = Field(default=240, gt=0, le=600, allow_inf_nan=False)
    max_corrections: int = Field(default=2, ge=0, le=4)


QUERY_MODELS = {
    ToolName.COMMUNITY_ANALYSIS: CommunityQuery,
    ToolName.FUNCTION_ENVIRONMENT: FunctionEnvironmentQuery,
    ToolName.FIND_SAMPLES: FindSamplesQuery,
    ToolName.FIND_TAXA: FindTaxaQuery,
    ToolName.TAXON_ABUNDANCE: TaxonAbundanceQuery,
    ToolName.DIVERSITY_ANALYSIS: DiversityQuery,
    ToolName.ENVIRONMENT_ASSOCIATION: EnvironmentAssociationQuery,
    ToolName.FIND_FUNCTION_SAMPLES: FunctionSamplesQuery,
    ToolName.FUNCTION_PROFILE: FunctionProfileQuery,
    ToolName.FUNCTION_ATLAS: AtlasQuery,
    ToolName.FUNCTION_STUDY: FunctionStudyQuery,
    ToolName.COMPARE_FUNCTION_SIGNALS: FunctionComparisonQuery,
    ToolName.RETRIEVE_GENE_SEQUENCES: SequenceQuery,
}


def argument_issues(tool: ToolDefinition, arguments: dict) -> list[dict]:
    """先校验实际 MCP Schema，再复用领域校验器；不改写参数或执行工具。"""
    validator = validator_for(tool.input_schema)
    validator.check_schema(tool.input_schema)
    issues = [
        {"path": ".".join(map(str, error.absolute_path)), "message": error.message[:300]}
        for error in islice(validator(tool.input_schema).iter_errors(arguments), 5)
    ]
    if issues or tool.name not in QUERY_MODELS:
        return issues
    try:
        QUERY_MODELS[tool.name].model_validate(arguments["query"])
    except ValidationError as error:
        return [
            {"path": "query." + ".".join(map(str, item["loc"])), "message": item["msg"][:300]}
            for item in error.errors(include_input=False, include_context=False)[:5]
        ]
    return []


def completion_issue(goals: list[str], checks: list[GoalCheck], steps: list[dict]) -> str | None:
    """校验目标覆盖与证据引用；不声称机器能验证模型的科研解释。"""
    if sorted(check.goal_id for check in checks) != list(range(1, len(goals) + 1)):
        return "完成检查必须逐项覆盖原目标，目标编号不能缺失、重复或新增。"
    available = {step["step_id"] for step in steps}
    for check in checks:
        if len(check.evidence_steps) != len(set(check.evidence_steps)):
            return f"目标 {check.goal_id} 的证据编号重复。"
        if not set(check.evidence_steps) <= available:
            return f"目标 {check.goal_id} 引用了不存在的成功步骤。"
        if check.status == "completed" and not check.evidence_steps:
            return f"目标 {check.goal_id} 声明完成但没有工具证据。"
    return None


RESULT_MODELS = {
    ToolName.COMMUNITY_ANALYSIS: CommunityResult,
    ToolName.FUNCTION_ENVIRONMENT: FunctionEnvironmentResult,
    ToolName.FIND_SAMPLES: FindSamplesResult,
    ToolName.GET_SAMPLE_INFO: SampleInfoResult,
    ToolName.FIND_TAXA: FindTaxaResult,
    ToolName.TAXON_ABUNDANCE: TaxonAbundanceResult,
    ToolName.DIVERSITY_ANALYSIS: DiversityResult,
    ToolName.ENVIRONMENT_ASSOCIATION: EnvironmentAssociationResult,
    ToolName.FIND_FUNCTION_SAMPLES: FunctionSamplesResult,
    ToolName.FUNCTION_PROFILE: FunctionSignalResult,
    ToolName.FUNCTION_ATLAS: AtlasResult,
    ToolName.FUNCTION_STUDY: FunctionStudyResult,
    ToolName.COMPARE_FUNCTION_SIGNALS: ComparisonResult,
    ToolName.RETRIEVE_GENE_SEQUENCES: SequenceResult,
}


def validate_result(tool: ToolName, result: dict) -> dict:
    return RESULT_MODELS[tool].model_validate(result).model_dump(mode="json")


def bounded_evidence(
    steps: list[dict], *, max_chars: int = 24000, results_summarized: bool = False
) -> list[dict]:
    """保留依赖步骤所需的实际编号；有限明细与明确截断，不裁断 JSON 文本。"""

    def compact(value, list_limit, string_limit, depth=0, field=None):
        if depth > 7:
            return {"truncated": True}
        if isinstance(value, dict):
            result = {
                k: compact(v, list_limit, string_limit, depth + 1, k)
                for k, v in list(value.items())[:30]
            }
            if len(value) > 30:
                result["truncated"] = True
            return result
        if isinstance(value, list):
            # 比较统计最多二十个家族，不能随大表压缩而遗漏目标家族。
            limit = 20 if field in ("summaries", "pfam_accessions") else list_limit
            result = [compact(v, list_limit, string_limit, depth + 1) for v in value[:limit]]
            if len(value) > limit:
                result.append({"truncated": True, "total_items": len(value)})
            return result
        if isinstance(value, str) and len(value) > string_limit:
            return {"text_prefix": value[:string_limit], "truncated": True}
        return value

    # 预算包括 JSON 键和结构开销；完整结果仍留在 API 响应和持久化记录中。
    per_step = max_chars // max(1, len(steps)) - 2
    evidence = []
    for step in steps:
        original = {k: v for k, v in step.items() if k != "call_key"}
        tool = step.get("tool_name")
        if (
            not results_summarized
            and tool
            in (
                ToolName.FUNCTION_ATLAS,
                ToolName.COMMUNITY_ANALYSIS,
                ToolName.FUNCTION_ENVIRONMENT,
                ToolName.FUNCTION_STUDY,
                ToolName.COMPARE_FUNCTION_SIGNALS,
                ToolName.RETRIEVE_GENE_SEQUENCES,
            )
            and isinstance(step.get("result"), dict)
        ):
            # 大型明细和逐文件来源留在完整结果；规划须优先看到全部目标家族统计。
            summary = build_result_summary(ToolName(tool), step["result"])
            summary["full_result_list_counts"] = {
                key: len(value) for key, value in step["result"].items() if isinstance(value, list)
            }
            summary["model_context_note"] = (
                "完整工具结果已保存并由界面展示；full_result_list_counts 是完整结果的行数。"
                "此处省略或截断仅影响模型摘要，不代表工具未返回明细，勿为补齐摘要重复调用。"
            )
            if tool == ToolName.RETRIEVE_GENE_SEQUENCES:
                summary["sequence_records"] = [
                    {"geneID": row["geneID"], "header": row["header"]}
                    for row in step["result"].get("sequences", [])
                ]
            metadata = summary.get("metadata", {})
            provenance = metadata.get("provenance", {})
            filters = provenance.get("filters", {})
            for field in ("source_files", "source_reports"):
                if field in filters:
                    filters[field] = {"omitted_from_planning": True}
            original["result"] = summary
        for list_limit, string_limit in ((10, 1000), (5, 500), (2, 250), (1, 100)):
            item = compact(original, list_limit, string_limit)
            if len(json.dumps(item, ensure_ascii=False)) <= per_step:
                break
        else:
            item = {
                "step_id": step.get("step_id"),
                "tool_name": step.get("tool_name"),
                "truncated": True,
                "reason": "单步骤证据超过模型上下文预算，不能推断明细。",
            }
        evidence.append(item)
    return evidence
