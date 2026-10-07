"""核验分类匹配边界及全量 gzip 探查的失败行为。"""

import gzip
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "inspect_matou_taxonomy.py"
script_module = runpy.run_path(str(SCRIPT))
inspect_taxonomy = script_module["inspect_taxonomy"]
prepare_pfam = script_module["prepare_pfam"]
PFAM_HEADER = "\t".join(script_module["PFAM_HEADER"]) + "\n"
HEADER = "geneID\ttaxId\ttaxName\ttaxRank\ttaxLineage\n"


def write_taxonomy(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "taxonomy.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as target:
        target.write(content)
    return path


def test_exact_node_or_ancestor_matching_and_read_only_source(tmp_path):
    path = write_taxonomy(
        tmp_path,
        HEADER
        + "1\t1\tBacillariophyta\tphylum\troot;Eukaryota;\n"
        + "2\t2\tThalassiosira\tgenus\troot; bacillariophyta ;\n"
        + "3\t3\tNotBacillariophyta\tspecies\troot;\n"
        + "4\t4\tOther\tspecies\troot;NotBacillariophyta;\n"
        + "5\t1\tBacillariophyta\tphylum\troot;Bacillariophyta;\n"
        + "1\t1\tBacillariophyta\tphylum\troot;\n",
    )
    original = path.read_bytes()
    report = inspect_taxonomy(path, "Bacillariophyta")
    assert report["total_records"] == 6
    assert report["matched_records"] == 4
    assert report["taxName_matches"] == 3
    assert report["taxLineage_matches"] == 2
    assert report["validation"]["geneID_uniqueness"] == "not_checked"
    assert report["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    assert [row["geneID"] for row in report["matched_examples"]] == ["1", "2", "5", "1"]


@pytest.mark.parametrize(
    "content",
    [
        HEADER,
        "geneID\ttaxName\n1\tBacillariophyta\n",
        HEADER + "1\t2\tOther\tgenus\n",
        HEADER + "-1\t2\tOther\tgenus\troot;\n",
        HEADER + "1.5\t2\tOther\tgenus\troot;\n",
        HEADER + "０１\t2\tOther\tgenus\troot;\n",
    ],
)
def test_invalid_schema_or_gene_id_fails(tmp_path, content):
    with pytest.raises(ValueError):
        inspect_taxonomy(write_taxonomy(tmp_path, content), "Bacillariophyta")


def test_truncated_gzip_fails_instead_of_reporting_success(tmp_path):
    path = write_taxonomy(tmp_path, HEADER + "1\t2\tOther\tgenus\troot;\n")
    path.write_bytes(path.read_bytes()[:-5])
    with pytest.raises(EOFError):
        inspect_taxonomy(path, "Bacillariophyta")


def test_other_taxon_zero_matches_and_bounded_examples(tmp_path):
    path = write_taxonomy(
        tmp_path,
        HEADER + "".join(f"{i}\t1\tOther\tgenus\troot;\n" for i in range(1, 21)),
    )
    report = inspect_taxonomy(path, "Other")
    assert report["matched_records"] == 20
    assert len(report["matched_examples"]) == 10
    assert inspect_taxonomy(path, "Bacillariophyta")["matched_records"] == 0


def test_corrupted_gzip_checksum_fails(tmp_path):
    path = write_taxonomy(tmp_path, HEADER + "1\t2\tOther\tgenus\troot;\n")
    content = bytearray(path.read_bytes())
    content[-8] ^= 1
    path.write_bytes(content)
    with pytest.raises(gzip.BadGzipFile):
        inspect_taxonomy(path, "Other")


def preparation_inputs(tmp_path, taxonomy_rows=None, pfam_rows=None):
    raw = tmp_path / "raw"
    raw.mkdir()
    taxonomy = write_taxonomy(
        raw,
        HEADER
        + (
            taxonomy_rows
            if taxonomy_rows is not None
            else "1\t2836\tBacillariophyta\tphylum\troot;\n"
            "2\t2\tThalassiosira\tgenus\troot;Bacillariophyta;\n"
            "3\t3\tOther\tgenus\troot;\n"
        ),
    )
    pfam = raw / "pfam.gz"
    with gzip.open(pfam, "wt", encoding="utf-8", newline="") as target:
        target.write(
            PFAM_HEADER
            + (
                pfam_rows
                if pfam_rows is not None
                else "1\t1\t0\t300\t1\t90\tPF00001\t100\t1\t90\t40\t1e-10\t0.9\n"
                "2\t1\t5\t300\t91\t180\tPF00001\t100\t1\t90\t20\t1e-3\t0.8\n"
                "3\t3\t0\t300\t1\t90\tPF00002\t100\t1\t90\t40\t1e-10\t0.9\n"
            )
        )
    checksum = hashlib.sha256(taxonomy.read_bytes()).hexdigest()
    return taxonomy, pfam, checksum, tmp_path / "derived"


def test_preparation_preserves_domain_hits_and_unannotated_genes(tmp_path):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path)
    originals = {path: path.read_bytes() for path in (taxonomy, pfam)}
    report = prepare_pfam(taxonomy, pfam, "Bacillariophyta", output, checksum)
    assert report["taxonomy"]["selected_unique_geneIDs"] == 2
    assert report["taxonomy"]["selected_duplicate_records"] == 0
    assert report["pfam"]["total_hit_records"] == 3
    assert report["pfam"]["selected_hit_records"] == 2
    assert report["pfam"]["selected_genes_with_pfam"] == 1
    assert report["pfam"]["selected_genes_without_pfam"] == 1
    assert report["pfam"]["gene_annotation_coverage"] == 0.5
    assert (output / "gene_ids.tsv").read_text() == "geneID\n1\n2\n"
    assert (output / "pfam_hits.tsv").read_text() == (
        PFAM_HEADER + "1\t1\t0\t300\t1\t90\tPF00001\t100\t1\t90\t40\t1e-10\t0.9\n"
        "2\t1\t5\t300\t91\t180\tPF00001\t100\t1\t90\t20\t1e-3\t0.8\n"
    )
    assert json.loads((output / "report.json").read_text(encoding="utf-8")) == report
    assert report["script_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    for item in report["outputs"].values():
        assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not list(output.glob("*.partial"))


def test_duplicate_selected_gene_id_blocks_publication(tmp_path):
    taxonomy, pfam, checksum, output = preparation_inputs(
        tmp_path,
        taxonomy_rows="1\t1\tOther\tgenus\troot;\n" * 2,
    )
    with pytest.raises(ValueError, match="重复 geneID"):
        prepare_pfam(taxonomy, pfam, "Other", output, checksum)
    assert not (output / "report.json").exists()
    assert not (output / "gene_ids.tsv").exists()


@pytest.mark.parametrize(
    "rows",
    [
        "1\t1\n",
        "1\t-1\t0\t300\t1\t90\tPF00001\t100\t1\t90\t40\t1e-10\t0.9\n",
        "",
    ],
)
def test_bad_pfam_schema_or_gene_id_blocks_publication(tmp_path, rows):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path, pfam_rows=rows)
    with pytest.raises(ValueError):
        prepare_pfam(taxonomy, pfam, "Bacillariophyta", output, checksum)
    assert not (output / "report.json").exists()
    assert not (output / "pfam_hits.tsv").exists()


@pytest.mark.parametrize("kind", ["truncated", "crc"])
def test_pfam_gzip_failure_blocks_publication(tmp_path, kind):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path)
    content = bytearray(pfam.read_bytes())
    if kind == "truncated":
        content = content[:-5]
    else:
        content[-8] ^= 1
    pfam.write_bytes(content)
    with pytest.raises((EOFError, gzip.BadGzipFile)):
        prepare_pfam(taxonomy, pfam, "Bacillariophyta", output, checksum)
    assert not (output / "report.json").exists()


