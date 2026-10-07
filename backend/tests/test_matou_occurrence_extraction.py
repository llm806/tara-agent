"""验证全量筛选的键唯一性、来源绑定、原始数值保留和失败不发布。"""

import gzip
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/extract_matou_occurrences.py"
MODULE = runpy.run_path(str(SCRIPT))
extract = MODULE["extract"]
HEADER = b"geneid\tsamplename\tvalue\n"


def inputs(tmp_path, g_rows=b"1\tA\t0\n2\tA\t1e-8\n3\tB\t2\n", t_rows=None):
    raw = tmp_path / "raw"
    raw.mkdir()
    paths = []
    for label, rows in (("G", g_rows), ("T", t_rows if t_rows is not None else g_rows)):
        path = raw / f"{label}.gz"
        path.write_bytes(gzip.compress(HEADER + rows))
        paths.append(path)
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    genes = prepared / "gene_ids.tsv"
    genes.write_bytes(b"geneID\n1\n3\n")
    (prepared / "report.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "script_version": "2",
                "validation": {"selected_taxonomy_geneIDs_unique": True},
                "selection": {"taxon": "OtherTaxon", "method": "exact_node_or_ancestor"},
                "taxonomy": {"selected_unique_geneIDs": 2},
                "outputs": {
                    "gene_ids": {
                        "path": str(genes),
                        "size_bytes": genes.stat().st_size,
                        "sha256": hashlib.sha256(genes.read_bytes()).hexdigest(),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return prepared, *paths, tmp_path / "output"


def test_full_scan_preserves_raw_rows_and_censuses_all_samples(tmp_path):
    prepared, g, t, output = inputs(
        tmp_path,
        b"1\tB\t0.00000000000000000001\r\n2\tB\t0\r\n3\tA\t2e-8\n",
        b"2\tRNA\t1\n3\tRNA\t0\n",
    )
    originals = {p: p.read_bytes() for p in (g, t, prepared / "gene_ids.tsv")}
    report = extract(prepared, g, t, output, min_free_gib=0)
    assert report["scope"] == "full_occurrence_files"
    assert report["selection"]["taxon"] == "OtherTaxon"
    assert [f["sample_count"] for f in report["files"]] == [2, 1]
    assert [f["selected_records"] for f in report["files"]] == [2, 1]
    assert report["files"][1]["zero_values"] == 1
    assert gzip.decompress((output / "MetaG.selected.occurrences.tsv.gz").read_bytes()) == (
        HEADER + b"1\tB\t0.00000000000000000001\r\n3\tA\t2e-8\n"
    )
    assert (output / "samples.tsv").read_text().splitlines() == [
        "assay\tsamplename\ttotal_records\tselected_records\tzero_values",
        "MetaG\tB\t2\t1\t1",
        "MetaG\tA\t1\t1\t0",
        "MetaT\tRNA\t2\t1\t1",
    ]
    assert json.loads((output / "report.json").read_text(encoding="utf-8")) == report
    assert not list(output.glob("*.partial"))
    for file, path in zip(report["files"], (g, t), strict=True):
        assert file["source"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert file["validation"]["gene_sample_keys_unique"] is True
    for info in report["outputs"].values():
        assert hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest() == info["sha256"]
    assert report["script"]["sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert all(p.read_bytes() == data for p, data in originals.items())


@pytest.mark.parametrize(
    "rows",
    [
        b"1\tA\t1\n1\tA\t2\n",  # 重复键
        b"2\tA\t1\n1\tA\t2\n",  # 无序
        b"1\tA\t1\n1\tB\t2\n2\tA\t3\n",  # 样本块重入
        b"1\tA\t-1e-1000\n",
        b"1\tA\tNaN\n",
        b"1\tA\tInfinity\n",
        b"1\tA\tbad\n",
        b"0\tA\t1\n",
        b"1\t\t1\n",
        b"1\t A\t1\n",
        b"1\tA\n",
        b"",
        b"1\t" + b"A" * 20_000 + b"\t1\n",
    ],
)
def test_invalid_full_table_never_publishes(tmp_path, rows):
    prepared, g, t, output = inputs(tmp_path, t_rows=rows)
    with pytest.raises(ValueError):
        extract(prepared, g, t, output, min_free_gib=0)
    assert not (output / "report.json").exists()
    assert not list(output.glob("*.tsv.gz"))


@pytest.mark.parametrize("damage", ["crc", "truncated", "header", "concatenated"])
def test_gzip_validation_and_hash_cover_whole_compressed_source(tmp_path, damage):
    prepared, g, t, output = inputs(tmp_path)
    content = bytearray(t.read_bytes())
    if damage == "crc":
        content[-8] ^= 1
    elif damage == "truncated":
        content = content[:-5]
    elif damage == "header":
        content = gzip.compress(b"wrong\n")
    else:
        content += gzip.compress(b"4\tC\t3\n")
        t.write_bytes(content)
        report = extract(prepared, g, t, output, min_free_gib=0)
        assert report["files"][1]["total_records"] == 4
        assert report["files"][1]["source"]["sha256"] == hashlib.sha256(content).hexdigest()
        return
    t.write_bytes(content)
    with pytest.raises((ValueError, OSError, EOFError)):
        extract(prepared, g, t, output, min_free_gib=0)
    assert not (output / "report.json").exists()


@pytest.mark.parametrize("kind", ["changed_genes", "count", "incomplete", "duplicates", "cap"])
def test_preparation_provenance_is_verified_before_scan(tmp_path, kind):
    prepared, g, t, output = inputs(tmp_path)
    report_path = prepared / "report.json"
    report = json.loads(report_path.read_text())
    if kind == "changed_genes":
        (prepared / "gene_ids.tsv").write_bytes(b"geneID\n1\n4\n")
    elif kind == "count":
        report["taxonomy"]["selected_unique_geneIDs"] = 3
    elif kind == "incomplete":
        report["status"] = "failed"
    elif kind == "duplicates":
        (prepared / "gene_ids.tsv").write_bytes(b"geneID\n1\n1\n")
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        extract(prepared, g, t, output, min_free_gib=0, max_genes=1 if kind == "cap" else 10)
    assert not output.exists()


def test_original_directories_and_existing_output_are_protected(tmp_path):
    prepared, g, t, output = inputs(tmp_path)
    for bad in (g.parent / "nested", prepared / "nested", prepared.parent):
        with pytest.raises(ValueError, match="分开"):
            extract(prepared, g, t, bad, min_free_gib=0)
    output.mkdir()
    marker = output / "report.json"
    marker.write_text("previous")
    with pytest.raises(FileExistsError):
        extract(prepared, g, t, output, min_free_gib=0)
    assert marker.read_text() == "previous"


def test_sample_and_disk_caps_stop_publication(tmp_path, monkeypatch):
    prepared, g, t, output = inputs(tmp_path)
    with pytest.raises(ValueError, match="样本数量"):
        extract(prepared, g, t, output, min_free_gib=0, max_samples=1)
    with pytest.raises(ValueError, match="保留额度"):
        extract(prepared, g, t, tmp_path / "disk", min_free_gib=1e10)
    assert not (output / "report.json").exists()


def test_input_changed_between_scans_blocks_publication(tmp_path, monkeypatch):
    prepared, g, t, output = inputs(tmp_path)
    original_scan = extract.__globals__["scan"]

    def scan_and_change(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        if args[1] == "MetaT":
            state = g.stat()
            os.utime(g, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000))
        return result

    monkeypatch.setitem(extract.__globals__, "scan", scan_and_change)
    with pytest.raises(ValueError, match="发生变化"):
        extract(prepared, g, t, output, min_free_gib=0)
    assert not (output / "report.json").exists()


def test_interrupt_leaves_only_unpublished_results(tmp_path, monkeypatch):
    prepared, g, t, output = inputs(tmp_path)
    original_scan = extract.__globals__["scan"]

    def interrupt_second(*args, **kwargs):
        if args[1] == "MetaT":
            raise KeyboardInterrupt
        return original_scan(*args, **kwargs)

    monkeypatch.setitem(extract.__globals__, "scan", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        extract(prepared, g, t, output, min_free_gib=0)
    assert not (output / "report.json").exists()
    assert list(output.glob("*.partial"))


def test_cli_success_and_failure(tmp_path):
    prepared, g, t, output = inputs(tmp_path)
    command = [
        sys.executable,
        str(SCRIPT),
        "--prepared-dir",
        str(prepared),
        "--meta-g",
        str(g),
        "--meta-t",
        str(t),
        "--output-dir",
        str(output),
        "--min-free-gib",
        "0",
    ]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    success = subprocess.run(command, capture_output=True, encoding="utf-8", env=environment)
    assert success.returncode == 0, success.stderr
    assert json.loads(success.stdout)["status"] == "completed"
    failure = subprocess.run(command, capture_output=True, encoding="utf-8", env=environment)
    assert failure.returncode == 1
    assert not failure.stdout
