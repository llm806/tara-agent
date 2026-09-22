"""Agent 运行时使用的最小版本资源清单。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tara_agent.agent.models import ToolDefinition
from tara_agent.agent.prompts import (
    ANSWER_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
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
        "计算和比较多样性",
        "分析环境变量与丰度或多样性的关联",
    ],
    "limitations": [
        "不访问互联网或查询实时信息",
        "不回答缺少已批准知识来源的一般知识问题",
        "不执行任意 Python、Shell 或 SQL",
        "尚不读取用户上传的论文或自定义 Skill",
        "当前一次分析只调用一个已批准的数据工具",
    ],
}

ROUTER_PROMPT_RESOURCE = _resource("prompt.request_router", "1.5.0", ROUTER_SYSTEM_PROMPT)
PLANNER_PROMPT_RESOURCE = _resource("prompt.tool_planner", "1.4.0", PLANNER_SYSTEM_PROMPT)
ANSWER_PROMPT_RESOURCE = _resource("prompt.analysis_answer", "1.0.0", ANSWER_SYSTEM_PROMPT)
CAPABILITY_RESOURCE = _resource("capability.tara_agent", "1.0.0", CAPABILITY_PROFILE)
WORKFLOW_RESOURCE = _resource(
    "workflow.tara_agent_request",
    "1.2.0",
    "route -> analysis[understand -> execute -> answer] | respond",
)


def tool_resources(tools: list[ToolDefinition]) -> list[AgentResource]:
    """根据当前 MCP 契约生成稳定的工具资源标识。"""

    return [
        _resource(
            f"tool.{tool.name.value}",
            "1.0.0",
            {
                "description": tool.description,
                "input_schema": tool.input_schema,
            },
        )
        for tool in tools
    ]
