"""Agent 运行时使用的最小版本资源清单。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tara_agent.agent.models import ToolDefinition
from tara_agent.agent.prompts import (
    ANSWER_SYSTEM_PROMPT,
    MULTI_STEP_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    WORKFLOW_SELECTOR_SYSTEM_PROMPT,
)


@dataclass(frozen=True, slots=True)
class AgentResource:
    """可写入 Trace 的不可变资源标识。"""

    resource_id: str
    version: str
    checksum: str

    def trace_data(self) -> dict[str, str]:
        return {
            "resource_id": self.resource_id,
            "version": self.version,
            "checksum": self.checksum,
        }


def _resource(resource_id: str, version: str, content: Any) -> AgentResource:
    serialized = (
        content
        if isinstance(content, str)
        else json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return AgentResource(resource_id, version, checksum)


CAPABILITY_PROFILE = {
    "product": "Tara Agent",
    "purpose": "基于已接入的 Tara Oceans 数据进行查询、可复现计算和结果解释",
    "datasets": [
        "样本采集背景数据",
        "样本环境数据",
        "18S V4 ASV 数据",
        "18S V9 ASV 数据",
    ],
    "capabilities": [
        "按采样和环境条件查找样本",
        "查看样本采集和环境信息",
        "查找生物分类群",
        "计算分类群丰度",
        "按站点比较样本相对丰度均值和最大值；查询指定样本的属、种或ASV组成",
        "计算和比较多样性",
        "分析环境变量与分类群相对丰度的 Spearman 关联",
        "通用群落分析：子集ASV筛选、真核分母、重复汇总、exp(Shannon)、多变量PLS及四粒径NMDS/环境拟合",
    ],
    "limitations": [
        "不访问互联网或查询实时信息",
        "不回答缺少已批准知识来源的一般知识问题",
        "不执行任意 Python、Shell 或 SQL",
        "尚不读取用户上传的论文或自定义 Skill",
        "多步骤分析只组合已有工具，不增加工具尚未支持的科学计算",
    ],
    "conditional_capabilities": [
        {
            "required_tools": ["function_environment"],
            "description": (
                "一个目标Pfam的MetaG/MetaT信号与context_stat关联；"
                "只用带证据和版本的显式跨库映射，一个PANGAEA样本一行，无映射明确受阻。"
            ),
        },
        {
            "required_tools": ["function_study"],
            "description": (
                "硅藻任务二：Top100合并功能、粒径与海区分配、DUF285及LHC亚家族PLS；"
                "明确当前计算和作者参考来源；未准备资料或亚家族缓存时明确阻断对应项目。"
            ),
        },
        {
            "required_tools": ["find_function_samples", "function_profile"],
            "description": (
                "仅在这些工具实际可用时，支持已准备类群的 MATOU 样本查询及单样本候选功能谱；"
                "单样本工具不支持 RNA/DNA 活性、环境关联或绝对定量比较。"
            ),
        },
        {
            "required_tools": ["function_atlas", "compare_function_signals"],
            "description": (
                "支持已准备类群的复杂功能任务：MetaT 总体 Top20/Top100 Pfam、各原始样本功能谱、"
                "指定功能的 MetaG/MetaT 相对贡献对应比较和描述性相关。大范围须有预计算缓存；"
                "对应依据为版本化作者采样编码，排除 WGA、近似过滤组和缺失对侧；"
                "不证明同一提取物，不计算 RNA/DNA 活性或差异表达显著性。"
            ),
        },
        {
            "required_tools": ["retrieve_gene_sequences"],
            "description": (
                "按 geneID 或 Pfam 查询提前核验的 MATOU 核酸序列；未准备序列时明确报错。"
            ),
        },
    ],
}

ROUTER_PROMPT_RESOURCE = _resource("prompt.request_router", "1.9.0", ROUTER_SYSTEM_PROMPT)
PLANNER_PROMPT_RESOURCE = _resource("prompt.tool_planner", "1.10.0", PLANNER_SYSTEM_PROMPT)
ANSWER_PROMPT_RESOURCE = _resource("prompt.analysis_answer", "1.9.0", ANSWER_SYSTEM_PROMPT)
SELECTOR_PROMPT_RESOURCE = _resource(
    "prompt.workflow_selector", "1.3.0", WORKFLOW_SELECTOR_SYSTEM_PROMPT
)
MULTI_PLANNER_PROMPT_RESOURCE = _resource(
    "prompt.multi_step_planner", "1.8.0", MULTI_STEP_SYSTEM_PROMPT
)
CAPABILITY_RESOURCE = _resource("capability.tara_agent", "1.7.0", CAPABILITY_PROFILE)
WORKFLOW_RESOURCE = _resource(
    "workflow.tara_agent_request",
    "1.6.0",
    "route -> analysis[select_workflow -> single(understand -> execute -> answer) | "
    "multi(plan -> execute -> plan, finish/clarify/stop -> answer)] | respond; "
    "multi defaults: 6 tool calls, 8 planning turns, 240 seconds, no identical calls; "
    "pre-execution contract feedback: at most 2 corrections; "
    "finish requires fixed-goal coverage and references to verified steps; "
    "blocked goals are partial; single planner multi-tool proposals escalate to "
    "bounded multi-step without execution",
)


def tool_resources(tools: list[ToolDefinition]) -> list[AgentResource]:
    """根据当前 MCP 契约生成稳定的工具资源标识。"""

    return [
        _resource(
            f"tool.{tool.name.value}",
            "1.1.0"
            if tool.name.value in {"taxon_abundance", "function_study", "community_analysis"}
            else "1.0.0",
            {
                "description": tool.description,
                "input_schema": tool.input_schema,
            },
        )
        for tool in tools
    ]
