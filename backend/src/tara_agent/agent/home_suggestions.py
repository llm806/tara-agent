"""应用层组织首页问题与标签，复用科学服务，不改变科学工具或其部署版本。"""

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from tara_agent.agent.gateway import ToolGateway
from tara_agent.agent.models import ToolName
from tara_agent.analysis.function_query_models import FunctionSamplesResult

TaskType = Literal[
    "data_query",
    "statistical_analysis",
    "function_analysis",
    "multi_step_analysis",
    "research_discussion",
    "literature_review",
]


@dataclass(frozen=True, slots=True)
class HomeQuestion:
    id: str
    category: str
    question: str
    task_type: TaskType
    datasets: tuple[str, ...]
    availability: Literal["available", "planned"] = "available"
    limitation: str | None = None


def describe_core_question(item) -> HomeQuestion:
    # 兼容现有远程目录；只识别其明确的编号契约，不从自然语言猜数据来源。
    datasets = []
    for marker in ("v4", "v9"):
        if item.id.startswith(
            tuple(
                f"{prefix}-{marker}-"
                for prefix in ("taxa", "abundance", "diversity", "association")
            )
        ):
            datasets.append(f"18s_{marker}")
    if item.category in ("样本筛选", "样本详情", "多样性比较", "环境关联"):
        datasets.append("context_general")
    if item.category in ("样本详情", "环境关联") or item.id.startswith("samples-temperature-"):
        datasets.append("context_stat")
    task_type = (
        "statistical_analysis"
        if item.category in ("丰度分布", "多样性比较", "环境关联")
        else "data_query"
    )
    return HomeQuestion(item.id, item.category, item.question, task_type, tuple(datasets))


