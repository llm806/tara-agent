import gzip
import runpy
from pathlib import Path

import pytest

MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "deploy/inspect_matou_occurrences.py")
)
inspect_prefix = MODULE["inspect_prefix"]
HEADER = "geneid\tsamplename\tvalue\n"


def source(tmp_path, text):
    path = tmp_path / "occurrences.gz"
    with gzip.open(path, "wt", encoding="utf-8") as output:
        output.write(text)
    return path


def test_prefix_does_not_validate_unread_rows_or_change_source(tmp_path):
    path = source(tmp_path, HEADER + "1\tA\t1e-8\ninvalid tail\n")
    before = path.read_bytes()
    report = inspect_prefix(path, 1)
    assert report["rows_read"] == 1
    assert report["reached_eof"] is False
    assert report["minimum_value_in_prefix"] == "1E-8"
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="不是三列"):
        inspect_prefix(path, 2)


def test_duplicate_keys_and_sorting_are_scoped_to_observed_rows(tmp_path):
    path = source(tmp_path, HEADER + "2\tA\t0\n1\tA\t1\n2\tA\t2\n2\tB\t3\n")
    report = inspect_prefix(path, 10)
    assert report["reached_eof"] is True
    assert report["observed_samples_first_10"] == {"A": 3, "B": 1}
    assert report["duplicate_gene_sample_rows_in_prefix"] == 1
    assert report["gene_id_decreases_within_adjacent_same_sample"] == 1
    assert report["zero_values_in_prefix"] == 1
    assert report["minimum_value_in_prefix"] == "0"
    assert report["maximum_value_in_prefix"] == "3"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1e-1000", "", "oops"])
def test_invalid_values_fail_including_negative_float_underflow(tmp_path, value):
    with pytest.raises(ValueError, match="value"):
        inspect_prefix(source(tmp_path, HEADER + f"1\tA\t{value}\n"))


@pytest.mark.parametrize("row", ["0\tA\t1", "١\tA\t1", "1\t\t1", "1\t A\t1"])
def test_invalid_identifiers_fail(tmp_path, row):
    with pytest.raises(ValueError):
        inspect_prefix(source(tmp_path, HEADER + row + "\n"))


@pytest.mark.parametrize("limit", [0, -1, 1_000_001])
def test_limits_fail_before_source_access(tmp_path, limit):
    with pytest.raises(ValueError, match="试读行数"):
        inspect_prefix(tmp_path / "missing", limit)


@pytest.mark.parametrize("text", ["wrong\theader\n", HEADER])
def test_wrong_header_and_empty_table_fail(tmp_path, text):
    with pytest.raises(ValueError):
        inspect_prefix(source(tmp_path, text))


def test_damaged_gzip_fails_when_eof_is_attempted(tmp_path):
    path = source(tmp_path, HEADER + "1\tA\t1\n")
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(EOFError):
        inspect_prefix(path, 10)


def test_oversized_line_fails(tmp_path):
    path = source(tmp_path, HEADER + "1\t" + "A" * 20_000 + "\t1\n")
    with pytest.raises(ValueError, match="长度限制"):
        inspect_prefix(path)


def test_changing_source_invalidates_report(tmp_path, monkeypatch):
    path = source(tmp_path, HEADER + "1\tA\t1\n")
    states = iter([(1, 2, 3, 4), (2, 2, 3, 4)])
    monkeypatch.setitem(inspect_prefix.__globals__, "fingerprint", lambda _: next(states))
    with pytest.raises(ValueError, match="源文件发生变化"):
        inspect_prefix(path, 10)


def test_unmeasurably_short_read_has_no_invented_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE["time"], "monotonic", lambda: 1.0)
    report = inspect_prefix(source(tmp_path, HEADER + "1\tA\t1\n"), 1)
    assert report["rows_per_second"] is None
