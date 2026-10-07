"""从来源绑定到单样本计算的独立数据链路；不需要完整 MATOU 数据。"""

import gzip
import json
import runpy
import sys
from pathlib import Path

import pytest

from tara_agent.analysis.function_service import MatouFunctionService
from tara_agent.analysis.function_signal_models import FunctionSignalQuery
from tara_agent.data import matou_cli, matou_prepare
from tara_agent.data.matou_artifacts import describe
from tara_agent.data.matou_prepare import PreparationLimits, prepare_function_data
from tara_agent.data.matou_reader import MatouDataReader
from tara_agent.observability.execution import bind_execution_trace


def save_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def dataset(tmp_path):
    raw, prepared, occurrences, mapping = [
        tmp_path / n for n in ("raw", "prepared", "occurrences", "mapping")
    ]
    for directory in (raw, prepared, occurrences, mapping):
        directory.mkdir()
    selection = {
        "taxon": "Example taxon",
        "method": "exact_case_insensitive_taxName_or_semicolon_lineage_token",
    }
    genes = prepared / "gene_ids.tsv"
    genes.write_text("geneID\n1\n2\n3\n", encoding="utf-8")
    pfam_hits = prepared / "pfam_hits.tsv"
    pfam_hits.write_text("unused original hit rows", encoding="utf-8")
    for name in ("taxonomy.gz", "pfam.gz", "MetaG.gz", "MetaT.gz"):
        (raw / name).write_bytes(b"original source placeholder")
    prep = {
        "status": "completed",
        "script_version": "2",
        "selection": selection,
        "validation": {"selected_taxonomy_geneIDs_unique": True},
        "taxonomy": {
            "selected_unique_geneIDs": 3,
            "source": describe(raw / "taxonomy.gz").model_dump(),
        },
        "pfam": {"source": describe(raw / "pfam.gz").model_dump()},
        "outputs": {
            "gene_ids": describe(genes).model_dump(),
            "pfam_hits": describe(pfam_hits).model_dump(),
        },
    }
    save_json(prepared / "report.json", prep)
    mapping_file = mapping / "gene_pfam.tsv"
    mapping_file.write_text(
        "geneID\tpfamAcc\tmin_iEvalue\n1\tPF00001\t1e-8\n1\tPF00002\t0.01\n2\tPF00002\t1e-6\n",
        encoding="utf-8",
    )
    pfam = {
        "status": "completed",
        "script_version": "2",
        "scope": "prepared_pfam_hits",
        "selection": selection,
        "preparation_report": describe(prepared / "report.json").model_dump(),
        "source": describe(pfam_hits).model_dump(),
        "unique_gene_pfam_pairs": 3,
        "genes_with_pfam": 2,
        "validation": {"prepared_output_digest_and_counts_match": True, "inputs_unchanged": True},
        "mapping_method": {
            "pair": "unique_geneID_pfamAcc",
            "min_iEvalue": "minimum_over_all_original_hits_and_frames",
            "quality_policy": "all_original_pairs_preserved_no_threshold_selected",
        },
        "outputs": {"gene_pfam": describe(mapping_file).model_dump()},
    }
    save_json(mapping / "report.json", pfam)
    sample_file = occurrences / "samples.tsv"
    sample_file.write_text(
        "assay\tsamplename\ttotal_records\tselected_records\tzero_values\nMetaG\tG-A\t3\t3\t0\nMetaG\tG-empty\t2\t0\t0\nMetaT\tT-A\t3\t3\t0\n",
        encoding="utf-8",
    )
    outputs = {"samples": describe(sample_file).model_dump()}
    files = []
    for assay, sample, count in (("MetaG", "G-A", 2), ("MetaT", "T-A", 1)):
        path = occurrences / f"{assay}.selected.occurrences.tsv.gz"
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(
                f"geneid\tsamplename\tvalue\n1\t{sample}\t0.6\n2\t{sample}\t0.3\n3\t{sample}\t0.1\n"
            )
        outputs[assay] = describe(path).model_dump()
        files.append(
            {
                "assay": assay,
                "source": describe(raw / f"{assay}.gz").model_dump(),
                "sample_count": count,
                "selected_records": 3,
                "validation": {"full_gzip_read": True, "gene_sample_keys_unique": True},
            }
        )
    occ = {
        "status": "completed",
        "script_version": "1",
        "scope": "full_occurrence_files",
        "selection": selection,
        "preparation_report": describe(prepared / "report.json").model_dump(),
        "gene_ids": describe(genes).model_dump(),
        "selected_unique_geneIDs": 3,
        "outputs": outputs,
        "files": files,
        "validation": {"inputs_and_script_unchanged": True, "gene_list_digest_matches": True},
    }
    save_json(occurrences / "report.json", occ)
    return prepared, occurrences, mapping, tmp_path / "result"


