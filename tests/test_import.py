from pathlib import Path

from tests.conftest import ORDERS_CSV, write_csv


def test_import_infers_schema_and_roles(backend, orders):
    assert orders["id"] == "ds_1"
    assert orders["rows"] == 12
    described = backend.describe_dataset("orders")
    roles = {c["name"]: c["role"] for c in described["schema"]}
    types = {c["name"]: c["type"] for c in described["schema"]}
    assert roles["order_id"] == "identifier"
    assert roles["order_date"] == "time"
    assert roles["region"] == "dimension"
    assert roles["amount"] == "measure"
    assert types["amount"] == "float"
    assert types["quantity"] == "integer"
    assert described["row_count"] == 12
    assert described["hints"]["measures"] == ["quantity", "unit_price", "amount"]
    assert described["sample"]["columns"][0] == "order_id"
    assert len(described["sample"]["rows"]) == 5


def test_import_normalizes_name_and_records_provenance(backend):
    response = backend.import_dataset(str(ORDERS_CSV), name="Shop Orders 2026", description="x")
    assert response["dataset"]["name"] == "shop_orders_2026"
    prov = backend.get_provenance("shop_orders_2026")
    assert prov["produced_by"]["kind"] == "import_dataset"
    assert prov["inputs"] == []
    events = [e["event"] for e in prov["audit"]]
    assert events == ["dataset.created", "version.created"]


def test_import_missing_file_is_structured(backend):
    response = backend.import_dataset("/nonexistent/file.csv")
    assert response["status"] == "error"
    assert response["code"] == "NOT_FOUND"
    assert response["recoverable"] is True


def test_import_unsupported_format(backend, tmp_path: Path):
    path = tmp_path / "data.yaml"
    path.write_text("a: 1")
    response = backend.import_dataset(str(path))
    assert response["code"] == "INVALID_SCHEMA"
    assert "csv" in response["message"]


def test_import_same_file_twice_replays(backend, orders):
    again = backend.import_dataset(str(ORDERS_CSV), description="Orders imported from the shop export")
    assert again["status"] == "success"
    assert again["idempotent_replay"] is True
    assert again["dataset"]["id"] == orders["id"]
    assert backend.list_datasets()["count"] == 1


def test_import_name_conflict_with_different_content(backend, orders, tmp_path: Path):
    other = write_csv(tmp_path / "orders.csv", "a,b", ["1,2"])
    response = backend.import_dataset(str(other))
    assert response["code"] == "CONFLICT"
    assert response["details"]["existing"]["id"] == orders["id"]
    assert "delete_dataset" in response["hint"]


def test_import_with_schema_hints(backend, tmp_path: Path):
    path = write_csv(tmp_path / "sales.csv", "region,order_amount", ["West,10", "East,20"])
    response = backend.import_dataset(
        str(path),
        schema_hints={"order_amount": {"aliases": ["revenue"], "description": "Gross order value"}},
        aliases=["sales figures"],
    )
    assert response["status"] == "success", response
    described = backend.describe_dataset("sales figures")
    amount = next(c for c in described["schema"] if c["name"] == "order_amount")
    assert amount["aliases"] == ["revenue"]
    assert amount["description"] == "Gross order value"


def test_import_rejects_unknown_hint_columns(backend, tmp_path: Path):
    path = write_csv(tmp_path / "t.csv", "a,b", ["1,2"])
    response = backend.import_dataset(str(path), schema_hints={"zzz": {"aliases": ["x"]}})
    assert response["code"] == "INVALID_SCHEMA"
    assert response["candidates"] == ["a", "b"]
    assert backend.list_datasets()["count"] == 0
    assert backend.integrity_report()["ok"]


def test_import_parquet(backend, tmp_path: Path):
    import duckdb

    target = tmp_path / "events.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS id, 'a' AS kind, 2.5 AS score) TO '{target}' (FORMAT PARQUET)"
    )
    response = backend.import_dataset(str(target))
    assert response["status"] == "success", response
    assert response["dataset"]["columns"] == ["id", "kind", "score"]
