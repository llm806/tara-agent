"""科研边界验证：子集过滤、两级均值、缺失不补零和显式样本身份。"""

import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import polars as pl
import pytest
from fastapi.testclient import TestClient
from scipy.spatial.distance import pdist, squareform
from scipy.stats import spearmanr

from tara_agent.analysis.community import CommunityService, average_rows
from tara_agent.analysis.community_models import CommunityQuery, FunctionEnvironmentQuery
from tara_agent.analysis.ecology_statistics import adjust_bh, association, envfit, nmds
from tara_agent.analysis.function_environment import FunctionEnvironmentService
from tara_agent.data.reader import ProcessedDataReader
from tara_agent.data.research_context import (
    ResearchContext,
    prepare_research,
    validate_sample_environment,
)
from tara_agent.data_service.app import create_app
from tara_agent.data_service.client import RemoteToolGateway

from .test_data_service import TOKEN, request_body
from .test_data_service import service_settings as service_settings


class CoreReader:
    def __init__(self):
        self.processed_dir = None
        self.manifest = SimpleNamespace(generation="test-core-v1")
        self.context = pl.DataFrame(
            [
                dict(
                    sample_id_pangaea=f"s{i}",
                    station="1",
                    depth="SRF",
                    size_fraction="0.8-5" if i < 3 else "0.8-20",
                    event_latitude=10.0,
                    event_longitude=20.0,
                    ocean_region="IO",
                    abs_lat=10.0,
                    temperature=float(i),
                )
                for i in range(1, 4)
            ]
        )
        self.counts = pl.DataFrame(
            {
                "amplicon": ["a", "b", "rare", "other", "non_euk"],
                "s1": [8, 2, 1, 9, 100],
                "s2": [0, 10, 0, 10, 100],
                "s3": [2, 8, 0, 10, 100],
            }
        )

    def load_sample_context(self):
        return self.context

    def marker_sample_ids(self, marker):
        return ["s1", "s2", "s3"]

    def scan_asv_metadata(self, marker):
        return pl.DataFrame(
            {
                "amplicon": ["a", "b", "rare", "other", "non_euk"],
                "taxonomy": ["Root;Eukaryota;Bacillariophyta"] * 3
                + ["Root;Eukaryota;Other", "Root;Bacteria;Bacillariophyta"],
            }
        ).lazy()

    def scan_abundance(self, marker, samples):
        return self.counts.select("amplicon", *samples).lazy()

    def collect(self, frame, **kwargs):
        return frame.collect()


class Research:
    sha256 = "b" * 64
    manifest = {"sources": {"sample_mapping": {"source": "verified specimen registry"}}}

    def __init__(self, mapping=None):
        self.mapping = mapping

    def ensure_unchanged(self):
        pass

    def frame(self, key, **kwargs):
        if self.mapping is None:
            raise ValueError("未准备研究资料：" + key)
        return self.mapping


def test_community_filter_denominator_and_average_before_exp():
    result = CommunityService(CoreReader(), Research()).analyze(
        CommunityQuery(
            markers=["v4", "v9"],
            outputs=["distribution", "diversity"],
            environment_source="context_stat",
            permutations=99,
        )
    )
    assert result.status == "completed"
    assert len(result.groups) == 2 and len(result.observations) == 6
    first = result.observations[0]
    assert first["retained_asv_count"] == 2
    assert first["taxon_read_count"] == 10
    # rare目标ASV只从分子过滤，全部真核分母仍包含它；细菌记录不进入分母或分子。
    assert first["eukaryotic_read_count"] == 20
    assert first["relative_abundance"] == 0.5
    h = -(0.8 * np.log(0.8) + 0.2 * np.log(0.2))
    assert first["shannon_index"] == pytest.approx(h)
    # 两个原粒径先各自均值，再等权合并；不是合并reads或先平均Shannon再取指数。
    assert result.groups[0]["exp_shannon"] == pytest.approx(((np.exp(h) + 1) / 2 + np.exp(h)) / 2)
    subset = CommunityService(CoreReader(), Research()).analyze(
        CommunityQuery(
            markers=["v4"],
            outputs=["diversity"],
            sample_ids=["s1"],
            environment_source="context_stat",
            permutations=99,
        )
    )
    assert subset.observations[0]["retained_asv_count"] == 0
    assert subset.observations[0]["shannon_index"] is None


