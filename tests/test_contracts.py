"""Output contracts (MADR 0007): the agent declares the shape of its deliverable
while the requirement is fresh; the backend holds every export to it.

The trust model under test (MADR 0008): the declaration is taken as given, the
export attempted later is the thing that gets checked.
"""

import csv
from pathlib import Path

from tests.conftest import write_csv

AGG = {"group_by": ["region"], "metric": "revenue", "sort": "-revenue"}


def _events(backend, entity_id: str) -> list[str]:
    return [e["event"] for e in backend.audit.for_entity(entity_id)]


def _read(target: Path) -> list[list[str]]:
    with target.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


# -- declaring ---------------------------------------------------------------

def test_declare_records_the_contract_and_its_audit_event(backend):
    response = backend.declare_output(["region", {"name": "revenue", "type": "float"}], rows={"one_per": ["region"]},
                                      description="revenue per region")
    assert response["status"] == "success", response
    contract = response["contract"]
    assert contract["id"] == "oc_1" and contract["status"] == "open" and contract["revision"] == 1
    assert contract["columns"] == [{"name": "region"}, {"name": "revenue", "type": "float"}]
    assert contract["rows"] == {"one_per": ["region"]}
    assert "exactly the columns [region, revenue (float)]" in response["summary"]
    assert _events(backend, "oc_1") == ["output_contract.declared"]
    op = backend.get_operation(response["operation_id"])
    assert op["kind"] == "declare_output" and op["canonical_ir"]["row_keys"] == ["region"]


def test_redeclaring_the_same_shape_changes_nothing(backend):
    backend.declare_output(["region", "revenue"], rows="at_least_one")
    again = backend.declare_output(["region", "revenue"], rows="at least one")
    assert again["status"] == "success" and again["unchanged"] is True
    assert again["contract"]["revision"] == 1
    assert _events(backend, "oc_1") == ["output_contract.declared"]


def test_changing_an_open_contract_needs_a_reason_and_is_recorded(backend):
    backend.declare_output(["region", "revenue"], rows="at_least_one")
    refused = backend.declare_output(["region", "revenue", "orders"], rows="at_least_one")
    assert refused["code"] == "CONFLICT" and refused["field"] == "reason"
    assert refused["details"]["contract"]["id"] == "oc_1"
    amended = backend.declare_output(["region", "revenue", "orders"], rows="at_least_one",
                                     reason="the question also asks for the order count")
    assert amended["status"] == "success" and amended["amended"] is True
    assert amended["contract"]["id"] == "oc_1" and amended["contract"]["revision"] == 2
    history = backend.get_output_contract()["history"]
    assert [e["event"] for e in history] == ["output_contract.declared", "output_contract.amended"]
    amendment = history[-1]["details"]
    assert amendment["reason"] == "the question also asks for the order count"
    assert [c["name"] for c in amendment["before"]["columns"]] == ["region", "revenue"]
    assert [c["name"] for c in amendment["after"]["columns"]] == ["region", "revenue", "orders"]


def test_declaration_shapes_are_validated(backend):
    assert backend.declare_output([])["code"] == "INVALID_INTENT"
    dup = backend.declare_output(["Region", "region"])
    assert dup["code"] == "INVALID_INTENT" and "twice" in dup["message"]
    typed = backend.declare_output([{"name": "x", "type": "money"}])
    assert typed["code"] == "INVALID_INTENT" and "float" in typed["details"]["allowed_types"]
    rows = backend.declare_output(["x"], rows="many")
    assert rows["code"] == "INVALID_INTENT" and rows["field"] == "rows"
    missing = backend.declare_output(["x"])
    assert missing["code"] == "INVALID_INTENT" and missing["field"] == "rows"
    assert missing["details"]["allowed"] == ["one", "at_least_one", {"one_per": ["column", "..."]}]
    assert backend.get_output_contract()["contract"] is None


def test_compact_declaration_with_types_and_rows_as_one(backend):
    response = backend.declare_output({"day": "date", "total": "number"}, rows=1)
    assert response["contract"]["columns"] == [{"name": "day", "type": "date"}, {"name": "total", "type": "float"}]
    assert response["contract"]["rows"] == "one"


# -- export held to the contract --------------------------------------------

