"""验证真实风险：完整家族排名、分母、缺失、采样对应、缓存和序列来源。"""

import json
import runpy
from pathlib import Path

import httpx
import polars as pl
import pytest

from tara_agent.agent.gateway import MCPToolGateway
from tara_agent.agent.graph import TaraAgent
from tara_agent.agent.models import ModelStreamDelta, RouteDecision, ToolName
from tara_agent.agent.multistep import AnalysisDecision, WorkflowSelection, validate_result
from tara_agent.analysis.function_atlas import MatouAtlasService, summarize_sample
from tara_agent.analysis.function_atlas_models import (
    AtlasQuery,
    FunctionComparisonQuery,
    SequenceItem,
    SequenceQuery,
)
from tara_agent.analysis.function_study_prepare import extract_sequences, prepare_study
from tara_agent.data.matou_artifacts import MatouArtifact, MatouSample, describe
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.data.matou_study import SAMPLE_CODE
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.data_service.app import create_app
from tara_agent.data_service.client import RemoteToolGateway
from tara_agent.mcp import create_server
from tara_agent.observability.execution import bind_execution_trace

from .test_data_service import service_settings as service_settings
from .test_matou_function_data import dataset as dataset
from .test_matou_function_data import prepare
from .test_matou_remote_tools import TOKEN


def sample_artifact(directory, assay, name, values):
    path = directory / f"{assay}-{name}.parquet"
    frame = pl.DataFrame(
        {
            "geneID": list(values),
            "samplename": [name] * len(values),
            "value": list(values.values()),
        },
        schema={"geneID": pl.Int64, "samplename": pl.String, "value": pl.Float64},
    )
    frame.write_parquet(path)
    record = describe(path).model_dump()
    record["path"] = path.name
    return MatouSample(
        assay=assay, sample_name=name, artifact=MatouArtifact(**record, rows=frame.height)
    )


