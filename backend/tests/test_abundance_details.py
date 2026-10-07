"""验证组成分母、未知分类、全范围排序和站点聚合。"""

from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from tara_agent.agent.charts import build_charts
from tara_agent.agent.context import build_answer_model_input
from tara_agent.agent.models import ToolName
from tara_agent.analysis.abundance_details import station_abundance
from tara_agent.analysis.compute import TaraComputeService
from tara_agent.analysis.compute_models import TaxonAbundanceObservation, TaxonAbundanceQuery
from tara_agent.data.catalog import DATASET_SPECS, DatasetKind
from tara_agent.data.preprocess import preprocess
from tara_agent.data.reader import ProcessedDataReader


@pytest.fixture
def composition_service(preprocessable_dataset_dir, tmp_path: Path):
    rows = [
        ("a", "Root;Eukaryota;Stramenopiles;Ochrophyta;Bacillariophyta;Order;Family;A;A_one", 6, 1),
        ("b", "Root;Eukaryota;Stramenopiles;Ochrophyta;Bacillariophyta;Order;Family;B;B_two", 2, 7),
        (
            "c",
            "Root;Eukaryota;Stramenopiles;Ochrophyta;Bacillariophyta;Order;Family;A;unclassified_A",
            2,
            0,
        ),
        ("d", "Root;Eukaryota;Other", 10, 2),
    ]
    for spec in DATASET_SPECS:
        if spec.kind is DatasetKind.AMPLICON:
            path = preprocessable_dataset_dir / spec.filename
            header = path.read_text(encoding="utf-8").splitlines()[0]
            lines = []
            for identifier, taxonomy, first, second in rows:
                confidence = ";".join(["100"] * len(taxonomy.split(";")))
                lines.append(
                    f"{identifier * 32}\t{taxonomy}\t{confidence}\t{first + second}\t"
                    f"{int(first > 0) + int(second > 0)}\tACGT\t{first}\t{second}"
                )
            path.write_text(header + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    processed = tmp_path / "processed"
    preprocess(preprocessable_dataset_dir, processed, enforce_expected_shape=False)
    return TaraComputeService(ProcessedDataReader(processed))


def test_genus_comparison_retains_both_samples_and_denominators(composition_service):
    result = composition_service.taxon_abundance(
        TaxonAbundanceQuery(
            marker="v9",
            taxon="Bacillariophyta",
            sample_ids=["TARA_TEST_001", "TARA_TEST_002"],
            taxonomic_rank="genus",
            limit=1,
        )
    )
    first, second = result.composition
    assert (first.taxon_label, second.taxon_label) == ("A", "B")
    assert first.read_count == 8 and first.asv_count == 2
    assert first.relative_abundance == pytest.approx(8 / 20)
    assert first.fraction_within_selected_taxon == pytest.approx(8 / 10)
    assert second.relative_abundance == pytest.approx(7 / 10)
    assert second.fraction_within_selected_taxon == pytest.approx(7 / 8)
    assert [s.total_groups for s in result.composition_summaries] == [2, 2]
    assert all(s.returned_groups == 1 for s in result.composition_summaries)


def test_species_unknown_is_retained_and_pagination_does_not_change_summary(composition_service):
    result = composition_service.taxon_abundance(
        TaxonAbundanceQuery(
            marker="v9",
            taxon="Bacillariophyta",
            sample_ids=["TARA_TEST_001"],
            taxonomic_rank="species",
            offset=1,
            limit=2,
        )
    )
    assert any(r.classification_status == "unresolved" for r in result.composition)
    assert result.composition[-1].cumulative_fraction == 1
    summary = result.composition_summaries[0]
    assert summary.unresolved_read_count == 2
    assert summary.dominant_taxon == "A_one"
    assert summary.dominant_fraction == 0.6


def test_highest_sample_is_selected_before_pagination(composition_service):
    result = composition_service.taxon_abundance(
        TaxonAbundanceQuery(
            marker="v9",
            taxon="Bacillariophyta",
            order_by="relative_abundance",
            limit=1,
        )
    )
    assert result.page.total == 2
    assert result.observations[0].sample_id == "TARA_TEST_002"


def test_station_leaders_are_computed_before_page_and_record_mapping(composition_service):
    result = composition_service.taxon_abundance(
        TaxonAbundanceQuery(
            marker="v9",
            taxon="Bacillariophyta",
            group_by="station",
            offset=1,
            limit=1,
        )
    )
    assert result.groups[0].station == "ST1"
    assert result.station_leaders[0].station == "ST2"
    assert result.station_leaders[0].mean_rank == result.station_leaders[0].max_rank == 1
    assert result.group_page.total == 2
    assert "context_general" in result.metadata.provenance.source_datasets


def test_absent_taxon_has_empty_composition_and_no_invented_dominant(composition_service):
    result = composition_service.taxon_abundance(
        TaxonAbundanceQuery(
            marker="v9",
            taxon="Missing",
            sample_ids=["TARA_TEST_001"],
            taxonomic_rank="species",
        )
    )
    assert result.composition == []
    summary = result.composition_summaries[0]
    assert summary.total_groups == 0 and summary.dominant_taxon is None


def test_station_means_are_unweighted_and_zero_libraries_remain_undefined():
    class Reader:
        def load_sample_context(self, samples):
            return pl.DataFrame(
                {
                    "sample_id_pangaea": ["a", "b", "c", "d", "e"],
                    "station": ["S1", "S1", "S2", "S3", None],
                }
            )

    values = [
        TaxonAbundanceObservation(
            sample_id=s,
            taxon_read_count=t,
            sample_total_read_count=n,
            relative_abundance=t / n if n else None,
        )
        for s, t, n in (("a", 9, 10), ("b", 100, 1000), ("c", 6, 10), ("d", 0, 0), ("e", 1, 10))
    ]
    rows, excluded = station_abundance(Reader(), values, "mean")
    assert [r.station for r in rows] == ["S2", "S1", "S3"]
    assert rows[1].mean_relative_abundance == pytest.approx(0.5)
    assert rows[1].max_relative_abundance == pytest.approx(0.9)
    assert rows[1].mean_rank == 2 and rows[1].max_rank == 1
    assert rows[2].mean_rank is None and rows[2].max_rank is None
    assert excluded == ["e"]


def test_composition_requires_bounded_samples():
    with pytest.raises(ValidationError):
        TaxonAbundanceQuery(marker="v9", taxon="Bacillariophyta", taxonomic_rank="genus")


def test_leaders_and_chart_do_not_invent_undefined_values():
    rows = [
        {"station": "S1", "mean_relative_abundance": 0.5, "max_relative_abundance": 0.9},
        {"station": "S2", "mean_relative_abundance": None, "max_relative_abundance": None},
    ]
    charts = build_charts(ToolName.TAXON_ABUNDANCE, {"group_by": "station", "groups": rows})
    assert len(charts) == 2
    assert all(chart.x == ["S1"] for chart in charts)
    assert charts[0].y == [0.5] and charts[1].y == [0.9]


def test_classification_answer_has_source_annotations_without_sequences():
    payload = {
        "asvs": [
            {
                "amplicon": "id",
                "taxonomy": "Root;Eukaryota;Ochrophyta",
                "confidence": "100;100;100",
                "sequence": "ACGT",
            }
        ]
        * 25
    }
    answer = build_answer_model_input("查询分类归属", ToolName.FIND_TAXA, payload)
    summary = answer["tool_result"]
    assert "asvs" not in summary
    assert len(summary["returned_taxonomy_annotations"]) == 20
    assert summary["returned_taxonomy_annotations"][0]["taxonomy"] == payload["asvs"][0]["taxonomy"]
    assert "sequence" not in summary["returned_taxonomy_annotations"][0]
