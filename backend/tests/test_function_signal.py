import math

import polars as pl
import pytest
from pydantic import ValidationError

from tara_agent.analysis.function_signal import compute_function_signal
from tara_agent.analysis.function_signal_models import FunctionSignalContext, FunctionSignalQuery


@pytest.fixture
def inputs():
    context = FunctionSignalContext(
        assay="MetaT",
        sample_name="sample-A",
        taxon="test taxon",
        dataset_version="v1.5",
        occurrence_sha256="a" * 64,
        annotation_sha256="b" * 64,
    )
    occurrences = pl.DataFrame(
        {
            "geneID": [1, 2, 3],
            "samplename": ["sample-A"] * 3,
            "value": [0.6, 0.3, 0.1],
        },
        schema={"geneID": pl.Int64, "samplename": pl.String, "value": pl.Float64},
    )
    mapping = pl.DataFrame(
        {
            "geneID": [1, 1, 2, 4],
            "pfamAcc": ["PF00001", "PF00002", "PF00002", "PF00003"],
            "min_iEvalue": [1e-8, 1e-2, 1e-6, 1e-9],
        },
        schema={"geneID": pl.Int64, "pfamAcc": pl.String, "min_iEvalue": pl.Float64},
    )
    return occurrences, mapping, context


def test_unannotated_denominator_shared_genes_and_lineage(inputs):
    result = compute_function_signal(*inputs, FunctionSignalQuery())
    assert result.taxon_value_sum == pytest.approx(1)
    assert result.annotated_signal_fraction == pytest.approx(0.9)
    assert result.annotated_gene_count == 2
    assert result.genes_with_multiple_pfams == 1
    assert [(r.pfam_accession, r.value_sum) for r in result.observations] == [
        ("PF00002", pytest.approx(0.9)),
        ("PF00001", pytest.approx(0.6)),
    ]
    assert sum(r.fraction_of_observed_taxon_signal for r in result.observations) > 1
    assert result.metadata.provenance.filters["context"]["occurrence_sha256"] == "a" * 64
    assert result.annotation_interpretation == "candidate"
    assert "unconfirmed" in result.value_interpretation


def test_evalue_filter_requested_families_and_top_do_not_change_denominator(inputs):
    result = compute_function_signal(
        *inputs,
        FunctionSignalQuery(
            pfam_accessions=["PF00002"],
            max_i_evalue=1e-5,
            top_n=1,
        ),
    )
    assert result.taxon_value_sum == pytest.approx(1)
    assert result.annotated_signal_fraction == pytest.approx(0.9)
    assert result.observations[0].value_sum == pytest.approx(0.3)
    assert result.observations[0].fraction_of_observed_taxon_signal == pytest.approx(0.3)
    assert "candidate_evalue_condition" in {w.code for w in result.metadata.warnings}


def test_unobserved_request_is_distinguished_from_missing_annotation(inputs):
    result = compute_function_signal(
        *inputs,
        FunctionSignalQuery(
            pfam_accessions=["PF00003", "PF99999"],
        ),
    )
    assert result.unobserved_requested_pfams == ["PF00003", "PF99999"]
    assert [r.status for r in result.observations] == [
        "no_observed_gene_record",
        "not_in_retained_mapping",
    ]
    assert all(r.value_sum is None for r in result.observations)
    assert result.annotated_signal_fraction == pytest.approx(0.9)


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_explicit_zero_records_have_undefined_fraction(inputs, empty):
    occurrences, mapping, context = inputs
    occurrences = (
        occurrences.head(0) if empty else occurrences.with_columns(pl.lit(0.0).alias("value"))
    )
    result = compute_function_signal(occurrences, mapping, context, FunctionSignalQuery())
    assert result.taxon_value_sum == 0
    assert result.annotated_signal_fraction is None
    assert all(r.fraction_of_observed_taxon_signal is None for r in result.observations)
    assert "zero_observed_denominator" in {w.code for w in result.metadata.warnings}
    if not empty:
        assert all(r.status == "observed" and r.value_sum == 0 for r in result.observations)


def test_duplicate_keys_and_mixed_samples_fail(inputs):
    occurrences, mapping, context = inputs
    with pytest.raises(ValueError, match="重复基因"):
        compute_function_signal(
            pl.concat([occurrences, occurrences.head(1)]), mapping, context, FunctionSignalQuery()
        )
    with pytest.raises(ValueError, match="domain hits"):
        compute_function_signal(
            occurrences, pl.concat([mapping, mapping.head(1)]), context, FunctionSignalQuery()
        )
    with pytest.raises(ValueError, match="其他样本"):
        compute_function_signal(
            occurrences.with_columns(pl.lit("other").alias("samplename")),
            mapping,
            context,
            FunctionSignalQuery(),
        )


