"""验证合并顺序、分母、缺失、PLS及通过MCP返回的实际结果与来源。"""

import json

import numpy as np
import polars as pl
import pytest

from tara_agent.agent.charts import build_charts
from tara_agent.agent.context import build_result_summary
from tara_agent.agent.gateway import MCPToolGateway
from tara_agent.agent.graph import TaraAgent
from tara_agent.agent.models import ToolName
from tara_agent.agent.multistep import AnalysisDecision, WorkflowSelection, validate_result
from tara_agent.analysis.function_pls import fit_pls
from tara_agent.analysis.function_study import (
    FunctionStudyService,
    function_mapping,
    ranking_and_distribution,
    relative_profiles,
    sample_scope,
)
from tara_agent.analysis.function_study_models import FunctionStudyQuery
from tara_agent.data.function_reference import AUTHOR_COMMIT, REFERENCE_VERSION, FunctionReference
from tara_agent.data.matou_artifacts import describe
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.data_service.app import create_app
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.mcp import create_server
from tara_agent.observability.execution import bind_execution_trace

from .test_data_service import service_settings as service_settings
from .test_function_atlas import ComplexFunctionModel
from .test_function_atlas import cohort as cohort
from .test_matou_function_data import dataset as dataset


def reference_bundle(path):
    path.mkdir()
    frames = {
        "pfam": pl.DataFrame(
            {
                "pfam_accession": ["PF00001", "PF00002", "PF03382", "PF00504"],
                "name": ["a", "b", "DUF285", "LHC"],
                "description": ["Ribosomal protein A", "Ribosomal protein B", "DUF285", "LHC"],
            }
        ),
        "oceans": pl.DataFrame({"station": [str(i) for i in range(1, 7)], "ocean": ["AO"] * 6}),
        "environment": pl.DataFrame(
            {"station": [str(i) for i in range(1, 7)], "depth": ["SUR"] * 6}
        ),
        "coordinates": pl.DataFrame(
            {"station": [str(i) for i in range(1, 7)], "lat": [float(i) for i in range(6)]}
        ),
    }
    for assay, protocol in (("MetaG", "11"), ("MetaT", "14")):
        frames[assay] = pl.DataFrame(
            {
                "sample": [f"{i}SUR1GGMM{protocol}" for i in range(1, 7)],
                "pfamAcc": ["PF00001"] * 6,
                "rpkm": [float(i) for i in range(1, 7)],
            }
        )
    files = {}
    for key, frame in frames.items():
        target = path / (key + ".parquet")
        frame.write_parquet(target)
        files[key] = {**describe(target).model_dump(), "path": target.name}
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "version": REFERENCE_VERSION,
                "author_commit": AUTHOR_COMMIT,
                "files": files,
                "sources": {},
            }
        )
    )
    return FunctionReference(path)


def select_diatoms(directory):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["selection"]["taxon"] = "Bacillariophyta"
    path.write_text(json.dumps(manifest))


def test_merge_before_normalize_and_unknown_protocol_is_excluded():
    names = ["1SUR1GGMM14", "1SUR2GGZZ14", "2SUR1MMQQ14", "1SUR1GGMM12", "invalid"]
    scope, excluded = sample_scope(names, "MetaT", reference=False)
    assert len(excluded) == 2
    profiles = pl.DataFrame(
        {"sample_name": names[:3], "pfam_accession": ["PF00001"] * 3, "value_sum": [1.0, 9.0, 9.0]}
    )
    totals = pl.DataFrame({"sample_name": names[:3], "taxon_value_sum": [10.0, 10.0, 90.0]})
    rows, denominators = relative_profiles(profiles, scope, totals=totals)
    assert len(rows) == 2 and rows[0]["relative_signal"] == pytest.approx(0.5)
    assert sum(denominators.values()) == 110
    pfam = pl.DataFrame(
        {"pfam_accession": ["PF00001"], "name": ["rib"], "description": ["Ribosomal subunit"]}
    )
    ranks, size, ocean, _ = ranking_and_distribution(rows, function_mapping(pfam), {"1": "AO"}, 100)
    assert ranks[0]["function_id"] == "Ribosomal proteins"
    assert ranks[0]["mean_sample_relative_signal"] == pytest.approx(0.3)
    assert sum(r["allocation_fraction"] for r in size) == pytest.approx(1)
    assert ocean[0]["mapped_signal_fraction"] == pytest.approx(5 / 6)


