"""把真实数据特征转换为当前工具能够执行的问题目录。"""

from __future__ import annotations

from tara_agent.agent.models import ToolName
from tara_agent.agent.suggestions.models import QuestionSuggestion, SuggestionProfile
from tara_agent.agent.suggestions.profile import ENVIRONMENT_LABELS
from tara_agent.domain.contracts import Marker

_GENERAL_DATA = frozenset({"context_general"})
_CONTEXT_DATA = frozenset({"context_general", "context_stat"})


def build_question_catalog(
    profile: SuggestionProfile,
) -> tuple[QuestionSuggestion, ...]:
    candidates = _sample_candidates(profile)
    for marker in Marker:
        candidates.extend(_marker_candidates(profile, marker))
    return tuple(candidates)


def _sample_candidates(profile: SuggestionProfile) -> list[QuestionSuggestion]:
    candidates: list[QuestionSuggestion] = []
    for index, ocean_region in enumerate(profile.ocean_regions):
        candidates.append(
            QuestionSuggestion(
                f"samples-region-{index}",
                "样本筛选",
                f"{ocean_region} 中收录了哪些 Tara 样本？",
                ToolName.FIND_SAMPLES,
                _GENERAL_DATA,
            )
        )
    for index, depth in enumerate(profile.depths):
        candidates.append(
            QuestionSuggestion(
                f"samples-depth-{index}",
                "样本筛选",
                f"有哪些 {depth} 水层的样本可用于分析？",
                ToolName.FIND_SAMPLES,
                _GENERAL_DATA,
            )
        )
    for index, size_fraction in enumerate(profile.size_fractions):
        candidates.append(
            QuestionSuggestion(
                f"samples-size-fraction-{index}",
                "样本筛选",
                f"筛选粒径范围为 {size_fraction} 的样本",
                ToolName.FIND_SAMPLES,
                _GENERAL_DATA,
            )
        )
    for polar in profile.polar_options:
        label = "极地" if polar else "非极地"
        candidates.append(
            QuestionSuggestion(
                f"samples-polar-{str(polar).lower()}",
                "样本筛选",
                f"列出数据中的{label}样本",
                ToolName.FIND_SAMPLES,
                _GENERAL_DATA,
            )
        )
    for index, temperature in enumerate(profile.temperatures):
        threshold = f"{temperature:g}"
        candidates.extend(
            [
                QuestionSuggestion(
                    f"samples-temperature-min-{index}",
                    "样本筛选",
                    f"哪些样本的温度不低于 {threshold} 摄氏度？",
                    ToolName.FIND_SAMPLES,
                    _CONTEXT_DATA,
                ),
                QuestionSuggestion(
                    f"samples-temperature-max-{index}",
                    "样本筛选",
                    f"哪些样本的温度不高于 {threshold} 摄氏度？",
                    ToolName.FIND_SAMPLES,
                    _CONTEXT_DATA,
                ),
            ]
        )
    for index, filters in enumerate(profile.sample_filters):
        candidates.append(
            QuestionSuggestion(
                f"samples-region-depth-{index}",
                "样本筛选",
                f"查找 {filters.ocean_region} 中 {filters.depth} 水层的样本",
                ToolName.FIND_SAMPLES,
                _GENERAL_DATA,
            )
        )
        candidates.append(
            QuestionSuggestion(
                f"samples-depth-size-{index}",
                "样本筛选",
                f"查找 {filters.depth} 水层且粒径为 {filters.size_fraction} 的样本",
                ToolName.FIND_SAMPLES,
                _GENERAL_DATA,
            )
        )
    for index, sample_id in enumerate(profile.sample_ids):
        candidates.append(
            QuestionSuggestion(
                f"sample-details-{index}",
                "样本详情",
                f"样本 {sample_id} 是在哪里、什么环境下采集的？",
                ToolName.GET_SAMPLE_INFO,
                _CONTEXT_DATA,
            )
        )
    return candidates


def _marker_candidates(
    profile: SuggestionProfile,
    marker: Marker,
) -> list[QuestionSuggestion]:
    marker_label = marker.value.upper()
    marker_data = frozenset({f"18s_{marker.value}"})
    marker_with_general = marker_data | _GENERAL_DATA
    marker_with_context = marker_data | _CONTEXT_DATA
    candidates = _diversity_candidates(marker, marker_label, marker_with_general)

    for taxon_index, taxon in enumerate(profile.taxa_by_marker.get(marker, ())):
        prefix = f"{marker.value}-{taxon_index}"
        candidates.extend(
            [
                QuestionSuggestion(
                    f"taxa-{prefix}",
                    "分类群检索",
                    f"{marker_label} 中有哪些属于 {taxon} 的 ASV，在哪些样本检出？",
                    ToolName.FIND_TAXA,
                    marker_data,
                ),
                QuestionSuggestion(
                    f"abundance-{prefix}",
                    "丰度分布",
                    f"{marker_label} 中 {taxon} 在各样本的相对丰度如何？",
                    ToolName.TAXON_ABUNDANCE,
                    marker_data,
                ),
            ]
        )
        for variable in profile.environment_variables:
            candidates.append(
                QuestionSuggestion(
                    f"association-{prefix}-{variable.value}",
                    "环境关联",
                    (
                        f"{marker_label} 中 {taxon} 的相对丰度"
                        f"与{ENVIRONMENT_LABELS[variable]}是否相关？"
                    ),
                    ToolName.ENVIRONMENT_ASSOCIATION,
                    marker_with_context,
                )
            )
    return candidates


def _diversity_candidates(
    marker: Marker,
    marker_label: str,
    required_datasets: frozenset[str],
) -> list[QuestionSuggestion]:
    questions = (
        ("ocean-region-shannon", f"{marker_label} 的 Shannon 多样性在不同海区间有何差异？"),
        ("ocean-region-richness", f"比较不同海区的 {marker_label} 观测 ASV 丰富度"),
        ("depth-shannon", f"不同采样水层的 {marker_label} Shannon 多样性如何？"),
        ("depth-richness", f"按采样水层比较 {marker_label} 的观测 ASV 丰富度"),
        ("size-shannon", f"不同粒径范围的 {marker_label} Shannon 多样性有何差异？"),
        ("size-richness", f"按粒径范围比较 {marker_label} 的观测 ASV 丰富度"),
        ("polar-shannon", f"比较极地与非极地样本的 {marker_label} Shannon 多样性"),
        ("polar-richness", f"极地与非极地样本的 {marker_label} 观测 ASV 丰富度有何差异？"),
    )
    return [
        QuestionSuggestion(
            f"diversity-{marker.value}-{identifier}",
            "多样性比较",
            question,
            ToolName.DIVERSITY_ANALYSIS,
            required_datasets,
        )
        for identifier, question in questions
    ]