def test_export_that_matches_satisfies_the_contract_with_evidence(backend, orders, tmp_path: Path):
    backend.declare_output(["region", "revenue"], rows={"one_per": ["region"]})
    backend.materialize_result("orders", AGG, "regional_sales")
    target = tmp_path / "out" / "prediction.csv"
    response = backend.export_result("regional_sales", str(target))
    assert response["status"] == "success", response
    assert response["contract"]["status"] == "satisfied" and response["contract"]["satisfied_by"] == response["operation_id"]
    assert response["contract"]["verified"] == {"contract_id": "oc_1", "revision": 1, "columns": ["region", "revenue"],
                                                "rows": 4, "cardinality": "one_per", "distinct_keys": 4}
    assert "VerifyContract(oc_1 rev 1" in response["plan"]
    assert _read(target)[0] == ["region", "revenue"]
    assert _events(backend, "oc_1") == ["output_contract.declared", "output_contract.satisfied"]
    exported = [e for e in backend.audit.for_entity(orders["id"]) if e["event"] == "dataset.exported"]
    assert not exported  # the export was of regional_sales, not orders
    exported = [e for e in backend.get_provenance("regional_sales")["audit"] if e["event"] == "dataset.exported"]
    assert exported[-1]["details"]["contract"]["contract_id"] == "oc_1"
    assert backend.get_output_contract()["contract"]["dataset_version"] == 1


def test_export_with_an_extra_column_is_refused_with_the_repair(backend, orders, tmp_path: Path):
    """The failure the second DataSpace measurement lost four runs to."""
    backend.declare_output(["region", "revenue"], rows="at_least_one")
    backend.materialize_result("orders", {"group_by": ["region"], "measures": ["revenue", "count"]}, "wide")
    target = tmp_path / "prediction.csv"
    response = backend.export_result("wide", str(target))
    assert response["code"] == "CONTRACT_MISMATCH" and response["recoverable"] is True, response
    assert not target.exists()
    assert response["details"]["problems"] == [{"kind": "extra", "message": "column 'count' is not in the contract.", "column": "count"}]
    assert response["details"]["repair"] == {"select": ["region", "revenue"]}
    assert "materialize_result" in response["hint"] and "reason" in response["hint"]
    assert response["candidates"] == ["region", "revenue", "count"]
    failed = [o for o in backend.store.list_operations() if o.status == "failed"]
    assert failed[0].error["code"] == "CONTRACT_MISMATCH"
    assert backend.get_output_contract()["contract"]["status"] == "open"
    # the repair the error carries is enough to get there
    backend.materialize_result("wide", response["details"]["repair"], "narrow")
    assert backend.export_result("narrow", str(target))["status"] == "success"