class HomeSuggestionService:
    """首页分类抽样与能力展示；科学数据仍由原服务核验和读取。"""

    def __init__(
        self,
        source,
        gateway: ToolGateway,
        *,
        multi_step_available=False,
        random_source=None,
        function_cache_ready: Callable[[], bool] | None = None,
    ):
        self.source = source
        self.gateway = gateway
        self.random = random_source or random.SystemRandom()
        self.multi_step_available = multi_step_available
        self.function_cache_ready = function_cache_ready or (lambda: False)

    async def get_batch(self, *, limit: int, exclude_ids: set[str] | None = None):
        excluded = exclude_ids or set()
        core = await self.source.get_batch(limit=8, exclude_ids=excluded)
        candidates = [describe_core_question(item) for item in core]
        tools = {tool.name for tool in await self.gateway.list_tools()}
        if self.multi_step_available:
            candidates.extend(self._core_multistep_questions(candidates, tools))
        if {ToolName.FIND_FUNCTION_SAMPLES, ToolName.FUNCTION_PROFILE} <= tools:
            candidates.extend(await self._function_questions(tools))
        fresh = [item for item in candidates if item.id not in excluded]
        if len(fresh) < limit:
            fresh = candidates
        by_type = {}
        for item in fresh:
            by_type.setdefault(item.task_type, []).append(item)
        types = list(by_type)
        self.random.shuffle(types)
        # 先按任务类型、再按科研主题抽样，避免样本编号变体挤占核心问题。
        selected = []
        for key in types[:limit]:
            categories = {}
            for item in by_type[key]:
                categories.setdefault(item.category, []).append(item)
            selected.append(self.random.choice(categories[self.random.choice(list(categories))]))
        chosen = {item.id for item in selected}
        remaining = [item for item in fresh if item.id not in chosen]
        selected.extend(self.random.sample(remaining, min(limit - len(selected), len(remaining))))
        self.random.shuffle(selected)
        return selected

    def _core_multistep_questions(self, core, tools):
        candidates = []
        if {ToolName.TAXON_ABUNDANCE, ToolName.GET_SAMPLE_INFO} <= tools:
            for item in core:
                if item.category != "丰度分布":
                    continue
                candidates.append(
                    HomeQuestion(
                        f"multi-context-{item.id}",
                        "丰度与环境核查",
                        item.question.rstrip("？")
                        + "？请在全部符合条件的样本中按相对丰度降序排序，"
                        "查看最高样本的海区、水层、粒径和环境信息，再列出该样本中占比最高的属。",
                        "multi_step_analysis",
                        (*item.datasets, "context_general", "context_stat"),
                        limitation="先在完整筛选范围排序；属占比分母分别为目标类群和全样本读数。",
                    )
                )
                candidates.append(
                    HomeQuestion(
                        f"station-ranking-{item.id}",
                        "站点丰度比较",
                        item.question.rstrip("？")
                        + "？按站点分别给出样本相对丰度均值和最大值的排名，"
                        "保留各站点的有效样本数。",
                        "statistical_analysis",
                        (*item.datasets, "context_general"),
                        limitation="站点内样本等权汇总；不同水层、粒径的差异仍需分别核查。",
                    )
                )
        datasets = {dataset for item in core for dataset in item.datasets}
        if ToolName.DIVERSITY_ANALYSIS in tools and {"18s_v4", "18s_v9"} <= datasets:
            candidates.append(
                HomeQuestion(
                    "multi-marker-diversity",
                    "V4 / V9 趋势比较",
                    "分别比较 V4 与 V9 群落的海区间 ASV 丰富度和 Shannon 多样性，"
                    "两种标记的趋势是否一致？",
                    "multi_step_analysis",
                    ("18s_v4", "18s_v9", "context_general"),
                    limitation="两种标记独立计算；丰富度为观测值，受测序深度影响。",
                )
            )
        return candidates

    async def _function_questions(self, tools=None):
        candidates = []
        tools = tools if tools is not None else {t.name for t in await self.gateway.list_tools()}
        for assay in ("MetaG", "MetaT"):
            listing = FunctionSamplesResult.model_validate(
                await self.gateway.call(
                    ToolName.FIND_FUNCTION_SAMPLES,
                    {"query": {"assay": assay, "limit": 20}},
                )
            )
            datasets = ("matou_taxonomy", "matou_pfam", f"matou_{assay.lower()}")
            if (
                ToolName.FUNCTION_ATLAS in tools
                and (listing.total <= 20 or self.function_cache_ready())
                and any(s.observed_gene_records > 0 for s in listing.items)
            ):
                candidates.append(
                    HomeQuestion(
                        f"function-overall-{assay}",
                        "总体功能排名",
                        f"在全部已准备的 {assay} 样本中，{listing.taxon} 的哪些 Pfam "
                        "相对信号最高？列出前 100 项、各项贡献及样本覆盖率。",
                        "function_analysis",
                        datasets,
                        limitation="原始样本等权；未合并功能类别，不等同于论文图9的合并排名。",
                    )
                )
            for index, sample in enumerate(s for s in listing.items if s.observed_gene_records > 0):
                if index >= 4:
                    break
                candidates.append(
                    HomeQuestion(
                        f"function-{assay}-{sample.sample_name}",
                        "候选功能谱",
                        f"{assay} 样本 {sample.sample_name} 中，{listing.taxon} 的"
                        "哪些候选 Pfam 信号最高？列出前 10 项及相对份额。",
                        "function_analysis",
                        datasets,
                        limitation="单样本候选结构域信号；不代表绝对表达或 DNA/RNA 配对。",
                    )
                )
            if self.multi_step_available and any(
                s.observed_gene_records > 0 for s in listing.items
            ):
                candidates.append(
                    HomeQuestion(
                        f"multi-function-sensitivity-{assay}",
                        "候选条件敏感性",
                        f"选取一个有记录的 {assay} {listing.taxon} 样本，"
                        "比较不加 E 条件与 min_iEvalue ≤ 1e-5 时的前 10 项 Pfam 排名和注释覆盖。",
                        "multi_step_analysis",
                        datasets,
                        limitation="两次计算使用同一样本；候选 E 条件不是官方家族阈值。",
                    )
                )
        return candidates
