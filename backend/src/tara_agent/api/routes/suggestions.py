"""受当前数据与工具能力约束的首页问题推荐接口。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from tara_agent.agent.suggestions import QuestionSuggestionService
from tara_agent.api.dependencies import CurrentUserDependency
from tara_agent.api.schemas import (
    QuestionSuggestionListResponse,
    QuestionSuggestionResponse,
)

router = APIRouter(prefix="/question-suggestions", tags=["agent"])


@router.get("", response_model=QuestionSuggestionListResponse)
async def list_question_suggestions(
    request: Request,
    _: CurrentUserDependency,
    limit: Annotated[int, Query(ge=1, le=8)] = 4,
    exclude_id: Annotated[list[str] | None, Query(max_length=8)] = None,
) -> QuestionSuggestionListResponse:
    """返回一批可由当前系统实际处理的问题。"""

    service: QuestionSuggestionService | None = request.app.state.question_suggestions
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="示例问题暂时不可用，请检查处理后数据。",
        )
    items = await service.get_batch(
        limit=limit,
        exclude_ids=set(exclude_id or []),
    )
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
