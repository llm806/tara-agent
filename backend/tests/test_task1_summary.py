"""验证描述统计保留标记边界、缺失值和完整模型上下文。"""

import pytest

from tara_agent.agent.context import build_result_summary
from tara_agent.agent.models import ToolName
from tara_agent.analysis.community import descriptive_summary


def test_summary_marker_missing_and_band_boundaries():
    rows = [
        dict(
            marker=m,
            depth="SRF",
            size_fraction="20-180",
            event_latitude=lat,
            sample_ids=[sid],
            relative_abundance=value,
            exp_shannon=None,
        )
        for m, lat, sid, value in [
            ("v4", -30, "a", 0.2),
            ("v4", 59, "b", 0.4),
            ("v9", 30, "c", 0.9),
            ("v4", 91, "d", 0.8),
        ]
    ]
    result = descriptive_summary(rows, ["relative_abundance", "exp_shannon"], latitude=True)
    assert len(result) == 2
    assert result[0]["absolute_latitude_band"] == "30–60°"
    assert result[0]["relative_abundance_mean"] == pytest.approx(0.3)
    assert result[0]["source_sample_count"] == 2
    assert result[0]["exp_shannon_n"] == 0
    assert result[0]["exp_shannon_mean"] is None
    assert result[1]["relative_abundance_mean"] == 0.9


def test_answer_keeps_all_latitude_rows():
    rows = [dict(marker="v9", row=i) for i in range(48)]
    summary = build_result_summary(ToolName.COMMUNITY_ANALYSIS, {"latitude_bands": rows})
    assert summary["latitude_bands"] == rows


def test_latitude_summary_reports_non_monotonic_exception():
    rows = [
        dict(
            marker="v4",
            depth="DCM",
            size_fraction="180-2000",
            event_latitude=lat,
            sample_ids=[str(lat)],
            exp_shannon=value,
        )
        for lat, value in [(10, 5), (40, 6), (70, 2)]
    ]
    summary = descriptive_summary(rows, ["exp_shannon"], latitude=True)
    assert all(r["exp_shannon_band_mean_pattern"] == "non_monotonic_or_tied" for r in summary)