def test_current_total_denominator_is_not_renormalized(cohort):
    select_diatoms(cohort)
    reference = reference_bundle(cohort / "paper_task2")
    service = FunctionStudyService(MatouDataReader(cohort), reference)
    current = service.analyze(FunctionStudyQuery(include_environment=False))
    author = service.analyze(
        FunctionStudyQuery(include_environment=False, normalization="author_script")
    )
    assert current.function_ranks
    assert all(
        r.relative_contribution == r.mean_sample_relative_signal for r in current.function_ranks
    )
    assert current.top_contribution_sum < author.top_contribution_sum
    with pytest.raises(ValueError, match="不含全部硅藻基因分母"):
        service.analyze(FunctionStudyQuery(source="paper_reference"))


def test_pls_matches_independent_svd_and_reports_missing_and_constants():
    rng = np.random.default_rng(120)
    x = rng.normal(size=(30, 4))
    y = (
        x @ np.array([[2.0, 0.3], [-1.0, 2.0], [0.1, -0.5], [0.4, 1.0]])
        + rng.normal(size=(30, 2)) * 0.05
    )
    rows = [
        dict(
            sampling_group=str(i),
            **{f"x{j}": v for j, v in enumerate(x[i])},
            y0=y[i, 0],
            y1=y[i, 1],
        )
        for i in range(30)
    ]
    rows.append(dict(sampling_group="missing", x0=None))
    result = fit_pls(rows, [f"x{j}" for j in range(4)], ["y0", "y1"], target="test", figure="10b")
    assert result.status == "completed" and result.excluded_rows == 1
    scores = np.array([[r["component_1"], r["component_2"]] for r in result.scores])
    assert abs(np.corrcoef(scores.T)[0, 1]) < 1e-10
    xs = (x - x.mean(0)) / x.std(0, ddof=1)
    ys = (y - y.mean(0)) / y.std(0, ddof=1)
    for component in range(2):
        u, _, _ = np.linalg.svd(xs.T @ ys, full_matrices=False)
        score = xs @ u[:, 0]
        assert abs(np.corrcoef(score, scores[:, component])[0, 1]) > 0.99999
        p = xs.T @ score / (score @ score)
        c = ys.T @ score / (score @ score)
        xs -= np.outer(score, p)
        ys -= np.outer(score, c)
    constant = [{**r, "x0": 1.0} for r in rows[:30]]
    blocked = fit_pls(constant, ["x0", "x1"], ["y0", "y1"], target="test", figure="10b")
    assert blocked.status == "blocked" and blocked.constant_variables == ["x0"]
    assert all(r.component_1**2 + r.component_2**2 <= 1.000001 for r in result.coordinates)


@pytest.mark.anyio
async def test_current_study_mcp_remote_trace_and_missing_subfamilies(
    cohort, service_settings, tmp_path
):
    select_diatoms(cohort)
    reference_bundle(cohort / "paper_task2")
    reader = MatouDataReader(cohort)
    gateway = MCPToolGateway(
        create_server(ProcessedDataReader(service_settings.processed_data_dir), matou_reader=reader)
    )
    events = []
    with bind_execution_trace(parent_id="tool", writer=events.append):
        result = await gateway.call(ToolName.FUNCTION_STUDY, {"query": {"source": "current_data"}})
    result = validate_result(ToolName.FUNCTION_STUDY, result)
    assert result["status"] == "partial"
    assert result["function_ranks"][0]["function_name"] == "Ribosomal proteins"
    assert result["pls_results"][1]["status"] == "blocked"
    assert all(s["relative_signal"] is None for s in result["target_signals"])
    assert result["metadata"]["provenance"]["filters"]["reference_manifest_sha256"]
    assert any(e.get("name", "").startswith("读取论文功能") for e in events)
    charts = build_charts(ToolName.FUNCTION_STUDY, result)
    assert len(charts) == 3 and "Top 1" in charts[0].title
    summary = build_result_summary(ToolName.FUNCTION_STUDY, result)
    assert "target_signals" not in summary
    assert all("input_rows" not in p for p in summary["pls_results"])
    # 远程与进程内复用同一科学工具，同时绑定论文资料版本。
    import httpx

    token = "function-study-test-token-0123456789"
    token_file = tmp_path / "token"
    token_file.write_text(token)
    from tara_agent.config import Settings

    remote = create_app(service_settings.model_copy(update={"matou_data_dir": cohort}), token=token)
    config = Settings(
        environment="test",
        data_service_url="http://127.0.0.1:8011",
        data_service_token_file=token_file,
        _env_file=None,
    )
    remote_gateway = RemoteToolGateway(config, transport=httpx.ASGITransport(app=remote))
    actual = await remote_gateway.call(
        ToolName.FUNCTION_STUDY, {"query": {"source": "current_data"}}
    )
    assert actual == result