def test_checksum_mismatch_and_memory_limit_block_publication(tmp_path):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        prepare_pfam(taxonomy, pfam, "Bacillariophyta", output, "0" * 64)
    with pytest.raises(ValueError, match="保护上限"):
        prepare_pfam(taxonomy, pfam, "Bacillariophyta", tmp_path / "limited", checksum, 1)
    assert not (output / "report.json").exists()
    assert not (tmp_path / "limited" / "report.json").exists()


def test_raw_directory_and_existing_results_are_protected(tmp_path):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path)
    with pytest.raises(ValueError, match="原始数据目录"):
        prepare_pfam(taxonomy, pfam, "Bacillariophyta", taxonomy.parent / "output", checksum)
    assert not (taxonomy.parent / "output").exists()
    output.mkdir()
    marker = output / "report.json"
    marker.write_text("previous run", encoding="utf-8")
    with pytest.raises(FileExistsError):
        prepare_pfam(taxonomy, pfam, "Bacillariophyta", output, checksum)
    assert marker.read_text() == "previous run"


def test_no_matching_taxon_blocks_preparation(tmp_path):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path)
    with pytest.raises(ValueError, match="没有匹配基因"):
        prepare_pfam(taxonomy, pfam, "Absent", output, checksum)
    assert not (output / "report.json").exists()


def test_source_changed_during_run_blocks_publication(tmp_path, monkeypatch):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path)
    original = prepare_pfam.__globals__["source_description"]

    def describe_and_change(path, before):
        description = original(path, before)
        if path == pfam:
            state = path.stat()
            os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000))
        return description

    monkeypatch.setitem(prepare_pfam.__globals__, "source_description", describe_and_change)
    with pytest.raises(ValueError, match="源文件发生变化"):
        prepare_pfam(taxonomy, pfam, "Bacillariophyta", output, checksum)
    assert not (output / "report.json").exists()


def test_cli_preparation_success_and_failure_exit_codes(tmp_path):
    taxonomy, pfam, checksum, output = preparation_inputs(tmp_path)
    command = [
        sys.executable,
        str(SCRIPT),
        "--input",
        str(taxonomy),
        "--taxon",
        "Bacillariophyta",
        "--pfam",
        str(pfam),
        "--output-dir",
        str(output),
        "--expected-taxonomy-sha256",
        checksum,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "completed"
    failed = subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
        },
    )
    assert failed.returncode == 1
    assert not failed.stdout
