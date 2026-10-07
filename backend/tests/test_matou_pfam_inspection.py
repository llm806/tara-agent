"""验证命中、基因和 gene–Pfam 三种计数边界及准备来源绑定。"""

import csv
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/inspect_matou_pfam.py"
MODULE = runpy.run_path(str(SCRIPT))
inspect = MODULE["inspect"]


def prepared(tmp_path, rows=None):
    if rows is None:
        rows = [
            (1, "PF00001", "0", "0"),
            (1, "PF00001", "5", "1e-10"),
            (1, "PF00002", "0", "1e-5"),
            (2, "PF00001", "1", "1e-3"),
            (2, "PF00001", "1", "1"),
            (2, "PF00001", "1", "2"),
        ]
    directory = tmp_path / "prepared"
    directory.mkdir()
    path = directory / "pfam_hits.tsv"
    with path.open("w", encoding="utf-8", newline="") as source:
        writer = csv.writer(source, delimiter="\t", lineterminator="\n")
        writer.writerow(MODULE["HEADER"])
        for i, (gene, pfam, frame, evalue) in enumerate(rows, 1):
            writer.writerow([i, gene, frame, 300, 1, 100, pfam, 100, 1, 100, 20, evalue, "0.9"])
    report = {
        "status": "completed",
        "script_version": "2",
        "selection": {"taxon": "Example"},
        "outputs": {
            "pfam_hits": {
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        },
        "pfam": {
            "selected_hit_records": len(rows),
            "selected_genes_with_pfam": len({r[0] for r in rows}),
        },
    }
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return directory


def test_counts_distinguish_domains_pairs_and_genes_without_altering_inputs(tmp_path):
    directory = prepared(tmp_path)
    originals = {p: p.read_bytes() for p in directory.iterdir()}
    report = inspect(directory)
    assert report["selection"]["taxon"] == "Example"
    assert report["total_hit_records"] == 6
    assert report["unique_gene_pfam_pairs"] == 3
    assert report["same_gene_pfam_extra_hit_records"] == 3
    assert report["genes_with_pfam"] == 2
    assert report["genes_with_multiple_distinct_pfams"] == 1
    assert report["unique_pfam_accessions"] == 2
    assert list(report["i_Evalue_hit_bins"].values()) == [1] * 6
    assert report["frame_hit_counts"] == {"0": 2, "5": 1, "1": 3}
    assert report["script"]["sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert set(directory.iterdir()) == set(originals)


@pytest.mark.parametrize("evalue", ["NaN", "Infinity", "-1e-1000", "oops"])
def test_invalid_quality_values_fail(tmp_path, evalue):
    with pytest.raises(ValueError, match="i-Evalue"):
        inspect(prepared(tmp_path, [(1, "PF00001", "0", evalue)]))


@pytest.mark.parametrize("kind", ["digest", "count", "genes", "size", "status"])
def test_manifest_mismatches_fail(tmp_path, kind):
    directory = prepared(tmp_path)
    path = directory / "report.json"
    report = json.loads(path.read_text())
    if kind == "digest":
        report["outputs"]["pfam_hits"]["sha256"] = "0" * 64
    elif kind == "count":
        report["pfam"]["selected_hit_records"] += 1
    elif kind == "genes":
        report["pfam"]["selected_genes_with_pfam"] += 1
    elif kind == "size":
        report["outputs"]["pfam_hits"]["size_bytes"] += 1
    else:
        report["status"] = "failed"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        inspect(directory)


def test_memory_guard_stops_before_collecting_unbounded_hits(tmp_path):
    with pytest.raises(ValueError, match="保护上限"):
        inspect(prepared(tmp_path), max_hits=1)


@pytest.mark.parametrize("rows", [[], [(0, "PF00001", "0", "1")], [(1, "", "0", "1")]])
def test_empty_or_invalid_identifiers_fail(tmp_path, rows):
    with pytest.raises(ValueError):
        inspect(prepared(tmp_path, rows))


def test_source_changes_invalidate_result(tmp_path, monkeypatch):
    directory = prepared(tmp_path)
    original = inspect.__globals__["describe"]

    def describe_and_change(path):
        result = original(path)
        if path.name == "pfam_hits.tsv":
            state = path.stat()
            os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000))
        return result

    monkeypatch.setitem(inspect.__globals__, "describe", describe_and_change)
    with pytest.raises(ValueError, match="发生变化"):
        inspect(directory)


