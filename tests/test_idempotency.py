AGG = {"group_by": ["region"], "metric": "revenue"}


def test_repeated_materialization_does_not_duplicate(backend, orders):
    first = backend.materialize_result("orders", AGG, "regional_sales", description="d")
    second = backend.materialize_result("orders", AGG, "regional_sales", description="d")
    assert first["status"] == "success"
    assert second["status"] == "success"
    assert second["idempotent_replay"] is True
    assert second["dataset"]["id"] == first["dataset"]["id"]
    assert second["operation_id"] == first["operation_id"]
    assert backend.list_datasets()["count"] == 2
    assert backend.integrity_report()["ok"]


def test_same_name_different_request_conflicts(backend, orders):
    backend.materialize_result("orders", AGG, "regional_sales")
    response = backend.materialize_result("orders", {"group_by": ["product"], "metric": "revenue"}, "regional_sales")
    assert response["code"] == "CONFLICT"
    assert response["details"]["existing"]["name"] == "regional_sales"


def test_explicit_idempotency_key(backend, orders):
    first = backend.materialize_result("orders", AGG, "by_region", idempotency_key="k1")
    replay = backend.materialize_result("orders", AGG, "by_region", idempotency_key="k1")
    assert replay["idempotent_replay"] and replay["dataset"]["id"] == first["dataset"]["id"]
    reused = backend.materialize_result("orders", {"group_by": ["product"], "metric": "revenue"}, "by_product", idempotency_key="k1")
    assert reused["code"] == "CONFLICT"
    assert reused["field"] == "idempotency_key"


def test_publish_and_delete_are_idempotent(backend, orders):
    backend.update_metadata("orders", description="Orders")
    assert backend.publish_dataset("orders")["status"] == "success"
    again = backend.publish_dataset("orders")
    assert again["already_published"] is True
    assert backend.delete_dataset("orders")["status"] == "success"
    again = backend.delete_dataset("orders")
    assert again["already_deleted"] is True
    assert again["restore"]["dataset"] == "ds_1"