@pytest.fixture
def cohort(dataset):
    prepare(dataset)
    directory = dataset[3]
    reader = MatouDataReader(directory)
    samples = []
    for station, g, t in ((1, 0.2, 0.3), (2, 0.4, 0.5), (3, 0.6, 0.7)):
        for assay, code, value in (("MetaG", "11", g), ("MetaT", "14", t)):
            samples.append(
                sample_artifact(
                    directory, assay, f"{station}SUR1GGMM{code}", {1: value, 2: 0.1, 3: 0.9 - value}
                )
            )
    for assay, name in (
        ("MetaG", "4SUR1GGMM12"),
        ("MetaT", "4SUR1GGMM14"),
        ("MetaG", "5SUR1GGMM11"),
        ("MetaT", "5SUR2GGMM14"),
        ("MetaG", "6SUR1GGMM11"),
        ("MetaT", "6SUR1GGZZ14"),
    ):
        samples.append(sample_artifact(directory, assay, name, {1: 0.2, 2: 0.1, 3: 0.7}))
    manifest = reader.manifest.model_copy(update={"samples": samples})
    (directory / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    return directory


def test_atlas_complete_ranking_denominator_and_missing(dataset):
    prepare(dataset)
    result = MatouAtlasService(MatouDataReader(dataset[3])).atlas(
        AtlasQuery(assay="MetaG", top_n=100)
    )
    assert result.sample_count == 2 and result.informative_samples == 1
    assert result.zero_denominator_samples == ["G-empty"]
    assert result.ranks[0].pfam_accession == "PF00002"
    assert result.ranks[0].mean_observed_contribution == pytest.approx(0.9)
    assert result.ranks[1].mean_observed_contribution == pytest.approx(0.6)
    assert result.observations[-1].observed_fraction is None
    assert result.sample_summaries[0].taxon_value_sum == 1
    strict = MatouAtlasService(MatouDataReader(dataset[3])).atlas(
        AtlasQuery(assay="MetaG", pfam_accessions=["PF00002", "PF99999"], max_i_evalue=1e-5)
    )
    assert strict.ranks[0].mean_observed_contribution == pytest.approx(0.3)
    absent = next(o for o in strict.observations if o.pfam_accession == "PF99999")
    assert absent.status == "not_in_retained_mapping" and absent.value_sum is None


def test_all_families_are_aggregated_before_top_n():
    mapping = pl.DataFrame(
        {
            "geneID": [1] * 700,
            "pfamAcc": [f"PF{i:05}" for i in range(700)],
            "min_iEvalue": [1e-8] * 700,
        },
        schema={"geneID": pl.Int64, "pfamAcc": pl.String, "min_iEvalue": pl.Float64},
    )
    frame = pl.DataFrame(
        {"geneID": [1, 2], "samplename": ["A", "A"], "value": [1.0, 9.0]},
        schema={"geneID": pl.Int64, "samplename": pl.String, "value": pl.Float64},
    )
    result = summarize_sample(frame, mapping, "A")
    assert len(result["families"]) == 700 and result["taxon_value_sum"] == 10
    assert result["families"]["PF00699"] == 1


def test_equal_sample_ranking_is_invariant_to_sample_scale(cohort):
    service = MatouAtlasService(MatouDataReader(cohort))
    query = AtlasQuery(sample_names=[f"{n}SUR1GGMM14" for n in (1, 2, 3)])
    before = service.atlas(query)
    path = cohort / "MetaT-1SUR1GGMM14.parquet"
    frame = pl.read_parquet(path).with_columns((pl.col("value") * 1000).alias("value"))
    frame.write_parquet(path)
    manifest = json.loads((cohort / "manifest.json").read_text())
    artifact = next(s["artifact"] for s in manifest["samples"] if s["sample_name"] == "1SUR1GGMM14")
    artifact.update({**describe(path).model_dump(), "path": path.name})
    (cohort / "manifest.json").write_text(json.dumps(manifest))
    after = MatouAtlasService(MatouDataReader(cohort)).atlas(query)
    assert [r.mean_observed_contribution for r in before.ranks] == pytest.approx(
        [r.mean_observed_contribution for r in after.ranks]
    )


def test_correspondence_excludes_wga_iterations_and_nearby_filters(cohort):
    result = MatouAtlasService(MatouDataReader(cohort)).compare(
        FunctionComparisonQuery(pfam_accessions=["PF00001"])
    )
    assert result.matched_sampling_keys == 3
    assert result.summaries[0].spearman_rho == pytest.approx(1)
    assert result.summaries[0].metag_median_fraction == pytest.approx(0.4)
    assert result.summaries[0].metat_median_fraction == pytest.approx(0.5)
    assert result.summaries[0].median_fraction_difference == pytest.approx(0.1)
    assert len(result.excluded_samples) == 6
    assert {p.sampling_key for p in result.points} == {f"{n}SUR1GGMM" for n in (1, 2, 3)}
    assert result.metadata.provenance.filters["correspondence_evidence"]["author_commit"]
    # 作者真实参考表中批次含 01、010 和 0；保留原字面，不合并成整数 1。
    assert SAMPLE_CODE.fullmatch("109DCM01QQSS11")["iteration"] == "01"
    assert SAMPLE_CODE.fullmatch("38DCM0GGMM11")["iteration"] == "0"
    assert result.summaries[0].metag_iqr == pytest.approx(0.2)
    assert result.summaries[0].metat_population_cv is not None


def test_absent_family_is_not_zero_or_complete_pair(cohort):
    result = MatouAtlasService(MatouDataReader(cohort)).compare(
        FunctionComparisonQuery(pfam_accessions=["PF99999"])
    )
    assert result.summaries[0].complete_correspondences == 0
    assert result.summaries[0].spearman_rho is None
    assert all(p.metag_fraction is None and p.fraction_difference is None for p in result.points)


def test_cached_atlas_matches_source_and_preserves_trace(cohort, monkeypatch):
    reader = MatouDataReader(cohort)
    service = MatouAtlasService(reader)
    query = AtlasQuery(top_n=100)
    expected = service.atlas(query)
    expected_comparison = service.compare(FunctionComparisonQuery(pfam_accessions=["PF00001"]))
    prepare_study(cohort, min_free_gib=0)
    with pytest.raises(ValueError, match="清单已变化"):
        reader.ensure_unchanged()
    fresh = MatouDataReader(cohort)
    monkeypatch.setattr(
        fresh, "load_sample", lambda *args: pytest.fail("预计算查询不得读取基因长表")
    )
    events = []
    with bind_execution_trace(parent_id="tool", writer=events.append):
        actual = MatouAtlasService(fresh).atlas(query)
    assert actual.ranks == expected.ranks and actual.observations == expected.observations
    comparison = MatouAtlasService(fresh).compare(
        FunctionComparisonQuery(pfam_accessions=["PF00001"])
    )
    assert comparison.points == expected_comparison.points
    starts = [e for e in events if e["phase"] == "started"]
    assert starts[0]["parent_id"] == "tool" and starts[0]["span_kind"] == "service"
    assert all(e["parent_id"] == starts[0]["observation_id"] for e in starts[1:])
    assert actual.metadata.provenance.filters["study_manifest_sha256"] == fresh.study_sha256


def test_changed_cache_and_source_binding_fail_closed(cohort):
    manifest = prepare_study(cohort, min_free_gib=0)
    path = cohort / manifest.profiles.path
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(ValueError, match="摘要"):
        MatouAtlasService(MatouDataReader(cohort)).atlas(AtlasQuery())


def test_sequence_exact_header_missing_duplicate_and_resource_guards(tmp_path):
    path = tmp_path / "sequences.fna"
    path.write_text(">MATOU-v1.5.1\nACGT\nNN\n>MATOU-v1.5.2\nGGTT\n")
    frame, source = extract_sequences(path, {1})
    assert frame.row(0, named=True)["sequence"] == "ACGTNN" and source.sha256
    with pytest.raises(ValueError, match="缺少"):
        extract_sequences(path, {3})
    with pytest.raises(ValueError, match="资源上限"):
        extract_sequences(path, {1}, max_bases=3)
    path.write_text(">MATOU-v1.5.1\nACGT\n>MATOU-v1.5.1\nGGTT\n")
    with pytest.raises(ValueError, match="无序或重复"):
        extract_sequences(path, {1})
    path.write_text(">MATOU-v1.4.1\nACGT\n")
    with pytest.raises(ValueError, match="标题"):
        extract_sequences(path, {1})
    path.write_text(">MATOU-v1.5.1\nACGT\n>MATOU-v1.5.2\n")
    with pytest.raises(ValueError, match="序列为空"):
        extract_sequences(path, {1})
    with pytest.raises(ValueError, match="geneID 不一致"):
        SequenceItem(geneID=2, header="MATOU-v1.5.1", sequence="ACGT")


def test_sequence_query_and_pagination(cohort, tmp_path):
    fasta = tmp_path / "matou.fna"
    fasta.write_text(">MATOU-v1.5.1\nACGTNN\n>MATOU-v1.5.2\nGGTT\n>MATOU-v1.5.3\nAAAA\n")
    prepare_study(cohort, fasta=fasta, sequence_pfams=["PF00002"], min_free_gib=0)
    service = MatouAtlasService(MatouDataReader(cohort))
    result = service.sequences(SequenceQuery(pfam_accession="PF00002", limit=1, offset=1))
    assert result.total == 2 and result.sequences[0].geneID == 2
    missing = service.sequences(SequenceQuery(gene_ids=[3]))
    assert missing.total == 0 and missing.unavailable_gene_ids == [3]
    validate_result(ToolName.RETRIEVE_GENE_SEQUENCES, result.model_dump())


def test_sequence_lowercase_iupac_from_gzip_preserves_source(tmp_path):
    import gzip
    import hashlib

    path = tmp_path / "official-format.fna.gz"
    with gzip.open(path, "wt", encoding="ascii") as stream:
        stream.write(">MATOU-v1.5.1\nAcgtrysw\nKmbdhvN\n>MATOU-v1.5.2\nacgt\n")
    before = path.read_bytes()
    frame, source = extract_sequences(path, {1})
    assert frame.row(0, named=True)["sequence"] == "ACGTRYSWKMBDHVN"
    assert source.sha256 == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before


def test_sequence_invalid_symbol_has_line_and_gene_diagnostic(tmp_path):
    path = tmp_path / "invalid.fna"
    path.write_text(">MATOU-v1.5.1\nacgtz\n")
    with pytest.raises(ValueError, match="第 2 行、geneID=1、非法字符='Z'"):
        extract_sequences(path, {1})


def test_failed_preparation_does_not_publish(cohort, tmp_path):
    fasta = tmp_path / "bad.fna"
    fasta.write_text(">wrong\nACGT\n")
    with pytest.raises(ValueError, match="标题"):
        prepare_study(cohort, fasta=fasta, sequence_pfams=["PF00001"], min_free_gib=0)
    assert not (cohort / "study_manifest.json").exists()
    # 中断目录被忽略，原来的独立计算仍可使用。
    assert MatouAtlasService(MatouDataReader(cohort)).atlas(AtlasQuery()).ranks


class ComplexFunctionModel:
    name = "complex-function-contract-test"

    async def route(self, question, context, tools):
        return RouteDecision(kind="analysis", rationale="完成总体功能谱与对应比较。")

    async def select_workflow(self, question, context, tools):
        return WorkflowSelection(
            workflow="multi_step",
            rationale="两个独立后端计算。",
            goals=["总体 Top100", "目标 DNA/RNA 相对信号比较"],
        )

    async def next_analysis_step(self, question, context, tools, steps, budget):
        if len(steps) < 2:
            tool, query = (
                ("function_atlas", {"top_n": 100})
                if not steps
                else ("compare_function_signals", {"pfam_accessions": ["PF00001"]})
            )
            return AnalysisDecision(
                action="tool",
                rationale="调用已注册科学工具。",
                call={"tool_name": tool, "arguments": {"query": query}},
            )
        return AnalysisDecision(
            action="finish",
            rationale="两个目标都有后端结果。",
            goal_checks=[
                {
                    "goal_id": i,
                    "status": "completed",
                    "evidence_steps": [i],
                    "reason": "科学工具已计算。",
                }
                for i in (1, 2)
            ],
        )

    async def stream_answer(self, question, plan, result):
        yield ModelStreamDelta(kind="answer", content="已完成总体排名和对应编码的相对贡献比较。")


@pytest.mark.anyio
async def test_mcp_remote_chat_complex_task_with_cache_binding(cohort, service_settings, tmp_path):
    prepare_study(cohort, min_free_gib=0)
    settings = service_settings.model_copy(update={"matou_data_dir": cohort})
    app = create_app(settings, token=TOKEN)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    local = settings.model_copy(
        update={"data_service_url": "http://127.0.0.1:8011", "data_service_token_file": token}
    )
    remote = RemoteToolGateway(local, transport=httpx.ASGITransport(app=app))
    direct = MCPToolGateway(
        create_server(
            ProcessedDataReader(settings.processed_data_dir), matou_reader=MatouDataReader(cohort)
        )
    )
    for tool, query in (
        (ToolName.FUNCTION_ATLAS, {"top_n": 100}),
        (ToolName.COMPARE_FUNCTION_SIGNALS, {"pfam_accessions": ["PF00001"]}),
    ):
        assert await remote.call(tool, {"query": query}) == await direct.call(
            tool, {"query": query}
        )
    result = await TaraAgent(ComplexFunctionModel(), remote).run("完成复杂功能任务")
    assert result.result["status"] == "completed"
    assert len(result.result["analysis_steps"]) == 2 and len(result.charts) == 2
    verifier = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "deploy/check_remote_function_task.py")
    )["verify"]
    report = await verifier(remote, pfam_accessions=["PF00001"])
    assert report["status"] == "passed" and report["matched_sampling_codes"] == 3
    with pytest.raises(ValueError, match="空结果"):
        await verifier(remote, pfam_accessions=["PF99999"])
    # 缓存版本必须进入请求绑定，不能只检查旧主清单。
    health = await remote.get_status()
    from fastapi.testclient import TestClient

    with TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        response = client.post(
            "/call",
            json={
                "tool_name": "function_atlas",
                "arguments": {"query": {}},
                "expected_code_sha256": health.code_sha256,
                "expected_generation": health.manifest.generation,
                "expected_matou_manifest_sha256": health.matou.manifest_sha256,
            },
        )
        assert response.status_code == 409
