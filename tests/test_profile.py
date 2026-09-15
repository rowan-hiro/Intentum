"""describe_dataset's per-column profile: what the data holds, column by column, from one scan."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import write_csv


def facts(described, name):
    (column,) = [c for c in described["schema"] if c["name"] == name]
    return {k: column[k] for k in ("non_null", "distinct", "min", "max") if k in column}


def test_every_column_is_counted_and_numeric_and_temporal_columns_are_ranged(backend, orders):
    described = backend.describe_dataset("orders", sample_rows=0)
    assert described["row_count"] == 12
    assert facts(described, "region") == {"non_null": 12, "distinct": 4}  # repetition shows against row_count
    assert facts(described, "order_id") == {"non_null": 12, "distinct": 12, "min": 1001, "max": 1012}
    assert facts(described, "amount") == {"non_null": 12, "distinct": 11, "min": 87.5, "max": 900.0}
    assert facts(described, "order_date") == {"non_null": 12, "distinct": 1, "min": "2026-08-26", "max": "2026-08-26"}
    assert "min" not in facts(described, "customer")  # strings are counted, not ranged
    assert described["profile"].startswith("non_null and distinct count every column's values")


def test_gaps_show_as_non_null_below_the_row_count(backend, tmp_path: Path):
    path = write_csv(tmp_path / "gaps.csv", "id,score,seen", ["1,10.5,2024-03-01", "2,,", "3,n/a,2024-03-05"])
    assert backend.import_dataset(str(path))["status"] == "success"
    described = backend.describe_dataset("gaps", sample_rows=0)
    assert described["row_count"] == 3
    assert facts(described, "score") == {"non_null": 2, "distinct": 2}  # the blank is null; 'n/a' is a value
    assert facts(described, "seen") == {"non_null": 2, "distinct": 2, "min": "2024-03-01", "max": "2024-03-05"}


def test_the_profile_can_be_left_out_and_is_skipped_beyond_the_column_bound(backend, orders):
    plain = backend.describe_dataset("orders", profile=False)
    assert "profile" not in plain and all("non_null" not in c for c in plain["schema"])
    backend.PROFILE_COLUMN_BOUND = 3
    wide = backend.describe_dataset("orders")
    assert wide["profile"].startswith("skipped: 8 columns, more than 3") and all("non_null" not in c for c in wide["schema"])


def test_a_range_without_finite_bounds_is_left_out_and_infinities_travel_as_text(backend, orders, tmp_path: Path):
    assert backend.materialize_result("orders", {"derive": {"x": "amount / 0"}}, "infish")["status"] == "success"
    described = backend.describe_dataset("infish", sample_rows=1)
    assert facts(described, "x") == {"non_null": 12, "distinct": 1}  # inf is no range
    assert described["sample"]["rows"][0][-1] == "inf"  # not null, and not a token JSON lacks
    path = write_csv(tmp_path / "blank.csv", "id,score", ["1,", "2,"])
    assert backend.import_dataset(str(path))["status"] == "success"
    blank = backend.describe_dataset("blank", sample_rows=0)
    assert "min" not in facts(blank, "score") and facts(blank, "score")["non_null"] == 0
