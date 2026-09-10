"""Failures halfway through compound operations must leave state consistent."""

import logging

import pytest

from agent_backend.core.errors import ExecutionFailedError
from agent_backend.core.logging import EventCapture, logger


def test_execution_failure_rolls_back(backend, orders):
    response = backend.materialize_result(
        "orders", {"derive": {"bad": {"cast": "region", "to": "integer"}}}, "broken"
    )
    assert response["status"] == "error"
    assert response["code"] == "EXECUTION_FAILED"
    assert backend.list_datasets()["count"] == 1
    assert "broken" not in [t for t in backend.engine.list_tables()]
    op = backend.get_operation(response.get("operation_id", "op_2"))
    assert op["operation"] == "failed"
    assert op["error"]["code"] == "EXECUTION_FAILED"
    assert op["execution_plan"] is not None  # plan was recorded before execution
    assert backend.integrity_report()["ok"], backend.integrity_report()


def test_metadata_commit_failure_drops_physical_table(backend, orders, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(backend.store, "insert_version", boom)
    response = backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "regional_sales")
    assert response["status"] == "error"
    assert response["code"] == "INTERNAL"
    monkeypatch.undo()
    assert backend.list_datasets()["count"] == 1
    assert backend.engine.list_tables() == ["ds_1_v1"]
    report = backend.integrity_report()
    assert report["ok"], report
    # the same request now succeeds (no stale idempotency record was written)
    ok = backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "regional_sales")
    assert ok["status"] == "success", ok


@pytest.mark.parametrize("failure_point", ["cast_column", "describe_table"])
def test_temporal_refinement_failure_drops_import_table(backend, tmp_path, monkeypatch, failure_point):
    source = tmp_path / "dates.parquet"
    backend.engine.conn.execute(
        "COPY (SELECT '2026-08-28'::VARCHAR AS stamp) TO ? (FORMAT PARQUET)", [str(source)])

    def fail(*args, **kwargs):
        raise ExecutionFailedError("Injected refinement failure")

    with monkeypatch.context() as failure:
        failure.setattr(backend.engine, failure_point, fail)
        response = backend.import_dataset(str(source))

    assert response["code"] == "EXECUTION_FAILED", response
    assert backend.list_datasets()["count"] == 0
    assert backend.engine.list_tables() == []
    assert backend.integrity_report()["ok"]
    failed = [op for op in backend.store.list_operations() if op.status == "failed"]
    assert failed and failed[0].error["code"] == "EXECUTION_FAILED"

    retry = backend.import_dataset(str(source))
    assert retry["status"] == "success", retry
    assert retry["schema"][0]["type"] == "date"
    assert backend.import_dataset(str(source))["idempotent_replay"]
    assert backend.integrity_report()["ok"]


def test_failed_operations_are_audited_and_logged(backend, orders):
    capture = EventCapture()
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        response = backend.transform_dataset("orders", {"select": ["nope"]})
    finally:
        logger.removeHandler(capture)
    assert response["code"] == "NOT_FOUND"
    stages = capture.stages()
    assert stages[0] == "intent.received"
    assert "operation.failed" in stages
    ops = backend.store.list_operations()
    failed = [o for o in ops if o.status == "failed"]
    assert failed and failed[0].error["code"] == "NOT_FOUND"
    audit = backend.audit.for_operation(failed[0].id)
    assert audit[0]["event"] == "operation.failed"


def test_successful_pipeline_logs_every_stage(backend, orders):
    capture = EventCapture()
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "regional_sales")
    finally:
        logger.removeHandler(capture)
    stages = capture.stages()
    for expected in ("intent.received", "resolution.completed", "ir.canonical", "validation.completed",
                     "plan.created", "execution.completed", "state.committed"):
        assert expected in stages, stages


def test_version_drift_is_detected_by_validator(backend, orders):
    """A canonical IR that references a stale version is refused."""
    import pytest

    from agent_backend.core.errors import InvalidStateError
    from agent_backend.core.ir import TransformIR

    response = backend.transform_dataset("orders", {"select": ["region"]}, explain=True)
    ir_dict = response["explain"]["canonical_ir"]
    ir_dict["source"]["version"] = 2
    with pytest.raises(InvalidStateError):
        backend.validator.validate_transform(TransformIR.model_validate(ir_dict))


def test_concurrent_operations_from_threads_never_fail_internally(backend, orders):
    """One agent may call several tools in one step; the backend shares one SQLite and one DuckDB connection."""
    from concurrent.futures import ThreadPoolExecutor

    def work(i: int) -> dict:
        if i % 3 == 0:
            return backend.describe_dataset("orders", sample_rows=5)
        if i % 3 == 1:
            return backend.transform_dataset("orders", {"filter": f"order_id > {1000 + i % 7}", "limit": 5})
        return backend.list_datasets()

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(work, range(120)))
    codes = [r.get("code") for r in responses if r["status"] != "success"]
    assert not codes, codes
    assert backend.integrity_report()["ok"] is True
