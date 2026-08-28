"""End-to-end flows driven by realistic, slightly sloppy intent objects."""

from tests.conftest import ORDERS_CSV, write_csv


def test_demo_scenario(backend):
    imported = backend.import_dataset(str(ORDERS_CSV), description="Orders exported from the shop")
    assert imported["status"] == "success"

    described = backend.describe_dataset("the orders I imported today")
    assert described["dataset"]["id"] == imported["dataset"]["id"]

    preview = backend.transform_dataset("the orders I imported today", {"group_by": ["region"], "metric": "revenue"})
    assert preview["status"] == "success"
    assert preview["result"]["row_count"] == 4

    created = backend.materialize_result(
        "the orders I imported today", {"group_by": ["region"], "metric": "revenue"},
        "regional_sales", description="Revenue by region",
    )
    assert created["status"] == "success"
    assert created["summary"].startswith("Created regional_sales from orders by summing amount as revenue grouped by region")
    assert created["lineage"] == ["ds_1"]

    described = backend.describe_dataset("regional_sales")
    assert [c["name"] for c in described["schema"]] == ["region", "revenue"]
    assert described["lineage"]["upstream"][0]["name"] == "orders"

    retry = backend.materialize_result(
        "the orders I imported today", {"group_by": ["region"], "metric": "revenue"},
        "regional_sales", description="Revenue by region",
    )
    assert retry["idempotent_replay"] is True
    assert backend.list_datasets()["count"] == 2

    prov = backend.get_provenance("regional_sales")
    assert prov["inputs"][0]["id"] == "ds_1"
    assert backend.integrity_report()["ok"]


def test_agent_repairs_intent_after_ambiguity(backend, tmp_path):
    write_csv(tmp_path / "a.csv", "region,amount", ["West,1"])
    write_csv(tmp_path / "b.csv", "region,amount", ["East,2"])
    backend.import_dataset(str(tmp_path / "a.csv"), name="sales_2026_08_26")
    backend.import_dataset(str(tmp_path / "b.csv"), name="sales_daily")
    first = backend.materialize_result("sales", {"group_by": ["region"], "metric": "amount"}, "out")
    assert first["status"] == "needs_resolution"
    chosen = first["candidates"][0]["id"]
    second = backend.materialize_result(chosen, {"group_by": ["region"], "metric": "amount"}, "out")
    assert second["status"] == "success", second
    assert second["lineage"] == [chosen]


def test_partial_transform_then_completion(backend):
    backend.import_dataset(str(ORDERS_CSV))
    partial = backend.transform_dataset("orders", {"type": "aggregate", "group_by": ["region"]})
    assert partial["code"] == "INVALID_TRANSFORM"
    completed = backend.transform_dataset("orders", {"type": "aggregate", "group_by": ["region"], "measures": ["revenue"]})
    assert completed["status"] == "success"
    assert backend.integrity_report()["ok"]
