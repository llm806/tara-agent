"""FastAPI 应用工厂。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tara_agent import __version__
from tara_agent.agent import AgentModel, DeepSeekChatModel, TaraAgent
from tara_agent.agent.gateway import MCPToolGateway
from tara_agent.agent.runtime import PersistentAgentRunner
from tara_agent.agent.suggestions import QuestionSuggestionService
from tara_agent.api.routes import (
    auth_router,
    chat_router,
    health_router,
    history_router,
    suggestions_router,
)
from tara_agent.auth import AuthService
from tara_agent.config import Settings, get_settings
from tara_agent.data.reader import ProcessedDataError, ProcessedDataReader
from tara_agent.mcp import create_server
from tara_agent.persistence import Database
from tara_agent.persistence.repositories import AgentRunRepository


def create_app(
    settings: Settings | None = None,
    *,
    agent_model: AgentModel | None = None,
    database: Database | None = None,
) -> FastAPI:
    runtime_settings = settings or get_settings()
    runtime_database = database
    if runtime_database is None and runtime_settings.database_url is not None:
        runtime_database = Database(runtime_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if runtime_database is not None:
            await runtime_database.ping()
        try:
            yield
        finally:
            if runtime_database is not None:
                await runtime_database.dispose()

    application = FastAPI(
        title=runtime_settings.app_name,
        version=__version__,
        description="Tara Agent 的海洋科学数据分析 API。",
        lifespan=lifespan,
    )
    application.state.settings = runtime_settings
    application.state.database = runtime_database
    application.state.run_repository = (
        AgentRunRepository(runtime_database) if runtime_database is not None else None
    )
    application.state.auth_service = (
        AuthService(
            runtime_database,
            session_days=runtime_settings.auth_session_days,
            guest_email=runtime_settings.auth_guest_email,
            guest_display_name=runtime_settings.auth_guest_display_name,
        )
        if runtime_database is not None
        else None
    )
    try:
        application.state.data_reader = ProcessedDataReader(runtime_settings.processed_data_dir)
    except ProcessedDataError:
        application.state.data_reader = None

    application.state.agent = None
    application.state.persistent_agent = None
    application.state.question_suggestions = None
    if application.state.data_reader is not None:
        mcp_server = create_server(application.state.data_reader)
        gateway = MCPToolGateway(mcp_server)
        application.state.question_suggestions = QuestionSuggestionService.from_reader(
            gateway,
            application.state.data_reader,
        )
        model = agent_model
        if model is None and runtime_settings.deepseek_api_key is not None:
            model = DeepSeekChatModel(runtime_settings)
        if model is not None:
            application.state.agent = TaraAgent(model, gateway)
            if application.state.run_repository is not None:
                application.state.persistent_agent = PersistentAgentRunner(
                    application.state.agent,
                    application.state.run_repository,
                )

    if runtime_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=runtime_settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    application.include_router(health_router, prefix=runtime_settings.api_prefix)
    application.include_router(auth_router, prefix=runtime_settings.api_prefix)
    application.include_router(chat_router, prefix=runtime_settings.api_prefix)
    application.include_router(history_router, prefix=runtime_settings.api_prefix)
    application.include_router(suggestions_router, prefix=runtime_settings.api_prefix)
    return application


app = create_app()