def prepare(dataset, **kwargs):
    return prepare_function_data(*dataset, PreparationLimits(min_free_gib=0, **kwargs))


def update_input_report(dataset, key, contents):
    directory = dataset[2] if key == "gene_pfam" else dataset[1]
    path = directory / (
        "gene_pfam.tsv" if key == "gene_pfam" else "MetaG.selected.occurrences.tsv.gz"
    )
    if key == "gene_pfam":
        path.write_text(contents, encoding="utf-8")
    else:
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(contents)
    report = json.loads((directory / "report.json").read_text())
    report["outputs"][key] = describe(path).model_dump()
    save_json(directory / "report.json", report)


def test_conversion_reader_and_service_preserve_denominator_and_sources(dataset):
    originals = {p: p.read_bytes() for d in dataset[:3] for p in d.iterdir()}
    manifest = prepare(dataset)
    assert len(manifest.samples) == 3
    reader = MatouDataReader(dataset[3])
    result = MatouFunctionService(reader).analyze_sample("MetaG", "G-A", FunctionSignalQuery())
    assert result.taxon_value_sum == pytest.approx(1)
    assert result.annotated_signal_fraction == pytest.approx(0.9)
    assert result.observations[0].pfam_accession == "PF00002"
    assert result.observations[0].value_sum == pytest.approx(0.9)
    assert result.metadata.provenance.filters["source_files"]["raw_taxonomy"]["sha256"]
    assert result.context.occurrence_sha256 == reader.sample_artifact("MetaG", "G-A").sha256
    assert all(p.read_bytes() == value for p, value in originals.items())
    empty = MatouFunctionService(reader).analyze_sample("MetaG", "G-empty", FunctionSignalQuery())
    assert empty.annotated_signal_fraction is None
    assert empty.observed_gene_count == 0


@pytest.mark.parametrize(
    "kind",
    [
        "binding",
        "selection",
        "status",
        "method",
        "scope",
        "count",
        "membership",
        "size",
        "validation",
    ],
)
def test_invalid_evidence_never_publishes(dataset, kind):
    directory = dataset[2]
    path = directory / "report.json"
    report = json.loads(path.read_text())
    if kind == "binding":
        report["preparation_report"]["sha256"] = "0" * 64
    elif kind == "selection":
        report["selection"]["taxon"] = "different taxon"
    elif kind == "status":
        report["status"] = "failed"
    elif kind == "method":
        report["mapping_method"]["quality_policy"] = "thresholded"
    elif kind == "scope":
        report["scope"] = "file_prefix"
    elif kind == "count":
        report["unique_gene_pfam_pairs"] = 2
    elif kind == "membership":
        update_input_report(
            dataset,
            "gene_pfam",
            "geneID\tpfamAcc\tmin_iEvalue\n1\tPF00001\t1e-8\n1\tPF00002\t0.01\n4\tPF00002\t1e-6\n",
        )
        return _assert_failed(dataset)
    elif kind == "validation":
        report["validation"]["inputs_unchanged"] = False
    else:
        report["outputs"]["gene_pfam"]["size_bytes"] += 1
    save_json(path, report)
    _assert_failed(dataset)


def _assert_failed(dataset):
    with pytest.raises(ValueError):
        prepare(dataset)
    assert not (dataset[3] / "manifest.json").exists()


