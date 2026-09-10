"""A refusal carries what was received and what is accepted at that point (MADR 0010).

The facts come from the raise site, which holds them: the fields in scope, both
operands of a comparison, the parser's vocabulary, the documents that exist.
They travel as ``details`` so that advice can be built from them rather than
from the message, and the ``field`` path names the step, never a literal
"expression".
"""

from pathlib import Path

from tests.conftest import write_csv


def test_an_import_of_a_document_names_its_kind_and_its_reader(backend, tmp_path: Path):
    doc = tmp_path / "notes.md"
    doc.write_text("# orders\n", encoding="utf-8")
    response = backend.import_dataset(str(doc))
    assert response["code"] == "INVALID_SCHEMA"
    assert response["details"] == {"kind": "markdown", "allowed_formats": ["csv", "parquet", "json", "sqlite"],
                                   "reader": "attach_metadata"}
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00")
    assert backend.import_dataset(str(video))["details"]["reader"] is None


def test_an_unknown_field_lists_the_fields_in_scope(backend, orders):
    response = backend.transform_dataset("orders", {"select": ["order_id", "shipping_cost"]})
    assert response["code"] == "NOT_FOUND"
    assert "the fields here are order_id, order_date, region" in response["message"]
    assert response["details"]["reference"] == "shipping_cost"
    assert response["details"]["available"][:3] == ["order_id", "order_date", "region"]
    assert response["field"] == "transform.steps[0] (select)"


def test_a_comparison_type_mismatch_names_the_step_and_both_operands(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "order_id = 'a1001'"})
    assert response["code"] == "TYPE_MISMATCH"
    assert response["field"] == "transform.steps[0] (filter)"
    details = response["details"]
    assert details["operator"] == "="
    assert details["left"] == {"text": "order_id", "type": "integer", "literal": False}
    assert details["right"] == {"text": "'a1001'", "type": "string", "literal": True}
    assert "region" in details["comparable_fields"] and "order_id" not in details["comparable_fields"]


def test_a_measure_without_a_field_names_the_numeric_fields(backend, orders):
    response = backend.transform_dataset("orders", {"aggregate": {"group_by": ["region"], "measures": [{"function": "max"}]}})
    assert response["code"] == "INVALID_TRANSFORM"
    assert "Numeric fields here: order_id, quantity, unit_price, amount" in response["message"]
    assert response["details"]["numeric_fields"] == ["order_id", "quantity", "unit_price", "amount"]
    assert response["details"]["allowed_without_field"] == ["count"]


def test_a_measure_written_as_an_aggregate_call_is_refused_as_an_expression(backend, orders):
    response = backend.transform_dataset("orders", {"group_by": ["region"], "metric": "max(amount) as peak"})
    assert response["status"] == "error" and response["code"] == "INVALID_TRANSFORM"
    assert response["details"]["received"] == "max(amount) as peak"
    assert response["details"]["allowed_keys"] == ["function", "field", "alias"]


def test_a_duplicate_output_field_names_the_entries_and_the_lenient_match(backend, orders):
    # "amounts" fuzzy-resolves to "amount", which is then named a second time.
    response = backend.transform_dataset("orders", {"select": ["amounts", "amount"]})
    assert response["code"] == "INVALID_TRANSFORM"
    assert response["message"] == ("Duplicate output field 'amount': 'amounts' resolved to 'amount' (fuzzy name match) "
                                   "and 'amount' names it too.")
    details = response["details"]
    assert details["entries"] == ["amounts", "amount"]
    assert details["resolution"][0]["reference"] == "amounts" and details["resolution"][0]["resolved_to"] == "amount"
    assert "region" in details["available"]


def test_an_invalid_derive_entry_names_the_accepted_keys(backend, orders):
    body = {"group_by": ["region"], "measures": [{"function": "sum", "field": "amount"}]}
    response = backend.transform_dataset("orders", [{"derive": [body]}])
    assert response["code"] == "INVALID_TRANSFORM"
    assert response["details"]["received"] == body
    assert response["details"]["entry_keys"] == ["group_by", "measures"]
    assert response["details"]["allowed_keys"][0] == "name"
    as_key = backend.transform_dataset("orders", {"aggregate": {"group_by": [body], "measures": [{"function": "count"}]}})
    assert as_key["code"] == "INVALID_TRANSFORM" and as_key["message"].startswith("Invalid group_by key")


def test_a_parse_error_names_the_step_the_token_and_the_vocabulary(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "customer contains 'Corp'"})
    assert response["code"] == "INVALID_TRANSFORM"
    assert response["field"] == "transform.steps[0] (filter)"
    details = response["details"]
    assert details["token"] == "contains" and details["position"] == 9
    assert details["expression"] == "customer contains 'Corp'"
    assert "contains" in details["allowed_functions"] and "and" in details["keywords"]
    assert "=" in details["allowed_operators"]


def test_json_text_as_a_source_is_refused_as_not_a_name(backend, orders):
    response = backend.transform_dataset('{"left": "orders", "right": "orders", "on": "order_id = order_id"}', {"limit": 1})
    assert response["status"] == "error" and response["code"] == "INVALID_INTENT"
    assert response["details"]["received"]["left"] == "orders"


def test_an_unknown_document_lists_the_documents(backend, tmp_path: Path):
    root = tmp_path / "ws"
    (root / "doc").mkdir(parents=True)
    write_csv(root / "orders.csv", "order_id,amount", ["1,10"])
    (root / "doc" / "microlab.md").write_text("# orders\n", encoding="utf-8")
    assert backend.import_workspace(str(root))["status"] == "success"
    response = backend.attach_metadata("knowledge.md")
    assert response["code"] == "NOT_FOUND"
    assert "the documents registered here are doc/microlab.md" in response["message"]
    assert response["details"] == {"reference": "knowledge.md", "documents": ["doc/microlab.md"]}


def test_an_existing_export_file_is_described(backend, orders, tmp_path: Path):
    target = tmp_path / "out.csv"
    assert backend.export_result("orders", str(target))["status"] == "success"
    response = backend.export_result("orders", str(target))
    assert response["code"] == "CONFLICT" and "overwrite" in response["hint"]
    assert response["details"]["path"] == str(target)
    assert response["details"]["existing"]["size_bytes"] == target.stat().st_size
    assert response["details"]["existing"]["modified_at"].endswith("+00:00")