def test_a_near_miss_name_is_repaired_by_rename_then_select(backend, orders, tmp_path: Path):
    backend.declare_output(["region", "total_revenue"], rows="at_least_one")
    backend.materialize_result("orders", AGG, "regional_sales")
    response = backend.export_result("regional_sales", str(tmp_path / "p.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    assert [p["kind"] for p in response["details"]["problems"]] == ["missing", "extra"]
    assert "repair" not in response["details"]
    backend.declare_output(["Region", "revenue"], rows="at_least_one", reason="match the file the grader expects")
    response = backend.export_result("regional_sales", str(tmp_path / "p.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    assert response["details"]["problems"] == [{"kind": "renamed", "message": "declared 'Region' exists as 'region'.",
                                                "column": "Region", "actual": "region"}]
    assert response["details"]["repair"] == {"rename": {"region": "Region"}, "select": ["Region", "revenue"]}


def test_column_order_and_declared_types_are_held(backend, orders, tmp_path: Path):
    backend.declare_output(["revenue", "region"], rows="at_least_one")
    backend.materialize_result("orders", AGG, "regional_sales")
    response = backend.export_result("regional_sales", str(tmp_path / "p.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    assert [p["kind"] for p in response["details"]["problems"]] == ["order"]
    assert response["details"]["repair"] == {"select": ["revenue", "region"]}
    backend.declare_output([{"name": "region", "type": "string"}, {"name": "revenue", "type": "integer"}],
                           rows="at_least_one", reason="revenue is a float; integer is compatible by family")
    assert backend.export_result("regional_sales", str(tmp_path / "p.csv"))["status"] == "success"
    backend.declare_output([{"name": "region", "type": "date"}, "revenue"], rows="at_least_one")
    response = backend.export_result("regional_sales", str(tmp_path / "q.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    assert [p["kind"] for p in response["details"]["problems"]] == ["type"]


def test_row_cardinality_is_checked_before_publish(backend, orders, tmp_path: Path):
    backend.declare_output(["region", "revenue"], rows="one")
    backend.materialize_result("orders", AGG, "regional_sales")
    target = tmp_path / "p.csv"
    target.write_bytes(b"previous\n")
    response = backend.export_result("regional_sales", str(target), overwrite=True)
    assert response["code"] == "CONTRACT_MISMATCH"
    assert response["details"]["problems"] == [{"kind": "rows", "message": "the contract says exactly one row; the dataset has 4."}]
    assert "repair" not in response["details"]
    assert target.read_bytes() == b"previous\n"
    backend.declare_output(["region", "revenue"], rows={"one_per": ["region"]}, reason="one row per region, not one row")
    assert backend.export_result("regional_sales", str(target), overwrite=True)["status"] == "success"


def test_one_per_catches_duplicate_keys(backend, orders, tmp_path: Path):
    backend.declare_output(["region", "amount"], rows={"one_per": ["region"]})
    backend.materialize_result("orders", {"select": ["region", "amount"]}, "flat")
    response = backend.export_result("flat", str(tmp_path / "p.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    assert "over 4 distinct key(s)" in response["details"]["problems"][0]["message"]


def test_export_without_a_contract_is_unchanged(backend, orders, tmp_path: Path):
    response = backend.export_result("orders", str(tmp_path / "orders.csv"))
    assert response["status"] == "success" and "contract" not in response
    assert "VerifyContract" not in response["plan"]


def test_a_satisfied_contract_still_holds_re_exports_until_a_new_one_is_declared(backend, orders, tmp_path: Path):
    backend.declare_output(["region", "revenue"], rows="at_least_one")
    backend.materialize_result("orders", AGG, "regional_sales")
    target = tmp_path / "p.csv"
    assert backend.export_result("regional_sales", str(target))["status"] == "success"
    # re-exporting something else over the deliverable is still held to it
    response = backend.export_result("orders", str(target), overwrite=True)
    assert response["code"] == "CONTRACT_MISMATCH"
    # a new deliverable is declared freely once the previous one is satisfied
    fresh = backend.declare_output(["order_id", "amount"], rows="at_least_one")
    assert fresh["contract"]["id"] == "oc_2" and "amended" not in fresh
    assert backend.audit.for_entity("oc_2")[0]["details"]["supersedes"] == "oc_1"


def test_get_output_contract_by_id_and_unknown(backend):
    backend.declare_output(["a"], rows="at_least_one")
    assert backend.get_output_contract(contract_id="oc_1")["contract"]["columns"] == [{"name": "a"}]
    missing = backend.get_output_contract(contract_id="oc_9")
    assert missing["code"] == "NOT_FOUND" and missing["candidates"] == ["oc_1"]


def test_declared_column_names_may_be_unicode(backend, tmp_path: Path):
    path = write_csv(tmp_path / "bs.csv", "报告期,总资产", ["2002-01-31,1.5"])
    assert backend.import_dataset(str(path), name="bs")["status"] == "success"
    backend.declare_output(["报告期", "总资产"], rows="at_least_one")
    response = backend.export_result("bs", str(tmp_path / "p.csv"))
    assert response["status"] == "success", response
    assert response["contract"]["verified"]["columns"] == ["报告期", "总资产"]


# -- carried columns and organizing columns (MADR 0012) ----------------------

def test_a_sort_key_does_not_have_to_be_carried(backend, orders, tmp_path: Path):
    """The failure every DataSpace measurement lost runs to: "in treatment id order"
    names a column in an adverbial role, and the model puts it in the answer."""
    declared = backend.declare_output(["revenue"], rows="at_least_one", order_by=["region"])
    assert declared["status"] == "success", declared
    assert declared["contract"]["order_by"] == [{"column": "region", "direction": "asc"}]
    assert declared["contract"]["organizing_columns"] == ["region"]
    assert "the dataset must carry them and the file will not" in declared["summary"]
    backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "by_region")
    target = tmp_path / "p.csv"
    assert backend.export_result("by_region", str(target))["status"] == "success"
    rows = _read(target)
    assert rows[0] == ["revenue"]
    # sorted by the column the file does not carry
    ordered = backend.transform_dataset("by_region", {"sort": "region"})["result"]["rows"]
    assert [r[1] for r in ordered] == [float(r[0]) for r in rows[1:]]


def test_a_sort_key_may_be_descending_and_may_also_be_carried(backend, orders, tmp_path: Path):
    backend.declare_output(["region", "revenue"], rows={"one_per": ["region"]}, order_by=["-revenue"])
    assert backend.get_output_contract()["contract"]["order_by"] == [{"column": "revenue", "direction": "desc"}]
    assert "organizing_columns" not in backend.get_output_contract()["contract"]
    backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "by_region")
    target = tmp_path / "p.csv"
    assert backend.export_result("by_region", str(target))["status"] == "success"
    values = [float(r[1]) for r in _read(target)[1:]]
    assert values == sorted(values, reverse=True)


def test_a_grain_key_does_not_have_to_be_carried(backend, orders, tmp_path: Path):
    """"the daily maximum" is one row per day whether or not the answer carries the day."""
    backend.declare_output(["revenue"], rows={"one_per": ["region"]})
    backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "by_region")
    target = tmp_path / "p.csv"
    assert backend.export_result("by_region", str(target))["status"] == "success"
    assert _read(target)[0] == ["revenue"]
    # the grain is still checked, on the key the file does not carry
    backend.declare_output(["amount"], rows={"one_per": ["region"]}, reason="same grain, from the flat rows")
    backend.materialize_result("orders", {"select": ["region", "amount"]}, "flat")
    refused = backend.export_result("flat", str(tmp_path / "q.csv"))
    assert refused["code"] == "CONTRACT_MISMATCH"
    assert "over 4 distinct key(s)" in refused["details"]["problems"][0]["message"]


def test_an_organizing_column_missing_from_the_dataset_is_refused(backend, orders, tmp_path: Path):
    backend.declare_output(["revenue"], rows="at_least_one", order_by=["region"])
    backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "by_region")
    backend.materialize_result("by_region", {"select": ["revenue"]}, "narrow")
    refused = backend.export_result("narrow", str(tmp_path / "p.csv"))
    assert refused["code"] == "CONTRACT_MISMATCH"
    assert [p["kind"] for p in refused["details"]["problems"]] == ["missing"]
    assert "does not write it to the file" in refused["details"]["problems"][0]["message"]
    assert "repair" not in refused["details"]


def test_the_repair_keeps_the_organizing_columns(backend, orders, tmp_path: Path):
    backend.declare_output(["revenue"], rows="at_least_one", order_by=["region"])
    backend.materialize_result("orders", {"group_by": ["region", "product"], "metric": "revenue"}, "wide")
    refused = backend.export_result("wide", str(tmp_path / "p.csv"))
    assert refused["code"] == "CONTRACT_MISMATCH"
    assert refused["details"]["repair"] == {"select": ["revenue", "region"]}
    backend.materialize_result("wide", refused["details"]["repair"], "answer")
    assert backend.export_result("answer", str(tmp_path / "p.csv"))["status"] == "success"


def test_order_by_shapes_are_read_and_validated(backend):
    backend.declare_output(["a"], rows="one", order_by={"column": "b", "direction": "descending"})
    assert backend.get_output_contract()["contract"]["order_by"] == [{"column": "b", "direction": "desc"}]
    unknown = backend.declare_output(["a"], rows="one", order_by=[{"column": "b", "direction": "sideways"}])
    assert unknown["code"] == "INVALID_INTENT" and unknown["field"] == "order_by"
    twice = backend.declare_output(["a"], rows="one", order_by=["b", "B"])
    assert twice["code"] == "INVALID_INTENT" and "twice" in twice["message"]


# -- what the names are here -------------------------------------------------

def test_a_declaration_is_answered_with_what_its_names_are_here(backend, orders):
    """The backend never sees the question; it can still say what a name is in
    this workspace, and an identifier is unique per row (MADR 0010, 0012)."""
    declared = backend.declare_output(["order_id", "amount"], rows="at_least_one", order_by=["region"])
    facts = {f["name"]: f for f in declared["names"]}
    assert facts["order_id"]["carried"] is True
    match = facts["order_id"]["matches"][0]
    assert match["dataset"] == "orders" and match["type"] == "integer" and match["unique_per_row"] is True
    assert "looks like" in facts["order_id"]["note"]
    assert facts["amount"]["matches"][0]["unique_per_row"] is False and "note" not in facts["amount"]
    assert facts["region"]["carried"] is False and "note" not in facts["region"]


def test_a_name_no_dataset_carries_yet_is_named_as_such(backend, orders):
    declared = backend.declare_output(["max_revenue"], rows="one")
    (fact,) = declared["names"]
    assert fact == {"name": "max_revenue", "carried": True,
                    "note": "no dataset in this workspace has a column named 'max_revenue' yet."}
