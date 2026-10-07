"""复用现有 MCP 工具的数据服务，不启动模型或产品数据库。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from tara_agent.agent.gateway import AgentToolError, MCPToolGateway
from tara_agent.agent.models import MATOU_TOOLS, ToolDefinition, ToolName
from tara_agent.agent.suggestions import QuestionSuggestionService
from tara_agent.api.schemas import QuestionSuggestionListResponse, QuestionSuggestionResponse
from tara_agent.config import Settings
from tara_agent.data.manifest import DataManifest
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.reader import ProcessedDataError, ProcessedDataReader
from tara_agent.data.research_context import ResearchContext
from tara_agent.mcp import create_server
from tara_agent.observability.execution import TraceObservationEvent, bind_execution_trace

_bearer = HTTPBearer(auto_error=False)


class MatouDataStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    study_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    function_reference_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    dataset_version: str
    pipeline_version: str
    taxon: str
    sample_counts: dict[str, int]


class DataServiceStatus(BaseModel):
    """数据生成版本和实际运行代码摘要，用于部署一致性检查。"""

    model_config = ConfigDict(extra="forbid")
    status: str = "ok"
    protocol_version: str = "1"
    code_sha256: str
    manifest: DataManifest
    matou: MatouDataStatus | None = None
    research_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DataToolRequest(BaseModel):
    """只允许已注册的工具；具体参数仍由同一 MCP Schema 校验。"""

    model_config = ConfigDict(extra="forbid")
    tool_name: ToolName
    arguments: dict[str, Any]
    parent_id: UUID | None = None
    expected_code_sha256: str = Field(min_length=64, max_length=64)
    expected_generation: str = Field(min_length=1)
    expected_research_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_matou_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_study_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_function_reference_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DataToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any] | None = None
    error: str | None = None
    observations: list[TraceObservationEvent] = Field(default_factory=list)


def runtime_code_sha256() -> str:
    """绑定科学算法、工具契约及计算依赖，前端或 API 改动无需更新数据服务。"""

    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = [root / "agent/models.py", root / "agent/gateway.py"]
    for directory in ("analysis", "data", "domain", "mcp", "observability", "agent/suggestions"):
        files.extend((root / directory).rglob("*.py"))
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        # Git/上传工具可能转换换行，不将换行差异当作算法变化。
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    for dependency in ("polars", "numpy", "scipy", "pydantic", "mcp"):
        digest.update(f"{dependency}={version(dependency)}\0".encode())
    return digest.hexdigest()


def create_app(
    settings: Settings | None = None,
    *,
    token: str | None = None,
) -> FastAPI:
    """未配置有效凭据或处理后数据时启动失败，避免匿名暴露计算接口。"""

    service_token = token if token is not None else os.environ.get("TARA_DATA_SERVICE_TOKEN", "")
    if len(service_token) < 32:
        raise ValueError("TARA_DATA_SERVICE_TOKEN 必须至少包含 32 个字符")
    runtime_settings = settings or Settings()
    reader = ProcessedDataReader(runtime_settings.processed_data_dir)
    matou_reader = (
        MatouDataReader(runtime_settings.matou_data_dir)
        if runtime_settings.matou_data_dir is not None
        else None
    )
    research = ResearchContext(reader.processed_dir)
    gateway = MCPToolGateway(create_server(reader, matou_reader=matou_reader, research=research))
    suggestions = QuestionSuggestionService.from_reader(gateway, reader)
    code_sha256 = runtime_code_sha256()

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"), service_token.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="数据服务认证失败")

    app = FastAPI(
        title="Tara Data Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authenticate)],
    )
    # 校内共享主机先串行执行科学任务；不随宿主 CPU 数启动额外工作进程。
    gate = asyncio.Semaphore(1)

    @app.get("/health", response_model=DataServiceStatus)
    async def health() -> DataServiceStatus:
        # 每次检查产物存在性和大小，目录被移除时不能继续宣称就绪。
        try:
            current = ProcessedDataReader(reader.processed_dir)
        except ProcessedDataError as exc:
            raise HTTPException(status_code=503, detail="处理后数据不可用") from exc
        if current.manifest.generation != reader.manifest.generation:
            raise HTTPException(status_code=503, detail="数据版本已变化，请重启数据服务")
        try:
            research.ensure_unchanged()
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=503, detail="研究资料包变化，请重启服务") from exc
        matou_status = None
        if matou_reader is not None:
            try:
                current_matou = MatouDataReader(matou_reader.directory)
                if current_matou.manifest_record.sha256 != matou_reader.manifest_record.sha256:
                    raise ValueError("MATOU 清单已变化")
                matou_reader.ensure_unchanged()
            except (ValueError, OSError) as exc:
                raise HTTPException(
                    status_code=503, detail="MATOU 数据已变化或不可用，请验证并重启服务"
                ) from exc
            manifest = matou_reader.manifest
            matou_status = MatouDataStatus(
                manifest_sha256=matou_reader.manifest_record.sha256,
                study_manifest_sha256=matou_reader.study_sha256,
                function_reference_sha256=matou_reader.reference_sha256,
                dataset_version=manifest.dataset_version,
                pipeline_version=manifest.pipeline_version,
                taxon=manifest.selection["taxon"],
                sample_counts={a: len(matou_reader.list_samples(a)) for a in ("MetaG", "MetaT")},
            )
        return DataServiceStatus(
            code_sha256=code_sha256,
            manifest=reader.manifest,
            matou=matou_status,
            research_manifest_sha256=research.sha256,
        )

    @app.get("/tools", response_model=list[ToolDefinition])
    async def tools() -> list[ToolDefinition]:
        return await gateway.list_tools()

    @app.post("/call", response_model=DataToolResponse)
    async def call(request: DataToolRequest) -> DataToolResponse:
        if (
            request.expected_code_sha256 != code_sha256
            or request.expected_generation != reader.manifest.generation
        ):
            raise HTTPException(status_code=409, detail="代码或数据版本不匹配，拒绝执行")
        if request.tool_name in MATOU_TOOLS:
            if (
                matou_reader is None
                or request.expected_matou_manifest_sha256 != matou_reader.manifest_record.sha256
                or request.expected_study_manifest_sha256 != matou_reader.study_sha256
                or (
                    request.tool_name is ToolName.FUNCTION_STUDY
                    and request.expected_function_reference_sha256 != matou_reader.reference_sha256
                )
            ):
                raise HTTPException(
                    status_code=409, detail="MATOU 未启用或清单版本不匹配，拒绝执行"
                )
            try:
                matou_reader.ensure_unchanged()
            except (ValueError, OSError) as exc:
                raise HTTPException(
                    status_code=503, detail="MATOU 清单已变化，请验证并重启服务"
                ) from exc
        if request.tool_name in {ToolName.COMMUNITY_ANALYSIS, ToolName.FUNCTION_ENVIRONMENT}:
            if request.expected_research_manifest_sha256 != research.sha256:
                raise HTTPException(status_code=409, detail="研究资料版本不匹配")
            try:
                research.ensure_unchanged()
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=503, detail="研究资料变化，请重启服务") from exc
        events: list[TraceObservationEvent] = []

        def record(event: dict[str, Any]) -> None:
            events.append(TraceObservationEvent.model_validate(event))

        async with gate:
            with bind_execution_trace(
                parent_id=str(request.parent_id) if request.parent_id is not None else None,
                writer=record,
            ):
                try:
                    result = await gateway.call(request.tool_name, request.arguments)
                except AgentToolError as exc:
                    return DataToolResponse(error=str(exc), observations=events)
        return DataToolResponse(result=result, observations=events)

    @app.get("/suggestions", response_model=QuestionSuggestionListResponse)
    async def question_suggestions(
        limit: Annotated[int, Query(ge=1, le=8)] = 4,
        exclude_id: Annotated[list[str] | None, Query(max_length=8)] = None,
    ) -> QuestionSuggestionListResponse:
        items = await suggestions.get_batch(limit=limit, exclude_ids=set(exclude_id or []))
        return QuestionSuggestionListResponse(
            items=[
                QuestionSuggestionResponse(
                    id=item.id,
                    category=item.category,
                    question=item.question,
                )
                for item in items
            ]
        )

    return app
