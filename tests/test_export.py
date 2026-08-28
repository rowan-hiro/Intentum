"""export_result: a managed dataset is written to a file at the system boundary."""

import csv
import stat
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

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


def test_export_root_confines_writes(tmp_path: Path, clock):
    from agent_backend import Backend
    from tests.conftest import ORDERS_CSV

    root = tmp_path / "allowed"
    backend = Backend(tmp_path / "ws", clock=clock, export_root=root)
    try:
        backend.import_dataset(str(ORDERS_CSV))
        outside = backend.export_result("orders", str(tmp_path / "escape.csv"))
        assert outside["code"] == "PERMISSION_DENIED" and outside["recoverable"] is True
        assert str(root) in outside["hint"]
        assert not (tmp_path / "escape.csv").exists()
        traversal = backend.export_result("orders", "../escape2.csv")
        assert traversal["code"] == "PERMISSION_DENIED"
        relative = backend.export_result("orders", "sub/orders.csv")
        assert relative["status"] == "success", relative
        assert Path(relative["path"]) == root / "sub" / "orders.csv"
        absolute = backend.export_result("orders", str(root / "abs.csv"))
        assert absolute["status"] == "success"
        failed = [o for o in backend.store.list_operations() if o.status == "failed"]
        assert failed and failed[0].error["code"] == "PERMISSION_DENIED"
    finally:
        backend.close()


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


# -- format specification (MADR 0005) --------------------------------------

DATASPACE_RULES = {"decimals": 4, "strip_trailing_zeros": True, "integer_min_decimals": 1}


def _balance_sheet(backend, tmp_path: Path):
    """A shape like the DataSpace balance sheet: a text date and a wide double."""
    from tests.conftest import write_csv

    path = write_csv(tmp_path / "bs.csv", "EndDate,TotalAssets,Count",
                     ["2002-01-31 00:00:00,31783696.815,7", "2002-02-28 00:00:00,4531103.0,"])
    assert backend.import_dataset(str(path))["status"] == "success"
    return path


def rendered(target: Path) -> list[str]:
    return target.read_text(encoding="utf-8").splitlines()


def test_format_spec_renders_numbers_half_up(backend, tmp_path: Path):
    _balance_sheet(backend, tmp_path)
    target = tmp_path / "out.csv"
    response = backend.export_result("bs", str(target), format_spec={**DATASPACE_RULES, "null_text": ""})
    assert response["status"] == "success", response
    # 31783696.815 is a binary double slightly below .815; rounding the shortest
    # decimal form half-up is what the task asks for.
    assert rendered(target) == ["EndDate,TotalAssets,Count",
                                "2002-01-31 00:00:00,31783696.815,7.0",
                                "2002-02-28 00:00:00,4531103.0,"]
    two = tmp_path / "two.csv"
    backend.export_result("bs", str(two), format_spec={"decimals": 2})
    assert rendered(two)[1].split(",")[1] == "31783696.82"


def test_format_spec_renders_dates_and_nulls(backend, tmp_path: Path):
    _balance_sheet(backend, tmp_path)
    target = tmp_path / "out.csv"
    response = backend.export_result("bs", str(target), format_spec={
        "null_text": "NA", "columns": {"enddate": {"timestamp_format": "%Y-%m-%d"}}})
    assert rendered(target) == ["EndDate,TotalAssets,Count",
                                "2002-01-31,31783696.815,7",
                                "2002-02-28,4531103.0,NA"]
    assert response["format_spec"]["columns"]["enddate"] == {"timestamp_format": "%Y-%m-%d"}


def test_format_spec_is_recorded_with_the_operation(backend, tmp_path: Path):
    _balance_sheet(backend, tmp_path)
    response = backend.export_result("bs", str(tmp_path / "out.csv"), format_spec=DATASPACE_RULES)
    operation = backend.get_operation(response["operation_id"])
    assert operation["canonical_ir"]["format_spec"] == DATASPACE_RULES
    assert any(s["kind"] == "FormatValues" for s in operation["execution_plan"]["steps"])
    exported = [e for e in backend.get_provenance("bs")["audit"] if e["event"] == "dataset.exported"][-1]
    assert exported["details"]["format_spec"] == DATASPACE_RULES
    assert exported["details"]["content_hash"] == response["content_hash"]