def test_missing_paper_environment_blocks_only_dependent_outputs():
    result = CommunityService(CoreReader(), Research()).analyze(
        CommunityQuery(
            markers=["v4"],
            outputs=["distribution", "association"],
            permutations=99,
        )
    )
    assert result.status == "partial"
    assert [s.status for s in result.sections] == ["completed", "blocked"]
    assert result.associations == [] and result.groups
    rows = average_rows(
        [
            dict(group="a", x=None, sample_ids=["s1"]),
            dict(group="a", x=2.0, sample_ids=["s2"]),
        ],
        ["group"],
        ["x"],
    )
    assert rows[0]["x"] is None


def test_rank_statistics_match_scipy_and_missing_constant_guards():
    rows = [dict(x=x, y=y) for x, y in [(1, 4), (2, 3), (2, 2), (4, 1)]]
    result = association(rows + [dict(x=None, y=0)], "x", "y", permutations=999)
    assert result["rho"] == pytest.approx(spearmanr([1, 2, 2, 4], [4, 3, 2, 1]).statistic)
    assert result["sample_count"] == 4 and result["excluded_rows"] == 1
    assert result == association(rows + [dict(x=None, y=0)], "x", "y", permutations=999)
    assert 0 < result["p_value"] <= 1
    assert adjust_bh([0.01, None, 0.04, 0.03]) == pytest.approx([0.03, None, 0.04, 0.04])
    assert association([dict(x=1, y=i) for i in range(4)], "x", "y")["status"] == "blocked"


def test_nmds_reproducible_distances_and_environment_vector():
    matrix = np.random.default_rng(5).uniform(0.1, 1, (12, 4))
    fit = nmds(matrix, starts=3, max_iterations=500)
    repeat = nmds(matrix, starts=3, max_iterations=500)
    np.testing.assert_allclose(fit["distances"], squareform(pdist(matrix, "braycurtis")))
    np.testing.assert_allclose(fit["coordinates"], repeat["coordinates"])
    assert np.isfinite(fit["stress"]) and 0 <= fit["stress"] < 1
    coords = fit["coordinates"]
    rows = [dict(temperature=float(x + 2 * y), constant=1.0) for x, y in coords]
    vectors = envfit(coords, rows, ["temperature", "constant"], permutations=99)
    assert vectors[0]["r_squared"] == pytest.approx(1.0)
    assert vectors[1]["status"] == "blocked"
    with pytest.raises(ValueError, match="全零"):
        nmds(np.zeros((5, 4)))


def test_nmds_iteration_budget_does_not_relax_convergence():
    matrix = np.random.default_rng(5).uniform(0.1, 1, (12, 4))
    limited = nmds(matrix, starts=3, max_iterations=1)
    fitted = nmds(matrix, starts=3)
    assert not limited["converged"]
    assert limited["iterations"] == 1
    assert fitted["converged"]
    assert fitted["stress"] < limited["stress"]
    assert fitted["tolerance"] == limited["tolerance"] == 1e-7
    assert fitted["max_iterations"] == CommunityQuery().nmds_max_iterations == 1000
    assert CommunityQuery(nmds_max_iterations=5000).nmds_max_iterations == 5000
    for invalid in (99, 5001):
        with pytest.raises(ValueError):
            CommunityQuery(nmds_max_iterations=invalid)


def functional(mapping=None):
    reader = CoreReader()
    matou = SimpleNamespace(
        manifest_record=SimpleNamespace(sha256="a" * 64),
        list_samples=lambda assay: [
            SimpleNamespace(sample_name=n) for n in ["t1", "t2", "t3", "t4"]
        ],
        ensure_unchanged=lambda: None,
    )
    service = FunctionEnvironmentService(reader, matou, Research(mapping))
    service.atlas = SimpleNamespace(
        _check_taxon=lambda taxon: None,
        _mapping=lambda threshold: None,
        _samples=lambda assay, names, *args: [
            dict(sample_name=n, taxon_value_sum=10.0, families={"PF03382": float(i + 1)})
            for i, n in enumerate(["t1", "t2", "t3", "t4"])
            if n in names
        ],
    )
    return service