@pytest.mark.parametrize(
    "rows",
    [
        "1\tG-A\t0.6\n1\tG-A\t0.3\n3\tG-A\t0.1\n",
        "1\tG-A\t0.6\n2\tG-A\tNaN\n3\tG-A\t0.1\n",
        "1\tG-A\t0.6\n2\tG-A\t-1e-1000\n3\tG-A\t0.1\n",
        "1\tG-A\t0.6\n2\tG-A\t1e-1000\n3\tG-A\t0.1\n",
        "1\tG-A\t0.6\n2\tG-A\t0.3\n4\tG-A\t0.1\n",
        "1\tG-A\t0.6\n2\tG-A\t0.3\n",
        "1\t../foreign\t0.6\n2\tG-A\t0.3\n3\tG-A\t0.1\n",
    ],
)
def test_bad_quantitative_records_rejected_even_with_updated_digest(dataset, rows):
    update_input_report(dataset, "MetaG", "geneid\tsamplename\tvalue\n" + rows)
    _assert_failed(dataset)


def test_candidate_duplicate_pair_is_not_silently_merged(dataset):
    update_input_report(
        dataset,
        "gene_pfam",
        "geneID\tpfamAcc\tmin_iEvalue\n1\tPF00001\t1e-8\n1\tPF00001\t0.01\n2\tPF00002\t1e-6\n",
    )
    _assert_failed(dataset)


def test_gzip_integrity_checked_to_eof(dataset):
    path = dataset[1] / "MetaG.selected.occurrences.tsv.gz"
    data = bytearray(path.read_bytes())
    data[-8] ^= 1
    path.write_bytes(data)
    report_path = dataset[1] / "report.json"
    report = json.loads(report_path.read_text())
    report["outputs"]["MetaG"] = describe(path).model_dump()
    save_json(report_path, report)
    with pytest.raises(gzip.BadGzipFile):
        prepare(dataset)
    assert not (dataset[3] / "manifest.json").exists()


def test_limits_output_isolation_and_existing_directory(dataset):
    with pytest.raises(ValueError, match="保护上限"):
        prepare(dataset, max_sample_genes=2)
    assert not dataset[3].exists()
    with pytest.raises(ValueError, match="分开"):
        prepare_function_data(*dataset[:3], dataset[0] / "nested")
    dataset[3].mkdir()
    with pytest.raises(FileExistsError):
        prepare(dataset)


def test_reader_rejects_digest_change_and_never_falls_back_to_gzip(dataset):
    prepare(dataset)
    reader = MatouDataReader(dataset[3])
    path = dataset[3] / reader.sample_artifact("MetaG", "G-A").path
    data = bytearray(path.read_bytes())
    data[10] ^= 1
    path.write_bytes(data)
    with pytest.raises(ValueError, match="SHA-256"):
        reader.load_sample("MetaG", "G-A")
    with pytest.raises(ValueError, match="未知"):
        reader.load_sample("MetaG", "T-A")


def test_manifest_path_escape_and_changed_manifest_are_rejected(dataset):
    prepare(dataset)
    reader = MatouDataReader(dataset[3])
    path = dataset[3] / "manifest.json"
    report = json.loads(path.read_text())
    report["samples"][0]["artifact"]["path"] = "../outside.parquet"
    save_json(path, report)
    with pytest.raises(ValueError, match="清单已变化"):
        reader.load_gene_pfam()
    with pytest.raises(ValueError, match="相对路径"):
        MatouDataReader(dataset[3])


def test_cli_prepare_list_and_analyze_success_and_failed_query(dataset, monkeypatch, capsys):
    argv = ["matou", "prepare", "--min-free-gib", "0"]
    for name, path in zip(
        ("prepared-dir", "occurrences-dir", "mapping-dir", "output-dir"), dataset, strict=True
    ):
        argv += [f"--{name}", str(path)]
    monkeypatch.setattr(sys, "argv", argv)
    assert matou_cli.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    for command in ("samples", "analyze"):
        argv = ["matou", command, "--data-dir", str(dataset[3]), "--assay", "MetaG"]
        if command == "analyze":
            argv += ["--sample", "G-A", "--pfam", "PF00001"]
        monkeypatch.setattr(sys, "argv", argv)
        assert matou_cli.main() == 0
        result = json.loads(capsys.readouterr().out)
        assert (
            "samples" in result
            if command == "samples"
            else result["observations"][0]["value_sum"] == 0.6
        )
    monkeypatch.setattr(sys, "argv", argv + ["--max-i-evalue", "NaN"])
    assert matou_cli.main() == 1
    assert not capsys.readouterr().out