def test_format_spec_is_validated(backend, tmp_path: Path):
    _balance_sheet(backend, tmp_path)
    target = tmp_path / "out.csv"
    unknown_key = backend.export_result("bs", str(target), format_spec={"decimal_places": 2})
    assert unknown_key["code"] == "INVALID_SCHEMA" and unknown_key["field"] == "format_spec"

    unknown_column = backend.export_result("bs", str(target), format_spec={"columns": {"nope": {"decimals": 1}}})
    assert unknown_column["code"] == "INVALID_SCHEMA" and "TotalAssets" in unknown_column["candidates"]

    bad_pattern = backend.export_result("bs", str(target), format_spec={"columns": {"EndDate": {"timestamp_format": "%Q"}}})
    assert bad_pattern["code"] == "INVALID_SCHEMA" and "%Q" in bad_pattern["message"]

    out_of_range = backend.export_result("bs", str(target), format_spec={"decimals": 99})
    assert out_of_range["code"] == "INVALID_SCHEMA"

    parquet = backend.export_result("bs", str(tmp_path / "out.parquet"), format="parquet", format_spec=DATASPACE_RULES)
    assert parquet["code"] == "INVALID_INTENT" and "typed values" in parquet["message"]
    assert not target.exists()  # every refusal happened before anything was written


def test_format_spec_reproduces_the_same_bytes(backend, tmp_path: Path):
    _balance_sheet(backend, tmp_path)
    first = backend.export_result("bs", str(tmp_path / "a.csv"), format_spec=DATASPACE_RULES)
    second = backend.export_result("bs", str(tmp_path / "b.csv"), format_spec=DATASPACE_RULES)
    assert first["content_hash"] == second["content_hash"]
    plain = backend.export_result("bs", str(tmp_path / "c.csv"))
    assert plain["content_hash"] != first["content_hash"]


@pytest.mark.parametrize(("value", "expected"), [
    (1e30, "1000000000000000000000000000000.00"),
    (Decimal("123456789012345678901234567890.125"), "123456789012345678901234567890.13"),
    (Decimal("-99999999999999999999999999999.995"), "-100000000000000000000000000000.00"),
])
def test_formatted_export_handles_large_numbers(backend, tmp_path, value, expected):
    source = tmp_path / "large.parquet"
    backend.engine.conn.sql("SELECT ? AS value", params=[value]).write_parquet(str(source))
    assert backend.import_dataset(str(source))["status"] == "success"
    target = tmp_path / "output.csv"

    with localcontext() as context:
        context.prec = 6
        response = backend.export_result("large", str(target), format_spec={"decimals": 2})

    assert response["status"] == "success", response
    assert rendered(target) == ["value", expected]
    assert response["content_hash"] == backend.workspace.content_hash(target)


@pytest.mark.parametrize("overwrite", [False, True])
def test_formatted_export_failure_preserves_target_and_cleans_up(backend, orders, tmp_path, monkeypatch, overwrite):
    from agent_backend.core.export.format import ValueRenderer

    target = tmp_path / "export" / "orders.csv"
    target.parent.mkdir()
    original = b"previous complete export\n"
    if overwrite:
        target.write_bytes(original)
    render_row = ValueRenderer.render_row
    calls = 0

    def fail_after_one_row(renderer, row):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Injected export write failure")
        return render_row(renderer, row)

    with monkeypatch.context() as failure:
        failure.setattr(ValueRenderer, "render_row", fail_after_one_row)
        response = backend.export_result("orders", str(target), format_spec={"decimals": 2}, overwrite=overwrite)

    assert response["status"] == "error", response
    assert list(target.parent.iterdir()) == ([target] if overwrite else [])
    if overwrite:
        assert target.read_bytes() == original
    assert "dataset.exported" not in [event["event"] for event in backend.get_provenance("orders")["audit"]]
    retry = backend.export_result("orders", str(target), format_spec={"decimals": 2}, overwrite=overwrite)
    assert retry["status"] == "success", retry
    assert len(rendered(target)) == 13


def test_export_does_not_overwrite_target_created_during_rendering(backend, orders, tmp_path, monkeypatch):
    from agent_backend.core.export.format import ValueRenderer

    target = tmp_path / "export" / "orders.csv"
    target.parent.mkdir()
    original = b"another completed export\n"
    render_row = ValueRenderer.render_row

    def create_target_then_render(renderer, row):
        if not target.exists():
            target.write_bytes(original)
        return render_row(renderer, row)

    monkeypatch.setattr(ValueRenderer, "render_row", create_target_then_render)
    response = backend.export_result("orders", str(target), format_spec={"decimals": 2})

    assert response["code"] == "CONFLICT", response
    assert target.read_bytes() == original
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize(("fmt", "format_spec"), [
    ("csv", {"decimals": 2}),
    ("csv", None),
    ("parquet", None),
])
def test_export_overwrite_preserves_private_target_permissions(backend, orders, tmp_path, fmt, format_spec):
    target = tmp_path / f"private.{fmt}"
    target.write_bytes(b"previous private export\n")
    target.chmod(0o600)

    response = backend.export_result("orders", str(target), format=fmt, format_spec=format_spec, overwrite=True)

    assert response["status"] == "success", response
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes() != b"previous private export\n"
