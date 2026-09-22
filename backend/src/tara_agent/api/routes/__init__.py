"""FastAPI 路由模块。"""

from tara_agent.api.routes.auth import router as auth_router
from tara_agent.api.routes.chat import router as chat_router
from tara_agent.api.routes.health import router as health_router
from tara_agent.api.routes.history import router as history_router
from tara_agent.api.routes.suggestions import router as suggestions_router

__all__ = [
    "auth_router",
    "chat_router",
    "health_router",
    "history_router",
    "suggestions_router",
]
