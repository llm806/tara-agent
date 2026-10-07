"""把远程数据服务适配到现有 Agent 工具契约。"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from tara_agent.agent.gateway import AgentToolError
from tara_agent.agent.models import MATOU_TOOLS, ToolDefinition, ToolName
from tara_agent.api.schemas import QuestionSuggestionListResponse, QuestionSuggestionResponse
from tara_agent.config import Settings
from tara_agent.data_service.app import (
    DataServiceStatus,
    DataToolRequest,
    DataToolResponse,
    runtime_code_sha256,
)
from tara_agent.observability.execution import (
    execution_parent_id,
    replay_execution_observations,
)


class RemoteToolGateway:
    """远程不可用时明确失败，绝不回退读取本机数据。"""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.data_service_url is None:
            raise ValueError("未配置数据服务地址")
        self.url = settings.data_service_url
        self.token = settings.get_data_service_token()
        self.timeout = settings.data_service_timeout_seconds
        self.transport = transport
        self.code_sha256 = runtime_code_sha256()
        self.generation: str | None = None
        self.matou_manifest_sha256: str | None = None
        self.study_manifest_sha256: str | None = None
        self.function_reference_sha256: str | None = None
        self._study_bound = False
        self.research_manifest_sha256: str | None = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.url,
                timeout=self.timeout,
                transport=self.transport,
                headers={"Authorization": f"Bearer {self.token}"},
                trust_env=False,
            ) as client:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            messages = {
                401: "数据服务认证失败，请检查凭据文件",
                409: "数据服务代码或数据版本不匹配，请同步版本并重启",
                503: "数据服务未就绪，请检查服务器处理数据",
            }
            raise AgentToolError(
                messages.get(exc.response.status_code, "远程数据请求失败")
            ) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise AgentToolError("无法访问数据服务，请检查 SSH 隧道和服务状态") from exc

    async def get_status(self) -> DataServiceStatus:
        try:
            status = DataServiceStatus.model_validate(await self._request("GET", "/health"))
        except ValidationError as exc:
            raise AgentToolError("数据服务状态契约不匹配") from exc
        if status.protocol_version != "1" or status.status != "ok":
            raise AgentToolError("数据服务协议不支持或服务未就绪")
        if status.code_sha256 != self.code_sha256:
            raise AgentToolError("本地与服务器分析代码或计算依赖不一致，请同步数据服务版本")
        generation = status.manifest.generation
        if self.generation is not None and generation != self.generation:
            raise AgentToolError("数据生成版本已变化，请验证后重启本地后端")
        self.generation = generation
        matou_sha = status.matou.manifest_sha256 if status.matou is not None else None
        if self.matou_manifest_sha256 is not None and matou_sha != self.matou_manifest_sha256:
            raise AgentToolError("MATOU 清单版本已变化，请验证后重启本地后端")
        self.matou_manifest_sha256 = matou_sha
        study_sha = status.matou.study_manifest_sha256 if status.matou is not None else None
        if self._study_bound and study_sha != self.study_manifest_sha256:
            raise AgentToolError("MATOU 功能谱缓存版本已变化，请验证后重启本地后端")
        self.study_manifest_sha256 = study_sha
        reference_sha = status.matou.function_reference_sha256 if status.matou is not None else None
        if self._study_bound and reference_sha != self.function_reference_sha256:
            raise AgentToolError("论文功能参考资料版本已变化，请重启本地后端")
        self.function_reference_sha256 = reference_sha
        if self._study_bound and self.research_manifest_sha256 != status.research_manifest_sha256:
            raise AgentToolError("研究资料版本已变化，请重启本地后端")
        self.research_manifest_sha256 = status.research_manifest_sha256
        self._study_bound = True
        return status

    async def list_tools(self) -> list[ToolDefinition]:
        await self.get_status()
        try:
            return [
                ToolDefinition.model_validate(item) for item in await self._request("GET", "/tools")
            ]
        except ValidationError as exc:
            raise AgentToolError("远程工具契约不匹配") from exc

    async def call(self, tool_name: ToolName, arguments: dict) -> dict:
        if tool_name not in frozenset(ToolName):
            raise AgentToolError(f"Tool is not allowed: {tool_name}")
        status = await self.get_status()
        if tool_name in MATOU_TOOLS and status.matou is None:
            raise AgentToolError("远程数据服务尚未启用 MATOU 功能数据")
        request = DataToolRequest(
            tool_name=tool_name,
            arguments=arguments,
            parent_id=execution_parent_id(),
            expected_code_sha256=self.code_sha256,
            expected_generation=status.manifest.generation,
            expected_research_manifest_sha256=status.research_manifest_sha256,
            expected_matou_manifest_sha256=(
                status.matou.manifest_sha256 if tool_name in MATOU_TOOLS and status.matou else None
            ),
            expected_study_manifest_sha256=(
                status.matou.study_manifest_sha256
                if tool_name in MATOU_TOOLS and status.matou
                else None
            ),
            expected_function_reference_sha256=(
                status.matou.function_reference_sha256
                if tool_name is ToolName.FUNCTION_STUDY and status.matou
                else None
            ),
        )
        try:
            response = DataToolResponse.model_validate(
                await self._request("POST", "/call", json=request.model_dump(mode="json"))
            )
            replay_execution_observations(response.observations)
        except ValueError as exc:
            raise AgentToolError("远程分析返回的结果或执行树无效") from exc
        if response.error is not None:
            raise AgentToolError(response.error)
        if response.result is None:
            raise AgentToolError("远程分析未返回结构化结果")
        return response.result

    async def get_batch(
        self,
        *,
        limit: int,
        exclude_ids: set[str] | None = None,
    ) -> list[QuestionSuggestionResponse]:
        await self.get_status()
        params = [("limit", str(limit))]
        params.extend(("exclude_id", key) for key in sorted(exclude_ids or []))
        try:
            payload = QuestionSuggestionListResponse.model_validate(
                await self._request("GET", "/suggestions", params=params)
            )
        except ValidationError as exc:
            raise AgentToolError("远程问题建议契约不匹配") from exc
        return payload.items