def test_functional_requires_mapping_and_counts_each_specimen_once():
    query = FunctionEnvironmentQuery(pfam_accession="PF03382", permutations=99)
    blocked = functional().analyze(query)
    assert blocked.status == "blocked" and blocked.points == []
    mapping = pl.DataFrame(
        {
            "assay": ["MetaT"] * 4,
            "sample_name": ["t1", "t2", "t3", "t4"],
            "sample_id_pangaea": ["s1", "s1", "s2", "s3"],
            "evidence": ["registry row"] * 4,
        }
    )
    result = functional(mapping).analyze(query)
    assert result.status == "completed" and len(result.points) == 3
    assert result.points[0]["sample_names"] == ["t1", "t2"]
    assert result.points[0]["target_signal"] == 3.0
    assert result.points[0]["relative_signal"] == pytest.approx(0.15)
    assert result.associations[0]["sample_count"] == 3
    assert result.mapping_coverage["mapped"] == 4


def sample_environment_rows(unit="degree_Celsius"):
    return pl.DataFrame(
        [
            dict(
                sample_id_pangaea=sid,
                variable="temperature",
                value=value,
                unit=unit,
                evidence="exact source barcode and ENA record",
                method="sensor feature median Q2",
                context_details='{"distance_lag_km":2.0,"time_lag":"PT1H"}',
            )
            for sid, value in [("s1", 999.0), ("s4", 4.0)]
        ]
    )


def test_verified_sample_environment_fills_missing_without_overwriting_core():
    mapping = pl.DataFrame(
        dict(
            assay=["MetaT"] * 4,
            sample_name=["t1", "t2", "t3", "t4"],
            sample_id_pangaea=["s1", "s2", "s3", "s4"],
            evidence=["explicit registry"] * 4,
        )
    )
    service = functional(mapping)
    supplement = sample_environment_rows()
    service.research.manifest = dict(
        files={"sample_environment": {}},
        sources={"sample_environment": {"source": "official sensors", "version": "Q2-v1"}},
    )
    service.research.frame = lambda key, **kwargs: (
        supplement if key == "sample_environment" else mapping
    )
    result = service.analyze(FunctionEnvironmentQuery(pfam_accession="PF03382", permutations=99))
    assert result.status == "completed"
    assert result.mapping_coverage["valid_statistical_samples"] == 4
    assert result.points[0]["environment_value"] == 1.0
    assert result.points[0]["environment_source"] == "context_stat"
    assert result.points[3]["environment_value"] == 4.0
    assert result.points[3]["environment_context_details"] == supplement["context_details"][1]
    assert result.associations[0]["rho"] == pytest.approx(1.0)
    assert result.metadata.provenance.filters["supplemental_environment_values_used"] == 1
    assert any(w.code == "supplemental_environment_context" for w in result.metadata.warnings)
    supplement = sample_environment_rows(unit="kelvin")
    with pytest.raises(ValueError, match="单位未经一致性核验"):
        service.analyze(FunctionEnvironmentQuery(pfam_accession="PF03382", permutations=99))


def test_sample_environment_rejects_ambiguous_or_nonfinite_rows():
    frame = sample_environment_rows()
    with pytest.raises(ValueError, match="重复"):
        validate_sample_environment(pl.concat([frame, frame]))
    with pytest.raises(ValueError, match="有限"):
        validate_sample_environment(frame.with_columns(pl.lit(float("inf")).alias("value")))
    with pytest.raises(ValueError, match="有效文本"):
        validate_sample_environment(frame.with_columns(pl.lit("").alias("evidence")))
    validate_sample_environment(frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("value")))


