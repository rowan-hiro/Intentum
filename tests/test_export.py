"""export_result: a managed dataset is written to a file at the system boundary."""

import csv
from pathlib import Path

AGG = {"group_by": ["region"], "metric": "revenue", "sort": "-revenue"}


def test_export_csv(backend, orders, tmp_path: Path):
    backend.materialize_result("orders", AGG, "regional_sales")
    target = tmp_path / "out" / "prediction.csv"
    response = backend.export_result("regional sales", str(target))
    assert response["status"] == "success", response
    assert response["rows"] == 4 and response["columns"] == ["region", "revenue"]
    assert target.exists()
    with target.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["region", "revenue"]
    assert rows[1] == ["East", "1791.0"]
    assert len(rows) == 5
    events = [e["event"] for e in backend.get_provenance("regional_sales")["audit"]]
    assert events[-1] == "dataset.exported"
    assert backend.get_operation(response["operation_id"])["operation"] == "completed"


def test_export_refuses_to_overwrite_unless_asked(backend, orders, tmp_path: Path):
    target = tmp_path / "orders.csv"
    assert backend.export_result("orders", str(target))["status"] == "success"
    again = backend.export_result("orders", str(target))
    assert again["code"] == "CONFLICT" and "overwrite" in again["hint"]
    assert backend.export_result("orders", str(target), overwrite=True)["status"] == "success"


def test_export_nulls_and_formats(backend, tmp_path: Path):
    from tests.conftest import write_csv

    path = write_csv(tmp_path / "t.csv", "EndDate,TotalAssets", ["2002-01-31 00:00:00,4531103.0", "2002-02-28 00:00:00,"])
    backend.import_dataset(str(path))
    target = tmp_path / "t_out.csv"
    backend.export_result("t", str(target))
    text = target.read_text(encoding="utf-8")
    assert text.splitlines()[1:] == ["2002-01-31 00:00:00,4531103.0", "2002-02-28 00:00:00,"]
    parquet = backend.export_result("t", str(tmp_path / "t.parquet"), format="parquet")
    assert parquet["status"] == "success" and parquet["rows"] == 2
    assert backend.export_result("t", str(tmp_path / "t.xlsx"), format="xlsx")["code"] == "INVALID_SCHEMA"
    assert backend.export_result("nope", str(tmp_path / "x.csv"))["code"] == "NOT_FOUND"
