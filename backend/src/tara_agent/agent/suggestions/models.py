"""问题建议内部数据结构。"""

from __future__ import annotations

from dataclasses import dataclass

from tara_agent.agent.models import ToolName
from tara_agent.analysis.compute_models import EnvironmentVariable
from tara_agent.domain.contracts import Marker


@dataclass(frozen=True, slots=True)
class QuestionSuggestion:
    """一条经过数据和能力约束、可以直接提交给 Agent 的问题。"""

    id: str
    category: str
    question: str
    tool_name: ToolName
    required_datasets: frozenset[str]


@dataclass(frozen=True, slots=True)
class SampleFilter:
    """一组在样本背景数据中真实出现过的筛选值。"""

    ocean_region: str
    depth: str
    size_fraction: str


@dataclass(frozen=True, slots=True)
class SuggestionProfile:
    """生成问题所需的少量真实数据特征。"""

    sample_ids: tuple[str, ...]
    ocean_regions: tuple[str, ...]
    depths: tuple[str, ...]
    size_fractions: tuple[str, ...]
    polar_options: tuple[bool, ...]
    temperatures: tuple[float, ...]
    sample_filters: tuple[SampleFilter, ...]
    environment_variables: tuple[EnvironmentVariable, ...]
    taxa_by_marker: dict[Marker, tuple[str, ...]]
