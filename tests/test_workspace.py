"""import_workspace: a whole task directory becomes datasets + artifacts in one operation."""

import json
import sqlite3
from pathlib import Path

import pytest

from tests.conftest import write_csv


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    root = tmp_path / "task_x" / "context"
    (root / "csv").mkdir(parents=True)
    (root / "json").mkdir()
    (root / "db").mkdir()
    (root / "doc").mkdir()
    (root / "video").mkdir()
    write_csv(root / "csv" / "orders.csv", "order_id,region,amount", ["1,West,10.5", "2,East,20"])
    write_csv(root / "csv" / "公司概况.csv", "公司代码,公司名称", ["1,北京公司"])
    (root / "json" / "pmi.json").write_text(json.dumps({
        "table": "pmi", "records": [{"id": 1, "EndDate": "2010-01-31 00:00:00", "IndexValue": 57.4},
                                    {"id": 2, "EndDate": "2010-02-28 00:00:00", "IndexValue": 52.0}]}), encoding="utf-8")
    (root / "json" / "plain.json").write_text(json.dumps([{"k": "a", "v": 1}, {"k": "b", "v": 2}]))
    # a json file that collides with the csv stem
    (root / "json" / "orders.json").write_text(json.dumps([{"order_id": 3, "region": "North", "amount": 5}]))
    conn = sqlite3.connect(root / "db" / "sub_db.sqlite")
    conn.execute("CREATE TABLE ed_moneyauthoritybs (id INTEGER PRIMARY KEY, EndDate TEXT, TotalAssets REAL)")
    conn.executemany("INSERT INTO ed_moneyauthoritybs VALUES (?,?,?)", [(1, "2002-01-31", 4531103.0), (2, "2002-02-28", None)])
    conn.execute("CREATE TABLE orders (id INTEGER, note TEXT)")  # collides with csv/orders.csv and json/orders.json
    conn.execute("INSERT INTO orders VALUES (1, 'x')")
    conn.commit()
    conn.close()
    (root / "knowledge.md").write_text("# Knowledge\n\n### 2.1 Money (`ed_moneyauthoritybs`)\n\nCentral bank balance sheet.\n", encoding="utf-8")
    (root / "doc" / "report.md").write_text("# report\n\nsome prose\n", encoding="utf-8")
    (root / "video" / "briefing.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    (root / ".DS_Store").write_bytes(b"junk")
    return root


def test_import_workspace_creates_datasets_and_artifacts(backend, workspace_dir):
    response = backend.import_workspace(str(workspace_dir))
    assert response["status"] == "success", response
    names = sorted(d["name"] for d in response["datasets"])
    assert names == sorted(["orders_csv", "orders_json", "sub_db_orders", "公司概况", "pmi", "plain", "ed_moneyauthoritybs"])
    kinds = {a["name"]: a["kind"] for a in response["artifacts"]}
    assert kinds["knowledge.md"] == "markdown"
    assert kinds["video/briefing.mp4"] == "video"
    assert kinds["db/sub_db.sqlite"] == "sqlite"
    assert ".DS_Store" not in kinds
    assert response["failed"] == []

    pmi = backend.describe_dataset("pmi")
    assert pmi["row_count"] == 2
    assert [c["name"] for c in pmi["schema"]] == ["id", "EndDate", "IndexValue"]
    assert pmi["source"]["name"] == "json/pmi.json"

    money = backend.describe_dataset("ed_moneyauthoritybs")
    assert money["row_count"] == 2
    assert money["source"]["kind"] == "sqlite" and money["source"]["locator"] == "ed_moneyauthoritybs"
    assert backend.integrity_report()["ok"]


def test_import_workspace_names_are_order_independent_and_aliased(backend, workspace_dir):
    backend.import_workspace(str(workspace_dir))
    # the original stem/table name stays reachable as an alias, and is ambiguous across the three
    response = backend.describe_dataset("orders")
    assert response["status"] == "needs_resolution"
    assert {c["name"] for c in response["candidates"]} == {"orders_csv", "orders_json", "sub_db_orders"}
    assert backend.describe_dataset("orders csv")["dataset"]["name"] == "orders_csv"


def test_import_workspace_is_idempotent(backend, workspace_dir):
    first = backend.import_workspace(str(workspace_dir))
    second = backend.import_workspace(str(workspace_dir))
    assert second["status"] == "success"
    assert sorted(second["replayed"]) == sorted(d["name"] for d in first["datasets"])
    assert backend.list_datasets()["count"] == len(first["datasets"])
    assert backend.list_artifacts()["count"] == len(first["artifacts"])
    assert backend.integrity_report()["ok"]


def test_import_workspace_reports_failed_sources(backend, workspace_dir):
    (workspace_dir / "csv" / "broken.csv").write_bytes(b"\xff\xfe\x00broken\x00,\x00")
    (workspace_dir / "db" / "corrupt.sqlite").write_bytes(b"not a sqlite file")
    response = backend.import_workspace(str(workspace_dir))
    assert response["status"] == "partial", response
    failed = {f["source"]: f["code"] for f in response["failed"]}
    assert "db/corrupt.sqlite" in failed
    assert len(response["datasets"]) >= 6
    assert backend.integrity_report()["ok"], backend.integrity_report()


def test_import_workspace_skips_documents_when_asked(backend, workspace_dir):
    response = backend.import_workspace(str(workspace_dir), include_documents=False)
    assert all(a["kind"] in ("csv", "json", "sqlite") for a in response["artifacts"])
    assert "knowledge.md" in response["skipped"]


def test_import_workspace_missing_directory(backend, tmp_path):
    response = backend.import_workspace(str(tmp_path / "nope"))
    assert response["code"] == "NOT_FOUND"


def test_import_single_sqlite_table_needs_locator(backend, workspace_dir):
    path = workspace_dir / "db" / "sub_db.sqlite"
    ambiguous = backend.import_dataset(str(path))
    assert ambiguous["status"] == "needs_resolution"
    assert {c["name"] for c in ambiguous["candidates"]} == {"ed_moneyauthoritybs", "orders"}
    one = backend.import_dataset(str(path), table="ED_MONEYAUTHORITYBS")
    assert one["status"] == "success", one
    assert one["dataset"]["name"] == "ed_moneyauthoritybs"
    assert one["source"]["locator"] == "ed_moneyauthoritybs"
    missing = backend.import_dataset(str(path), table="nope")
    assert missing["code"] == "NOT_FOUND" and missing["candidates"] == ["ed_moneyauthoritybs", "orders"]
    assert backend.import_dataset(str(workspace_dir / "csv" / "orders.csv"), table="x")["code"] == "INVALID_INTENT"


def test_list_artifacts_links_datasets(backend, workspace_dir):
    backend.import_workspace(str(workspace_dir))
    listing = backend.list_artifacts(kind="sqlite")
    assert listing["count"] == 1
    assert sorted(listing["artifacts"][0]["datasets"]) == ["ed_moneyauthoritybs", "sub_db_orders"]
    assert backend.list_artifacts(kind="nope")["code"] == "INVALID_INTENT"
    prov = backend.get_provenance("ed_moneyauthoritybs")
    assert prov["source"]["locator"] == "ed_moneyauthoritybs"
    assert prov["produced_by"]["parent_operation_id"] is not None