def test_cli_success_and_failure_are_explicit(tmp_path):
    directory = prepared(tmp_path)
    command = [sys.executable, str(SCRIPT), "--prepared-dir", str(directory)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    success = subprocess.run(command, capture_output=True, encoding="utf-8", env=env)
    assert success.returncode == 0, success.stderr
    assert json.loads(success.stdout)["status"] == "completed"
    failed = subprocess.run(
        command + ["--max-hits", "1"], capture_output=True, encoding="utf-8", env=env
    )
    assert failed.returncode == 1
    assert not failed.stdout


def test_mapping_counts_each_pair_once_preserves_other_pfams_and_publishes_last(tmp_path):
    directory = prepared(tmp_path)
    original = (directory / "pfam_hits.tsv").read_bytes()
    output = tmp_path / "mapping"
    report = inspect(directory, output_dir=output)
    assert (output / "gene_pfam.tsv").read_text() == (
        "geneID\tpfamAcc\tmin_iEvalue\n1\tPF00001\t0\n2\tPF00001\t0.001\n1\tPF00002\t0.00001\n"
    )
    assert report["threshold_sensitivity_counts"] == [
        {
            "condition": "min_iEvalue <= threshold",
            "threshold": "1e-5",
            "unique_gene_pfam_pairs": 2,
            "genes_with_pfam": 1,
            "unique_pfam_accessions": 2,
        },
        {
            "condition": "min_iEvalue <= threshold",
            "threshold": "1e-3",
            "unique_gene_pfam_pairs": 3,
            "genes_with_pfam": 2,
            "unique_pfam_accessions": 2,
        },
    ]
    assert (
        report["mapping_method"]["quality_policy"]
        == "all_original_pairs_preserved_no_threshold_selected"
    )
    assert json.loads((output / "report.json").read_text(encoding="utf-8")) == report
    assert not list(output.glob("*.partial"))
    assert (directory / "pfam_hits.tsv").read_bytes() == original
    info = report["outputs"]["gene_pfam"]
    assert info["sha256"] == hashlib.sha256((output / "gene_pfam.tsv").read_bytes()).hexdigest()


def test_minimum_uses_all_frames_without_dropping_weak_only_candidates(tmp_path):
    directory = prepared(
        tmp_path, [(1, "PF00001", "0", "2"), (1, "PF00001", "5", "1e-6"), (2, "PF00002", "1", "3")]
    )
    output = tmp_path / "mapping"
    report = inspect(directory, output_dir=output)
    assert "1\tPF00001\t0.000001\n" in (output / "gene_pfam.tsv").read_text()
    assert "2\tPF00002\t3\n" in (output / "gene_pfam.tsv").read_text()
    assert report["threshold_sensitivity_counts"][0]["genes_with_pfam"] == 1


@pytest.mark.parametrize("conditions", [("NaN",), ("-1",), ("0.001", "1e-3"), ()])
def test_bad_sensitivity_parameters_fail_before_publication(tmp_path, conditions):
    directory = prepared(tmp_path)
    output = tmp_path / "mapping"
    with pytest.raises(ValueError):
        inspect(directory, output_dir=output, sensitivity_evalues=conditions)
    assert not output.exists()


def test_mapping_protects_input_and_existing_run_directories(tmp_path):
    directory = prepared(tmp_path)
    for output in (directory, directory / "new", directory.parent):
        with pytest.raises(ValueError, match="分开"):
            inspect(directory, output_dir=output)
    output = tmp_path / "mapping"
    output.mkdir()
    marker = output / "report.json"
    marker.write_text("existing")
    with pytest.raises(FileExistsError):
        inspect(directory, output_dir=output)
    assert marker.read_text() == "existing"


def test_changed_input_during_map_write_never_publishes(tmp_path, monkeypatch):
    directory = prepared(tmp_path)
    original = inspect.__globals__["describe"]

    def change_input_after_map(path):
        result = original(path)
        if path.name == "gene_pfam.tsv.partial":
            source = directory / "pfam_hits.tsv"
            state = source.stat()
            os.utime(source, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000))
        return result

    monkeypatch.setitem(inspect.__globals__, "describe", change_input_after_map)
    output = tmp_path / "mapping"
    with pytest.raises(ValueError, match="发生变化"):
        inspect(directory, output_dir=output)
    assert not (output / "report.json").exists()
    assert not (output / "gene_pfam.tsv").exists()


def test_mapping_cli_uses_same_success_contract(tmp_path):
    directory = prepared(tmp_path)
    output = tmp_path / "mapping"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--prepared-dir",
            str(directory),
            "--output-dir",
            str(output),
            "--sensitivity-evalue",
            "1e-10",
        ],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["script_version"] == "2"
    assert report["threshold_sensitivity_counts"][0]["unique_gene_pfam_pairs"] == 1
    assert (output / "report.json").exists()