def test_input_change_during_conversion_prevents_publication(dataset, monkeypatch):
    original = matou_prepare._artifact

    def change_input(*args):
        result = original(*args)
        path = dataset[0] / "gene_ids.tsv"
        path.write_text(path.read_text() + "4\n")
        return result

    monkeypatch.setattr(matou_prepare, "_artifact", change_input)
    with pytest.raises(ValueError, match="输入发生变化"):
        prepare(dataset)
    assert not (dataset[3] / "manifest.json").exists()


def test_disk_and_time_guards_prevent_publication(dataset, monkeypatch):
    with pytest.raises(ValueError, match="保留额度"):
        prepare_function_data(*dataset, PreparationLimits(min_free_gib=1e12))
    assert not dataset[3].exists()
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return 0 if calls == 1 else 1

    monkeypatch.setattr(matou_prepare.time, "monotonic", clock)
    with pytest.raises(ValueError, match="时间保护"):
        prepare(dataset, max_seconds=0.1)
    assert not dataset[3].exists()


def test_service_and_reads_share_actual_trace_tree_on_success_and_failure(dataset):
    prepare(dataset)
    reader = MatouDataReader(dataset[3])
    service = MatouFunctionService(reader)
    events = []
    with bind_execution_trace(parent_id="tool-parent", writer=events.append):
        service.analyze_sample("MetaG", "G-A", FunctionSignalQuery())
    starts = [event for event in events if event["phase"] == "started"]
    root = starts[0]
    assert root["span_kind"] == "service" and root["parent_id"] == "tool-parent"
    assert len(starts) == 3
    assert all(event["parent_id"] == root["observation_id"] for event in starts[1:])
    assert all(event["phase"] == "completed" for event in events if event not in starts)
    assert starts[1]["details"]["data_sources"][0]["manifest_sha256"]
    mapping_path = dataset[3] / reader.manifest.gene_pfam.path
    mapping_path.write_bytes(b"damaged")
    events = []
    with (
        bind_execution_trace(parent_id="tool-parent", writer=events.append),
        pytest.raises(ValueError),
    ):
        service.analyze_sample("MetaG", "G-A", FunctionSignalQuery())
    assert {event["span_kind"] for event in events if event["phase"] == "failed"} == {
        "data",
        "service",
    }


def test_actual_upstream_scripts_produce_compatible_reports(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "deploy"
    taxonomy = runpy.run_path(str(scripts / "inspect_matou_taxonomy.py"))
    extraction = runpy.run_path(str(scripts / "extract_matou_occurrences.py"))
    pfam = runpy.run_path(str(scripts / "inspect_matou_pfam.py"))
    raw = tmp_path / "raw"
    raw.mkdir()
    taxonomy_path, pfam_path = raw / "taxonomy.gz", raw / "pfam.gz"
    with gzip.open(taxonomy_path, "wt", encoding="utf-8") as stream:
        stream.write("geneID\ttaxId\ttaxName\ttaxRank\ttaxLineage\n")
        for gene in (1, 2, 3):
            stream.write(f"{gene}\t1\tExample\tphylum\troot;\n")
    with gzip.open(pfam_path, "wt", encoding="utf-8") as stream:
        stream.write("\t".join(pfam["HEADER"]) + "\n")
        stream.write("1\t1\t0\t100\t1\t20\tPF00001\t20\t1\t20\t20\t1e-8\t0.9\n")
    prepared, occurrences, mapping, output = [
        tmp_path / n for n in ("prepared", "occurrences", "mapping", "function")
    ]
    taxonomy["prepare_pfam"](
        taxonomy_path, pfam_path, "Example", prepared, describe(taxonomy_path).sha256
    )
    paths = []
    for assay in ("MetaG", "MetaT"):
        path = raw / f"{assay}.gz"
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(
                f"geneid\tsamplename\tvalue\n1\t{assay}-A\t0.6\n2\t{assay}-A\t0.3\n3\t{assay}-A\t0.1\n"
            )
        paths.append(path)
    extraction["extract"](prepared, *paths, occurrences, min_free_gib=0)
    pfam["inspect"](prepared, output_dir=mapping)
    prepare_function_data(prepared, occurrences, mapping, output, PreparationLimits(min_free_gib=0))
    result = MatouFunctionService(MatouDataReader(output)).analyze_sample(
        "MetaG", "MetaG-A", FunctionSignalQuery()
    )
    assert result.context.taxon == "Example"
    assert result.annotated_signal_fraction == pytest.approx(0.6)