def test_reference_tampering_is_rejected(tmp_path):
    reference = reference_bundle(tmp_path / "reference")
    path = reference.directory / "pfam.parquet"
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ValueError, match="SHA-256"):
        reference.frame("pfam")


def test_lhc_subfamilies_need_matching_gene_data(cohort):
    select_diatoms(cohort)
    reference = reference_bundle(cohort / "paper_task2")
    reader = MatouDataReader(cohort)
    reference.manifest["files"]["lhc_profiles"] = {}
    reference.manifest["lhc_source_manifest_sha256"] = "0" * 64
    service = FunctionStudyService(reader, reference)
    with pytest.raises(ValueError, match="LHC亚家族预计算"):
        service._environment("current_data")


def test_lhc_cache_uses_all_classified_signal_and_preserves_absence(cohort):
    select_diatoms(cohort)
    reference = reference_bundle(cohort / "paper_task2")
    reader = MatouDataReader(cohort)
    records = [
        {
            "assay": assay,
            "sample_name": f"1SUR1GGMM{protocol}",
            "subfamily": family,
            "value_sum": value,
        }
        for assay, protocol in (("MetaG", "11"), ("MetaT", "14"))
        for family, value in (("LHCf", 2.0), ("LHCx", 1.0), ("LHCr9Homolog", 1.0))
    ]
    path = reference.directory / "lhc_profiles.parquet"
    pl.DataFrame(records).write_parquet(path)
    reference.manifest["files"]["lhc_profiles"] = {**describe(path).model_dump(), "path": path.name}
    reference.manifest["lhc_source_manifest_sha256"] = reader.manifest_record.sha256
    (reference.directory / "manifest.json").write_text(json.dumps(reference.manifest))
    signals, pls = FunctionStudyService(
        MatouDataReader(cohort), FunctionReference(reference.directory)
    )._environment("current_data")
    lhc = {s["target"]: s for s in signals if s["assay"] == "MetaG" and s["target"] != "LHC"}
    assert lhc["LHCf"]["denominator"] == 4.0
    assert lhc["LHCf"]["relative_signal"] == 0.5
    assert lhc["LHCx"]["relative_signal"] == 0.25
    assert lhc["LHCr"]["relative_signal"] is None
    assert pls[1].status == "blocked"
    # 作者Pfam汇总表不允许混用当前数据的LHC亚家族预计算。
    _, reference_pls = FunctionStudyService(
        reference=FunctionReference(reference.directory)
    )._environment("paper_reference")
    assert reference_pls[1].status == "blocked"


def test_mean_retains_positive_groups_without_annotation():
    mapping = {"PF00001": {"function_id": "PF00001", "function_name": "test", "excluded": False}}
    rows = [
        {
            "pfam_accession": "PF00001",
            "station": "1",
            "depth": "SUR",
            "size_fraction": "0.8-5/2000",
            "protocol": "14",
            "relative_signal": 0.5,
        }
    ]
    ranks, _, _, count = ranking_and_distribution(
        rows, mapping, {"1": "AO"}, 100, sample_group_count=2
    )
    assert count == 2 and ranks[0]["mean_sample_relative_signal"] == 0.25


@pytest.mark.anyio
async def test_model_finish_does_not_override_blocked_scientific_section(cohort, service_settings):
    select_diatoms(cohort)
    reference_bundle(cohort / "paper_task2")
    gateway = MCPToolGateway(
        create_server(
            ProcessedDataReader(service_settings.processed_data_dir),
            matou_reader=MatouDataReader(cohort),
        )
    )

    class StudyModel(ComplexFunctionModel):
        async def select_workflow(self, question, context, tools):
            return WorkflowSelection(
                workflow="multi_step", rationale="论文功能分析。", goals=["功能分析"]
            )

        async def next_analysis_step(self, question, context, tools, steps, budget):
            if not steps:
                return AnalysisDecision(
                    action="tool",
                    rationale="调用科学计算工具。",
                    call={"tool_name": "function_study", "arguments": {"query": {}}},
                )
            return AnalysisDecision(
                action="finish",
                rationale="工具已返回。",
                goal_checks=[
                    {
                        "goal_id": 1,
                        "status": "completed",
                        "evidence_steps": [1],
                        "reason": "有结果。",
                    }
                ],
            )

    response = await TaraAgent(StudyModel(), gateway).run("计算硅藻Top100及LHC环境关联")
    assert response.result["status"] == "partial"
    assert "LHC" in response.result["stop_reason"]
    assert response.result["analysis_steps"][0]["result"]["function_ranks"]