@pytest.mark.parametrize("bad", [-0.1, math.nan, math.inf])
def test_invalid_numbers_rejected(inputs, bad):
    occurrences, mapping, context = inputs
    for column, frame in (("value", occurrences), ("min_iEvalue", mapping)):
        changed = frame.with_columns(pl.lit(bad).alias(column))
        args = (changed, mapping) if column == "value" else (occurrences, changed)
        with pytest.raises(ValueError, match="有限且非负"):
            compute_function_signal(*args, context, FunctionSignalQuery())


def test_integer_gene_ids_cannot_be_silently_truncated(inputs):
    occurrences, mapping, context = inputs
    with pytest.raises(ValueError, match="类型"):
        compute_function_signal(
            occurrences.with_columns(pl.col("geneID").cast(pl.Float64)),
            mapping,
            context,
            FunctionSignalQuery(),
        )


def test_overflow_is_not_returned_as_valid_signal(inputs):
    occurrences, mapping, context = inputs
    with pytest.raises(ValueError, match="溢出"):
        compute_function_signal(
            occurrences.with_columns(pl.lit(1e308).alias("value")),
            mapping,
            context,
            FunctionSignalQuery(),
        )


@pytest.mark.parametrize(
    "parameters",
    [
        {"max_i_evalue": math.nan},
        {"max_i_evalue": math.inf},
        {"max_i_evalue": -1},
        {"pfam_accessions": []},
        {"pfam_accessions": ["PF00001", "PF00001"]},
        {"pfam_accessions": ["PF00001.1"]},
        {"top_n": 0},
        {"top_n": 501},
    ],
)
def test_invalid_query_is_rejected(parameters):
    with pytest.raises(ValidationError):
        FunctionSignalQuery(**parameters)


def test_common_positive_scaling_preserves_within_sample_shares(inputs):
    occurrences, mapping, context = inputs
    query = FunctionSignalQuery()
    baseline = compute_function_signal(*inputs, query)
    scaled = compute_function_signal(
        occurrences.with_columns((pl.col("value") * 137.5).alias("value")),
        mapping,
        context,
        query,
    )
    assert scaled.taxon_value_sum == pytest.approx(137.5 * baseline.taxon_value_sum)
    assert scaled.annotated_signal_fraction == pytest.approx(baseline.annotated_signal_fraction)
    for a, b in zip(baseline.observations, scaled.observations, strict=True):
        assert a.pfam_accession == b.pfam_accession
        assert a.fraction_of_observed_taxon_signal == pytest.approx(
            b.fraction_of_observed_taxon_signal
        )
    assert scaled.value_interpretation == baseline.value_interpretation


def test_no_retained_mapping_does_not_remove_taxon_denominator(inputs):
    result = compute_function_signal(*inputs, FunctionSignalQuery(max_i_evalue=0))
    assert result.taxon_value_sum == pytest.approx(1)
    assert result.annotated_value_sum == 0
    assert result.annotated_signal_fraction == 0
    assert result.observations == []


def test_roundoff_cannot_make_a_subset_share_larger_than_one(inputs):
    _, _, context = inputs
    occurrences = pl.DataFrame(
        {
            "geneID": list(range(1, 1002)),
            "samplename": [context.sample_name] * 1001,
            "value": [1.0] + [1e-15] * 1000,
        },
        schema={"geneID": pl.Int64, "samplename": pl.String, "value": pl.Float64},
    )
    mapping = pl.DataFrame(
        {
            "geneID": list(range(1, 1002)),
            "pfamAcc": ["PF00001"] * 1001,
            "min_iEvalue": [0.0] * 1001,
        },
        schema={"geneID": pl.Int64, "pfamAcc": pl.String, "min_iEvalue": pl.Float64},
    )
    result = compute_function_signal(occurrences, mapping, context, FunctionSignalQuery())
    assert result.annotated_signal_fraction == 1
    assert result.observations[0].fraction_of_observed_taxon_signal == pytest.approx(1)


@pytest.mark.parametrize("kind", ["null", "nonpositive_gene", "bad_pfam"])
def test_invalid_mapping_is_rejected_before_join(inputs, kind):
    occurrences, mapping, context = inputs
    changes = {
        "null": pl.lit(None, dtype=pl.Float64).alias("min_iEvalue"),
        "nonpositive_gene": pl.lit(0, dtype=pl.Int64).alias("geneID"),
        "bad_pfam": pl.lit("not-Pfam").alias("pfamAcc"),
    }
    with pytest.raises(ValueError):
        compute_function_signal(
            occurrences, mapping.with_columns(changes[kind]), context, FunctionSignalQuery()
        )
