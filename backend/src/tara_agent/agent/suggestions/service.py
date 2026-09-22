"""问题建议目录的构建与抽样服务。"""

from __future__ import annotations

import random

from tara_agent.agent.gateway import MCPToolGateway
from tara_agent.agent.suggestions.catalog import build_question_catalog
from tara_agent.agent.suggestions.models import QuestionSuggestion
from tara_agent.agent.suggestions.profile import read_suggestion_profile
from tara_agent.data.reader import ProcessedDataReader


class QuestionSuggestionService:
    """只从当前数据和已注册工具共同支持的问题中抽样。"""

    def __init__(
        self,
        gateway: MCPToolGateway,
        *,
        ready_datasets: set[str],
        candidates: tuple[QuestionSuggestion, ...],
        random_source: random.Random | None = None,
    ) -> None:
        self.gateway = gateway
        self.ready_datasets = frozenset(ready_datasets)
        self.random = random_source or random.SystemRandom()
        self.candidates = candidates

    @classmethod
    def from_reader(
        cls,
        gateway: MCPToolGateway,
        reader: ProcessedDataReader,
        *,
        random_source: random.Random | None = None,
    ) -> QuestionSuggestionService:
        """从已校验的处理后数据构建问题目录。"""

        profile = read_suggestion_profile(reader)
        return cls(
            gateway,
            ready_datasets=set(reader.manifest.sources),
            candidates=build_question_catalog(profile),
            random_source=random_source,
        )

    async def get_batch(
        self,
        *,
        limit: int,
        exclude_ids: set[str] | None = None,
    ) -> list[QuestionSuggestion]:
        tools = {definition.name for definition in await self.gateway.list_tools()}
        supported = [
            item
            for item in self.candidates
            if item.tool_name in tools
            and item.required_datasets <= self.ready_datasets
        ]
        excluded = exclude_ids or set()
        available = [item for item in supported if item.id not in excluded]
        if len(available) < limit:
            available = supported
        return self._balanced_sample(available, limit)

    def _balanced_sample(
        self,
        candidates: list[QuestionSuggestion],
        limit: int,
    ) -> list[QuestionSuggestion]:
        if len(candidates) <= limit:
            items = list(candidates)
            self.random.shuffle(items)
            return items

        by_category: dict[str, list[QuestionSuggestion]] = {}
        for item in candidates:
            by_category.setdefault(item.category, []).append(item)

        categories = list(by_category)
        self.random.shuffle(categories)
        selected = [
            self.random.choice(by_category[category])
            for category in categories[:limit]
        ]
        if len(selected) == limit:
            return selected

        selected_ids = {item.id for item in selected}
        remaining = [item for item in candidates if item.id not in selected_ids]
        selected.extend(self.random.sample(remaining, limit - len(selected)))
        return selected
