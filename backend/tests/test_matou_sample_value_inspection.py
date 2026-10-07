import gzip
import hashlib
import json
import runpy
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "deploy/inspect_matou_sample_values.py")
)
inspect = MODULE["inspect"]
first_block = MODULE["first_block"]
HEADER = "geneid\tsamplename\tvalue\n"


def digest(path):
    data = path.read_bytes()
    return {"path": str(path), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_gzip(path, text):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture
def dataset(tmp_path):
    raw, prepared, extracted = (tmp_path / n for n in ("raw", "prepared", "extracted"))
    for p in (raw, prepared, extracted):
        p.mkdir()
    selection = {"taxon": "test taxon", "method": "exact_lineage"}
    pfam = prepared / "pfam_hits.tsv"
    pfam.write_text(
        MODULE["PFAM_HEADER"].decode() + "\n"
        "1\t1\t0\t100\t1\t20\tPF00001\t20\t1\t20\t10\t1e-4\t1\n"
        "2\t1\t3\t100\t1\t20\tPF00001\t20\t1\t20\t20\t1e-6\t1\n"
        "3\t1\t0\t100\t1\t20\tPF00002\t20\t1\t20\t5\t0.2\t0.8\n"
        "4\t2\t0\t100\t1\t20\tPF00001\t20\t1\t20\t5\t0.01\t0.8\n",
        encoding="utf-8",
    )
    prep = {
        "status": "completed",
        "script_version": "2",
        "selection": selection,
        "outputs": {"pfam_hits": digest(pfam)},
        "pfam": {"selected_hit_records": 4},
    }
    prep_path = prepared / "report.json"
    prep_path.write_text(json.dumps(prep), encoding="utf-8")
    sample_path = extracted / "samples.tsv"
    sample_path.write_text(
        "assay\tsamplename\ttotal_records\tselected_records\tzero_values\n"
        "MetaG\tG-A\t3\t2\t0\nMetaG\tG-B\t1\t1\t0\n"
        "MetaT\tT-A\t3\t2\t0\nMetaT\tT-B\t1\t1\t0\n",
        encoding="utf-8",
    )
    manifest = {
        "status": "completed",
        "script_version": "1",
        "scope": "full_occurrence_files",
        "selection": selection,
        "preparation_report": digest(prep_path),
        "outputs": {"samples": digest(sample_path)},
        "files": [],
    }
    raw_values = {
        "MetaG": [(1, "0.6"), (2, "0.1"), (3, "0.3")],
        "MetaT": [(1, "0.2"), (2, "0.3"), (3, "0.5")],
    }
    kept = {"MetaG": {1, 3}, "MetaT": {2, 3}}
    for assay in ("MetaG", "MetaT"):
        prefix = assay[-1]
        rows = "".join(f"{g}\t{prefix}-A\t{v}\n" for g, v in raw_values[assay])
        selected_rows = "".join(
            f"{g}\t{prefix}-A\t{v}\n" for g, v in raw_values[assay] if g in kept[assay]
        )
        original = raw / f"{assay}.gz"
        subset = extracted / f"{assay}.selected.occurrences.tsv.gz"
        write_gzip(original, HEADER + rows + f"3\t{prefix}-B\t0.1\n")
        write_gzip(subset, HEADER + selected_rows + f"3\t{prefix}-B\t0.1\n")
        manifest["files"].append(
            {
                "assay": assay,
                "source": digest(original),
                "sample_count": 2,
                "total_records": 4,
                "selected_records": 3,
                "sample_examples": [f"{prefix}-A", f"{prefix}-B"],
            }
        )
        manifest["outputs"][assay] = digest(subset)
    manifest_path = extracted / "report.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return raw, extracted, prepared, tmp_path / "result"


def refresh_manifest(dataset):
    raw, extracted, _, _ = dataset
    p = extracted / "report.json"
    report = json.loads(p.read_text())
    report["outputs"]["samples"] = digest(extracted / "samples.tsv")
    for entry in report["files"]:
        assay = entry["assay"]
        entry["source"] = digest(raw / f"{assay}.gz")
        report["outputs"][assay] = digest(extracted / f"{assay}.selected.occurrences.tsv.gz")
    p.write_text(json.dumps(report), encoding="utf-8")


def test_sums_preservation_and_coverage_are_separate_and_do_not_imply_units(dataset):
    paths = [p for directory in dataset[:3] for p in directory.iterdir()]
    before = {p: p.read_bytes() for p in paths}
    report = inspect(*dataset)
    g, t = report["files"]
    assert g["original"]["value_sum"] == t["original"]["value_sum"] == "1.0"
    assert g["selected"]["value_sum"] == "0.9"
    assert t["selected"]["value_sum"] == "0.8"
    assert g["original"]["selected_gene_values_matched"] == 2
    assert all(r["selected_values_match_original"] for r in (g, t))
    diag = g["pfam_diagnostics"]
    assert diag["extra_hits_for_same_gene_pfam"] == 1
    assert diag["gene_pfam_pairs_observed_in_multiple_frames"] == 1
    all_hits, strict, relaxed = diag["candidate_signal_coverage"]
    assert all_hits["genes_with_pfam"] == 1
    assert all_hits["unannotated_value_sum"] == "0.3"
    assert Decimal(all_hits["annotated_fraction_of_selected_signal"]) == pytest.approx(
        Decimal(2) / 3
    )
    assert Decimal(all_hits["membership_weighted_fraction_of_selected_signal"]) > 1
    assert strict["genes_with_pfam"] == relaxed["genes_with_pfam"] == 1
    assert t["pfam_diagnostics"]["candidate_signal_coverage"][1]["genes_with_pfam"] == 0
    assert "unit" not in g["original"]
    assert json.loads((dataset[3] / "report.json").read_text(encoding="utf-8")) == report
    assert {p: p.read_bytes() for p in paths} == before


@pytest.mark.parametrize(
    "which, replacement", [("value", "3\tG-A\t0.4\n"), ("gene", "4\tG-A\t0.3\n")]
)
def test_changed_or_absent_selected_gene_fails_without_success_report(dataset, which, replacement):
    subset = dataset[1] / "MetaG.selected.occurrences.tsv.gz"
    write_gzip(subset, HEADER + "1\tG-A\t0.6\n" + replacement + "3\tG-B\t0.1\n")
    refresh_manifest(dataset)
    with pytest.raises(ValueError, match="原值不一致|不存在"):
        inspect(*dataset)
    assert not (dataset[3] / "report.json").exists()


def test_row_budget_does_not_publish_partial_sample_sum(dataset):
    with pytest.raises(ValueError, match="行数上限"):
        inspect(*dataset, max_raw_rows=2)
    assert not (dataset[3] / "report.json").exists()


def test_total_selected_gene_budget_applies_across_both_assays(dataset):
    with pytest.raises(ValueError, match="目标基因总数"):
        inspect(*dataset, max_selected_genes=2)
    assert not (dataset[3] / "report.json").exists()


@pytest.mark.parametrize("bad", ["-1e-1000", "NaN", "Infinity", "n/a"])
def test_invalid_original_value_fails_even_when_gene_is_not_selected(dataset, bad):
    path = dataset[0] / "MetaG.gz"
    write_gzip(path, HEADER + f"1\tG-A\t0.6\n2\tG-A\t{bad}\n3\tG-A\t0.3\n3\tG-B\t0.1\n")
    refresh_manifest(dataset)
    with pytest.raises(ValueError, match="数值"):
        inspect(*dataset)
    assert not (dataset[3] / "report.json").exists()


def test_sample_count_disagreement_is_not_treated_as_normalization(dataset):
    p = dataset[1] / "samples.tsv"
    p.write_text(p.read_text().replace("G-A\t3", "G-A\t2").replace("G-B\t1", "G-B\t2"))
    refresh_manifest(dataset)
    with pytest.raises(ValueError, match="完整样本行数"):
        inspect(*dataset)


def test_preparation_link_or_pfam_digest_change_fails(dataset):
    p = dataset[2] / "report.json"
    p.write_text(p.read_text() + " ")
    with pytest.raises(ValueError, match="绑定同一"):
        inspect(*dataset)
    p.write_text(p.read_text()[:-1])
    pfam = dataset[2] / "pfam_hits.tsv"
    pfam.write_bytes(pfam.read_bytes().replace(b"1e-6", b"1e-7"))
    with pytest.raises(ValueError, match="Pfam 产物"):
        inspect(*dataset)
    assert not (dataset[3] / "report.json").exists()


def test_input_changed_after_its_read_is_detected_before_publish(dataset, monkeypatch):
    real = inspect.__globals__["coverage"]

    def change(*args):
        p = dataset[0] / "MetaG.gz"
        p.write_bytes(p.read_bytes() + b" ")
        return real(*args)

    monkeypatch.setitem(inspect.__globals__, "coverage", change)
    with pytest.raises(ValueError, match="输入或脚本发生变化"):
        inspect(*dataset)
    assert not (dataset[3] / "report.json").exists()


def test_output_must_be_new_and_outside_source_and_prepared_directories(dataset):
    with pytest.raises(ValueError, match="输出必须"):
        inspect(*dataset[:3], dataset[0] / "result")
    dataset[3].mkdir()
    with pytest.raises(FileExistsError):
        inspect(*dataset)


@pytest.mark.parametrize(
    "option",
    [
        {"max_seconds": float("nan")},
        {"max_raw_rows": 0},
        {"max_selected_genes": 1_000_001},
        {"max_hits": -1},
    ],
)
def test_invalid_budget_rejected_before_creating_output(dataset, option):
    with pytest.raises(ValueError, match="保护参数"):
        inspect(*dataset, **option)
    assert not dataset[3].exists()


def test_first_block_stops_before_unread_tail_and_accepts_single_sample_eof(tmp_path):
    path = tmp_path / "data.gz"
    write_gzip(path, HEADER + "1\tA\t1e-8\n1\tB\t0\ninvalid unread tail\n")
    report, values = first_block(path, "A", 1, 10, time.monotonic() + 20, keep=True)
    assert report["next_sample_observed"] == "B"
    assert report["whole_file_digest_checked"] is False
    assert values == {1: Decimal("1e-8")}
    write_gzip(path, HEADER + "1\tA\t0\n")
    report, values = first_block(path, "A", 1, 10, time.monotonic() + 20, keep=True)
    assert report["stopped_at"] == "file_eof"
    stats = MODULE["coverage"](values, {})["candidate_signal_coverage"]
    assert all(r["annotated_fraction_of_selected_signal"] is None for r in stats)


def test_duplicate_gene_wrong_start_sample_or_expired_budget_fails(tmp_path):
    path = tmp_path / "data.gz"
    write_gzip(path, HEADER + "1\tA\t1\n1\tA\t1\n")
    with pytest.raises(ValueError, match="严格递增"):
        first_block(path, "A", 2, 10, time.monotonic() + 20)
    with pytest.raises(ValueError, match="时间上限"):
        first_block(path, "A", 2, 10, time.monotonic() - 1)
    with pytest.raises(ValueError, match="完整样本行数"):
        first_block(path, "B", 1, 10, time.monotonic() + 20)


def test_cli_publishes_json_on_success_and_only_error_on_failed_reuse(dataset, monkeypatch, capsys):
    arguments = ["inspect_matou_sample_values.py"]
    for name, path in zip(
        ("raw-dir", "occurrences-dir", "prepared-dir", "output-dir"), dataset, strict=True
    ):
        arguments.extend((f"--{name}", str(path)))
    monkeypatch.setattr(sys, "argv", arguments)
    assert MODULE["main"]() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "completed"
    assert json.loads(captured.out) == json.loads(
        (dataset[3] / "report.json").read_text(encoding="utf-8")
    )
    assert MODULE["main"]() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "检查失败" in captured.err


def test_corrupt_gzip_footer_is_rejected_when_block_reaches_eof(tmp_path):
    path = tmp_path / "data.gz"
    write_gzip(path, HEADER + "1\tA\t1\n")
    data = bytearray(path.read_bytes())
    data[-8] ^= 1
    path.write_bytes(data)
    with pytest.raises(gzip.BadGzipFile):
        first_block(path, "A", 1, 10, time.monotonic() + 20)
