from agent_backend import Backend
from agent_backend.core.access import DenyActionsPolicy

AGG = {"group_by": ["region"], "metric": "revenue"}


def test_publish_requires_valid_state(backend):
    from tests.conftest import ORDERS_CSV

    backend.import_dataset(str(ORDERS_CSV))  # no description
    response = backend.publish_dataset("orders")
    assert response["code"] == "INVALID_STATE"
    assert "description is empty" in response["details"]["problems"]
    backend.update_metadata("orders", description="Shop orders")
    response = backend.publish_dataset("orders")
    assert response["status"] == "success", response
    assert response["dataset"]["status"] == "published"


def test_publish_refuses_when_input_deleted(backend, orders):
    backend.materialize_result("orders", AGG, "regional_sales", description="d")
    backend.delete_dataset("orders")
    response = backend.publish_dataset("regional_sales")
    assert response["code"] == "INVALID_STATE"
    assert any("deleted" in p for p in response["details"]["problems"])


def test_update_metadata_columns_and_reserved_keys(backend, orders):
    response = backend.update_metadata(
        "orders",
        columns={"Amount": {"aliases": ["gross"], "semantic_role": "measure", "description": "Order total"}},
        metadata={"owner": "sales-team"},
    )
    assert response["status"] == "success", response
    assert response["changes"]["columns"] == ["amount"]
    described = backend.describe_dataset("orders")
    assert described["dataset"]["metadata"]["owner"] == "sales-team"
    preview = backend.transform_dataset("orders", {"group_by": ["region"], "metric": "gross"})
    assert preview["status"] == "success"
    assert backend.update_metadata("orders", metadata={"content_hash": "x"})["code"] == "INVALID_INTENT"
    assert backend.update_metadata("orders", columns={"amount": {"role": "x"}})["code"] == "INVALID_INTENT"
    assert backend.update_metadata("orders")["code"] == "INVALID_INTENT"


def test_delete_and_restore(backend, orders):
    backend.materialize_result("orders", AGG, "regional_sales")
    response = backend.delete_dataset("orders", reason="cleanup")
    assert response["status"] == "success"
    assert response["restore"] == {"operation": "restore_dataset", "dataset": "ds_1"}
    assert response["affected_downstream"][0]["name"] == "regional_sales"
    assert backend.list_datasets()["count"] == 1
    assert backend.list_datasets(include_deleted=True)["count"] == 2
    assert backend.integrity_report()["ok"]

    restored = backend.restore_dataset("ds_1")
    assert restored["status"] == "success"
    assert restored["dataset"]["status"] == "active"
    assert backend.describe_dataset("orders")["status"] == "success"
    events = [e["event"] for e in backend.get_provenance("orders")["audit"]]
    assert events[-2:] == ["dataset.deleted", "dataset.restored"]


def test_restore_name_conflict(backend, orders, tmp_path):
    from tests.conftest import write_csv

    backend.delete_dataset("orders")
    other = write_csv(tmp_path / "o.csv", "a", ["1"])
    backend.import_dataset(str(other), name="orders")
    response = backend.restore_dataset("ds_1")
    assert response["code"] == "CONFLICT"
    response = backend.restore_dataset("ds_1", new_name="orders_old")
    assert response["status"] == "success" and response["dataset"]["name"] == "orders_old"


def test_provenance_answers_the_questions(backend, orders):
    created = backend.materialize_result("orders", AGG, "regional_sales")
    prov = backend.get_provenance("regional_sales")
    assert prov["produced_by"]["id"] == created["operation_id"]
    assert prov["produced_by"]["original_intent"]["transform"] == AGG
    assert prov["inputs"] == [{"id": "ds_1", "name": "orders", "version": 1, "relationship": "derived_from"}]
    assert prov["canonical_intent"]["steps"][0]["type"] == "aggregate"
    assert prov["produced_by"]["completed_at"] is not None


def test_access_control_hook(tmp_path, clock):
    backend = Backend(tmp_path / "ws", clock=clock, policy=DenyActionsPolicy({"delete_dataset"}, principals={"guest"}))
    try:
        from tests.conftest import ORDERS_CSV

        backend.import_dataset(str(ORDERS_CSV))
        denied = backend.delete_dataset("orders", principal="guest")
        assert denied["code"] == "PERMISSION_DENIED"
        assert denied["recoverable"] is False
        allowed = backend.delete_dataset("orders", principal="admin")
        assert allowed["status"] == "success"
    finally:
        backend.close()


def test_list_datasets_is_concise(backend, orders):
    listing = backend.list_datasets()
    item = listing["datasets"][0]
    assert set(item) >= {"id", "name", "description", "status", "rows", "columns", "origin"}
    assert "physical_location" not in item