def test_sample_environment_bundle_allows_only_explicit_additional_ids(service_settings, tmp_path):
    from .test_matou_function_data import dataset, prepare

    directory = tmp_path / "matou-input"
    directory.mkdir()
    prepare(dataset.__wrapped__(directory))
    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(
        "assay\tsample_name\tsample_id_pangaea\tevidence\n"
        "MetaT\tT-A\tOFFICIAL_EXTRA\texplicit paper row\n",
        encoding="utf-8",
    )
    processed = service_settings.processed_data_dir
    args = dict(
        sample_mapping=mapping,
        mapping_source="official registry",
        mapping_version="v1",
        matou_dir=directory / "result",
    )
    with pytest.raises(ValueError, match="未知PANGAEA"):
        prepare_research(processed, tmp_path / "bad-research", **args)
    supplement = tmp_path / "sample-environment.tsv"
    sample_environment_rows().head(1).with_columns(
        pl.lit("OFFICIAL_EXTRA").alias("sample_id_pangaea")
    ).write_csv(supplement, separator="\t")
    prepare_research(
        processed,
        processed / "research",
        **args,
        sample_environment=supplement,
        sample_environment_source="official sensor context",
        sample_environment_version="source-sha256-v1",
    )
    bundle = ResearchContext(processed)
    assert bundle.manifest["version"] == "research-context-v2"
    data = bundle.frame("sample_environment", generation=bundle.manifest["core_generation"])
    assert data["sample_id_pangaea"].to_list() == ["OFFICIAL_EXTRA"]
    from tara_agent.data.matou_reader import MatouDataReader

    result = FunctionEnvironmentService(
        ProcessedDataReader(processed), MatouDataReader(directory / "result"), bundle
    ).analyze(FunctionEnvironmentQuery(pfam_accession="PF00002", permutations=99))
    assert result.points[0]["sample_id"] == "OFFICIAL_EXTRA"
    assert result.points[0]["environment_value"] == 999.0
    assert result.points[0]["environment_source"] == "sample_environment"
    assert result.mapping_coverage["mapped"] == 1
    assert result.mapping_coverage["matched_context_samples"] == 1
    with pytest.raises(ValueError, match="版本不匹配"):
        bundle.frame("sample_environment", generation="different-core")


def test_research_bundle_version_integrity_and_remote_pinning(service_settings, tmp_path):
    source = tmp_path / "environment.tsv"
    source.write_text("station\tdepth\tTemperature\n001\tSRF\t20\n", encoding="utf-8")
    processed = service_settings.processed_data_dir
    absent = ResearchContext(processed)
    prepare_research(
        processed,
        processed / "research",
        environment=source,
        environment_source="author fixed commit",
        units={"Temperature": "degree_Celsius"},
    )
    with pytest.raises(ValueError, match="出现或消失"):
        absent.ensure_unchanged()
    bundle = ResearchContext(processed)
    frame = bundle.frame(
        "environment", generation=ProcessedDataReader(processed).manifest.generation
    )
    assert frame["station"][0] == "1"
    with pytest.raises(ValueError, match="版本不匹配"):
        bundle.frame("environment", generation="wrong")
    with TestClient(create_app(service_settings, token=TOKEN)) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        body = request_body(
            client,
            "community_analysis",
            {"query": {"taxon": "Dinoflagellata", "markers": ["v4"], "outputs": ["distribution"]}},
        )
        assert client.post("/call", json=body).status_code == 409
        body["expected_research_manifest_sha256"] = bundle.sha256
        response = client.post("/call", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["result"], response.text
        assert response.json()["result"]["status"] == "completed"
    artifact = processed / "research" / "environment.parquet"
    artifact.write_bytes(artifact.read_bytes() + b"corruption")
    with pytest.raises(ValueError):
        bundle.frame("environment", generation=bundle.manifest["core_generation"])


@pytest.mark.anyio
async def test_remote_acceptance_reports_independent_missing_prerequisites(
    service_settings, tmp_path
):
    token = tmp_path / "token.txt"
    token.write_text(TOKEN)
    settings = service_settings.model_copy(
        update={
            "data_service_url": "http://127.0.0.1:8011",
            "data_service_token_file": token,
        }
    )
    gateway = RemoteToolGateway(
        settings, transport=httpx.ASGITransport(app=create_app(service_settings, token=TOKEN))
    )
    script = Path(__file__).parents[1] / "deploy" / "check_remote_research_tasks.py"
    outputs, report = await runpy.run_path(str(script))["verify"](gateway, "PF03382")
    assert len(report) == 3
    assert report[0]["status"] == "partial"
    assert report[1]["status"] == report[2]["status"] == "blocked"
    assert outputs["community_analysis"]["observations"]
