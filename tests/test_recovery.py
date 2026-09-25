"""Teach on refusal (MADR 0010): a refusal names what the backend accepts instead and,
when the fix is mechanical, rewrites the agent's own request into calls it can send as-is.

Every rewrite below is sent back to the backend and must succeed; the advice is only
as good as the call it proposes. The shapes come from the qwen3.5-35b-a3b runs of
2026-09-02 on task_44 (agent_harness/scenarios/dataspace/README.md).
"""

import json
from pathlib import Path

from agent_backend import Backend
from tests.conftest import write_csv


def kinds(response):
    return [a["kind"] for a in response.get("advice", [])]


def advice(response, kind):
    found = [a for a in response.get("advice", []) if a["kind"] == kind]
    assert found, f"no {kind} advice in {response}"
    return found[0]


def names(response):
    return [c["name"] for c in response["result"]["columns"]]


def run(backend, rewrite):
    """Send a rewrite back, call by call, the way an agent would."""
    responses = []
    for step in rewrite:
        tool, args = step["tool"], dict(step["arguments"])
        if tool == "transform_dataset":
            response = backend.transform_dataset(args.pop("source"), args.pop("transform"), **args)
        elif tool == "materialize_result":
            response = backend.materialize_result(args.pop("source"), args.pop("transform"), args.pop("name"), **args)
        elif tool == "export_result":
            response = backend.export_result(args.pop("dataset"), args.pop("path"), **args)
        elif tool == "attach_metadata":
            response = backend.attach_metadata(args.pop("source"), **args)
        else:
            raise AssertionError(f"unexpected tool in rewrite: {tool}")
        assert response["status"] == "success", response
        responses.append(response)
    return responses


def customers(backend, tmp_path: Path):
    path = write_csv(tmp_path / "customers.csv", "name,tier", ["Acme Corp,gold", "Globex,silver", "Initech,gold"])
    assert backend.import_dataset(str(path))["status"] == "success"


# -- refusals ----------------------------------------------------------------

def test_subquery_in_a_filter_becomes_a_semi_join_with_the_right_side_filtered_first(backend, orders, tmp_path):
    customers(backend, tmp_path)
    response = backend.transform_dataset(
        "orders", {"filter": "customer in (select name from customers where tier = 'gold') and amount > 100",
                   "select": ["order_id", "customer", "amount"]},
    )
    assert response["status"] == "error" and response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "subquery_as_semi_join")
    assert "semi_join" in found["explanation"]
    materialize, transform = found["rewrite"]
    assert materialize["tool"] == "materialize_result"
    assert materialize["arguments"]["source"] == "customers" and materialize["arguments"]["transform"] == {"filter": "tier = 'gold'"}
    assert transform["tool"] == "transform_dataset"
    assert transform["arguments"]["transform"][0] == {"semi_join": {"right": "customers_filtered", "on": {"customer": "name"}}}
    assert transform["arguments"]["transform"][1] == {"filter": "amount > 100", "select": ["order_id", "customer", "amount"]}
    _, result = run(backend, found["rewrite"])
    assert names(result) == ["order_id", "customer", "amount"]
    assert {row[1] for row in result["result"]["rows"]} <= {"Acme Corp", "Initech"}
    assert all(row[2] > 100 for row in result["result"]["rows"])


def test_subquery_without_a_where_clause_needs_no_materialization(backend, orders, tmp_path):
    customers(backend, tmp_path)
    response = backend.transform_dataset("orders", [{"filter": "customer in (select name from customers)"}, {"limit": 3}])
    found = advice(response, "subquery_as_semi_join")
    (transform,) = found["rewrite"]
    assert transform["arguments"]["transform"] == [{"semi_join": {"right": "customers", "on": {"customer": "name"}}}, {"limit": 3}]
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["row_count"] == 3


def test_not_in_a_subquery_is_explained_without_a_rewrite(backend, orders, tmp_path):
    customers(backend, tmp_path)
    response = backend.transform_dataset("orders", {"filter": "customer not in (select name from customers)"})
    found = advice(response, "subquery_as_semi_join")
    assert "anti-join" in found["explanation"] and "rewrite" not in found


def test_like_becomes_contains_starts_with_or_an_equality(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "customer like '%Corp%' or product like 'Gad%'", "select": ["customer", "product"]})
    assert response["status"] == "error"
    found = advice(response, "like_as_function")
    (transform,) = found["rewrite"]
    assert transform["arguments"]["transform"]["filter"] == "contains(customer, 'Corp') or starts_with(product, 'Gad')"
    (result,) = run(backend, found["rewrite"])
    assert {row[0] for row in result["result"]["rows"]} >= {"Acme Corp", "Globex"}

    exact = backend.transform_dataset("orders", {"filter": "region like 'East'"})
    assert advice(exact, "like_as_function")["rewrite"][0]["arguments"]["transform"] == {"filter": "region = 'East'"}
    middle = backend.transform_dataset("orders", {"filter": "customer like 'A%p'"})
    assert "rewrite" not in advice(middle, "like_as_function")


def test_a_distinct_flag_becomes_a_measureless_group_by(backend, orders):
    response = backend.transform_dataset("orders", {"select": ["region"], "distinct": True, "sort": "region"})
    assert response["code"] == "INVALID_INTENT"
    found = advice(response, "distinct_as_group_by")
    assert found["rewrite"][0]["arguments"]["transform"] == {"group_by": ["region"], "sort": "region"}
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["region"]
    regions = [row[0] for row in result["result"]["rows"]]
    assert regions == sorted(set(regions))


def test_a_distinct_prefix_in_select_becomes_a_group_by(backend, orders):
    response = backend.transform_dataset("orders", [{"filter": "amount > 100"}, {"select": ["distinct region", "customer"]}])
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "distinct_as_group_by")
    assert found["rewrite"][0]["arguments"]["transform"] == [{"filter": "amount > 100"}, {"group_by": ["region", "customer"]}]
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["region", "customer"]


def test_join_on_written_as_an_equality_becomes_a_mapping(backend, orders, tmp_path):
    customers(backend, tmp_path)
    response = backend.transform_dataset("orders", {"join": {"right": "customers", "on": "orders.customer = customers.name"}, "select": ["order_id", "tier"]})
    assert response["status"] != "success"
    found = advice(response, "join_on_as_mapping")
    assert found["rewrite"][0]["arguments"]["transform"]["join"]["on"] == {"customer": "name"}
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["order_id", "tier"]

    swapped = backend.transform_dataset("orders", {"semi_join": {"right": "customers", "on": "customers.name = customer"}})
    assert advice(swapped, "join_on_as_mapping")["rewrite"][0]["arguments"]["transform"]["semi_join"]["on"] == {"customer": "name"}


def test_an_aggregate_function_in_select_becomes_an_aggregate_step(backend, orders):
    response = backend.transform_dataset("orders", {"select": ["region", "sum(amount) as revenue", "count(*) as orders"], "group_by": ["region"], "sort": "-revenue"})
    assert response["status"] == "error"
    found = advice(response, "aggregate_as_step")
    rewritten = found["rewrite"][0]["arguments"]["transform"]
    assert rewritten == {"group_by": ["region"], "sort": "-revenue",
                         "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}, {"function": "count", "alias": "orders"}]}
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["region", "revenue", "orders"]

    bare = backend.transform_dataset("orders", [{"select": ["customer", "count(distinct product) as products"]}])
    assert advice(bare, "aggregate_as_step")["rewrite"][0]["arguments"]["transform"] == [
        {"group_by": ["customer"], "measures": [{"function": "count_distinct", "field": "product", "alias": "products"}]}]


def test_an_expression_in_select_becomes_a_derive_step(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "amount > 100", "select": ["strftime('%Y-%m-%d', order_date) as day", "amount"], "sort": "-amount"})
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "expression_as_derive")
    assert found["rewrite"][0]["arguments"]["transform"] == {
        "filter": "amount > 100", "select": ["day", "amount"], "sort": "-amount", "derive": {"day": "strftime('%Y-%m-%d', order_date)"}}
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["day", "amount"] and result["result"]["rows"][0] == ["2026-08-26", 900.0]

    typed = backend.transform_dataset("orders", [{"type": "select", "select": ["date_trunc('day', order_date) as day"]}])
    assert advice(typed, "expression_as_derive")["rewrite"][0]["arguments"]["transform"] == [
        {"derive": {"day": "date_trunc('day', order_date)"}}, {"type": "select", "select": ["day"]}]
    run(backend, advice(typed, "expression_as_derive")["rewrite"])


def test_a_string_function_on_a_timestamp_is_explained(backend, orders):
    response = backend.transform_dataset("orders", {"derive": {"day": "substr(order_date, 1, 10)"}})
    assert response["code"] == "TYPE_MISMATCH"
    found = advice(response, "temporal_as_text")
    assert "strftime(field, '%Y-%m-%d')" in found["explanation"] and "format_spec" in found["explanation"] and "rewrite" not in found


def test_a_limit_tail_in_a_filter_becomes_a_limit_step(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "amount > 100 LIMIT 2", "select": ["order_id", "amount"], "sort": "-amount"})
    assert response["code"] == "INVALID_TRANSFORM" and "LIMIT" in response["message"]
    found = advice(response, "limit_tail_as_step")
    assert found["rewrite"][0]["arguments"]["transform"] == {"filter": "amount > 100", "select": ["order_id", "amount"], "sort": "-amount", "limit": 2}
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["rows"] == [[1006, 900.0], [1010, 594.0]]

    typed = backend.transform_dataset("orders", [{"type": "filter", "filter": "region = 'East' limit 1"}])
    assert advice(typed, "limit_tail_as_step")["rewrite"][0]["arguments"]["transform"] == [{"type": "filter", "filter": "region = 'East'"}, {"limit": 1}]
    (result,) = run(backend, advice(typed, "limit_tail_as_step")["rewrite"])
    assert result["result"]["row_count"] == 1

    doubled = backend.transform_dataset("orders", {"filter": "amount > 100 limit 5", "limit": 2})
    assert "rewrite" not in advice(doubled, "limit_tail_as_step")


def test_an_inline_relation_as_source_is_materialized_first(backend, orders):
    response = backend.transform_dataset(
        [{"dataset": "orders", "filter": "region = 'East'", "select": ["order_id", "customer", "amount"]},
         {"dataset": "orders", "select": ["order_id"]}],
        {"sort": "-amount", "limit": 2},
    )
    assert response["status"] == "error"
    found = advice(response, "source_as_dataset")
    assert "2 inline relations" in found["explanation"]
    materialize, transform = found["rewrite"]
    assert materialize["arguments"] == {"source": "orders", "transform": {"filter": "region = 'East'", "select": ["order_id", "customer", "amount"]},
                                        "name": "orders_subset", "description": "orders, restricted"}
    assert transform["arguments"] == {"source": "orders_subset", "transform": {"sort": "-amount", "limit": 2}}
    _, result = run(backend, found["rewrite"])
    assert result["result"]["row_count"] == 2 and names(result) == ["order_id", "customer", "amount"]


def test_an_unknown_key_is_mapped_to_the_nearest_accepted_one(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "amount > 100", "sortby": "-amount", "limit": 2})
    assert response["code"] == "INVALID_INTENT"
    found = advice(response, "unknown_key")
    assert "'sort_by'" in found["explanation"]
    assert found["rewrite"][0]["arguments"]["transform"] == {"filter": "amount > 100", "sort_by": "-amount", "limit": 2}
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["rows"][0][7] == 900.0

    far = backend.transform_dataset("orders", {"pivot": ["region"]})
    assert "rewrite" not in advice(far, "unknown_key") and "Accepted keys" in advice(far, "unknown_key")["explanation"]


def test_advice_is_recorded_with_the_failed_operation(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "region like 'E%'"})
    op = backend.get_operation(response["operation_id"]) if "operation_id" in response else None
    failed = [o for o in backend.store.list_operations(limit=5) if o.status == "failed"] if hasattr(backend.store, "list_operations") else []
    assert kinds(response) == ["like_as_function"]
    if failed:
        assert failed[0].error.get("advice")


# -- silent failures ----------------------------------------------------------

def test_an_empty_result_says_where_the_filtered_value_actually_occurs(backend, orders, tmp_path):
    path = write_csv(tmp_path / "shipments.csv", "shipment_id,order_ref,carrier", ["1,5005,DHL", "2,1001,UPS"])
    assert backend.import_dataset(str(path))["status"] == "success"
    response = backend.transform_dataset("orders", {"filter": "order_id = 5005", "select": ["order_id", "amount"]})
    assert response["status"] == "success" and response["result"]["row_count"] == 0
    found = advice(response, "value_not_found")
    assert "5005 does not occur in orders.order_id" in found["explanation"]
    assert "shipments.order_ref" in found["explanation"]
    assert "rewrite" not in found


def test_an_empty_result_whose_values_all_occur_is_reported_as_a_combination(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "region = 'South' and customer = 'Acme Corp'"})
    assert response["result"]["row_count"] == 0
    found = advice(response, "no_matching_rows")
    assert "'South' occurs in orders.region" in found["explanation"] and "'Acme Corp' occurs in orders.customer" in found["explanation"]


def lots(backend, tmp_path: Path, weights=("99999999", "100000000", "250000000", " "), name="lots"):
    """Weights read from a document arrive as text; a blank is a missing value, not a word."""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps([{"lot": chr(65 + i), "weight": w} for i, w in enumerate(weights)]), encoding="utf-8")
    assert backend.import_dataset(str(path))["status"] == "success"


def test_numbers_stored_as_text_ordered_against_a_quoted_number_are_said_to_compare_as_text(backend, tmp_path):
    lots(backend, tmp_path)
    response = backend.transform_dataset("lots", {"filter": "weight > '100000000'", "select": ["lot", "weight"]})
    assert response["status"] == "success"
    assert [row[0] for row in response["result"]["rows"]] == ["A", "C"]  # '99999999' > '100000000' as text
    found = advice(response, "numbers_compared_as_text")
    assert "lots.weight holds numbers stored as text" in found["explanation"]
    assert "1 of its values falls on the other side" in found["explanation"] and "'99999999'" in found["explanation"]
    (transform,) = found["rewrite"]
    assert transform["arguments"]["transform"] == {"filter": "try_cast(weight as double) > 100000000",
                                                   "select": ["lot", "weight"]}
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["rows"] == [["C", "250000000"]] and "advice" not in result


def test_a_quoted_number_on_the_left_and_a_comparison_in_a_derive_are_rewritten_in_place(backend, tmp_path):
    lots(backend, tmp_path)
    flipped = backend.transform_dataset("lots", [{"filter": "'100000000' < weight"}, {"select": ["lot"]}])
    rewrite = advice(flipped, "numbers_compared_as_text")["rewrite"]
    assert rewrite[0]["arguments"]["transform"] == [{"filter": "100000000 < try_cast(weight as double)"}, {"select": ["lot"]}]
    assert run(backend, rewrite)[0]["result"]["rows"] == [["C"]]

    derived = backend.materialize_result("lots", {"derive": {"heavy": "weight >= '100000000'"}}, "heavy_lots")
    assert derived["status"] == "success"
    (call,) = advice(derived, "numbers_compared_as_text")["rewrite"]
    assert call["tool"] == "materialize_result" and call["arguments"]["name"] == "heavy_lots_numeric"
    assert call["arguments"]["transform"] == {"derive": {"heavy": "try_cast(weight as double) >= 100000000"}}
    run(backend, [call])
    rows = backend.transform_dataset("heavy_lots_numeric", {"select": ["lot", "heavy"], "sort": "lot"})["result"]["rows"]
    assert rows == [["A", False], ["B", True], ["C", True], ["D", None]]


def test_text_comparisons_that_numbers_would_not_change_or_that_hold_codes_stay_silent(backend, orders, tmp_path):
    lots(backend, tmp_path)
    agreeing = backend.transform_dataset("lots", {"filter": "weight > '1'"})
    assert agreeing["result"]["row_count"] == 3 and "numbers_compared_as_text" not in kinds(agreeing)

    lots(backend, tmp_path, weights=("99", "100", "A10"), name="bins")
    coded = backend.transform_dataset("bins", {"filter": "weight > '100'"})
    assert coded["status"] == "success" and "numbers_compared_as_text" not in kinds(coded)

    numeric = backend.transform_dataset("orders", {"filter": "amount > 100"})
    assert "numbers_compared_as_text" not in kinds(numeric)


def test_a_non_empty_result_carries_no_empty_result_advice(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "region = 'East'"})
    assert response["result"]["row_count"] > 0 and "advice" not in response


def test_a_preview_with_the_declared_shape_is_told_so_and_the_export_accepted(backend, orders):
    assert backend.declare_output(["region", "revenue"], rows={"one_per": ["region"]})["status"] == "success"
    preview = backend.transform_dataset("orders", {"group_by": ["region"], "metric": "revenue", "sort": "-revenue"})
    assert preview["status"] == "success"
    found = advice(preview, "matches_contract")
    assert "oc_1" in found["explanation"]
    assert found["rewrite"] == [{"tool": "materialize_result", "arguments": {
        "source": "orders", "transform": {"group_by": ["region"], "metric": "revenue", "sort": "-revenue"},
        "name": "answer_oc_1", "description": "the declared deliverable"}}]
    (materialized,) = run(backend, found["rewrite"])
    told = advice(materialized, "matches_contract")
    assert "export_result(dataset='answer_oc_1'" in told["explanation"] and "rewrite" not in told
    exported = backend.export_result("answer_oc_1", str(Path(backend.workspace.root) / "answer.csv"))
    assert exported["status"] == "success" and exported["contract"]["status"] == "satisfied"
    later = backend.transform_dataset("orders", {"group_by": ["region"], "metric": "revenue"})
    assert "advice" not in later


def test_a_preview_one_reshape_away_from_the_contract_gets_the_reshape(backend, orders):
    backend.declare_output(["region", "revenue"], rows="at_least_one")
    preview = backend.transform_dataset("orders", {"group_by": ["region", "product"], "metric": "revenue"})
    found = advice(preview, "near_contract")
    assert "'product' is not in the contract" in found["explanation"]
    assert found["rewrite"][0]["arguments"]["transform"] == [{"group_by": ["region", "product"], "metric": "revenue"}, {"select": ["region", "revenue"]}]
    (materialized,) = run(backend, found["rewrite"])
    assert materialized["dataset"]["columns"] == ["region", "revenue"]
    assert kinds(materialized) == ["matches_contract"]


def test_a_one_per_contract_is_matched_only_once_the_keys_are_counted_as_export_counts_them(backend, orders):
    backend.declare_output(["region", "product"], rows={"one_per": ["region"]})
    repeated = {"select": ["region", "product"]}  # twelve orders over four regions
    assert "matches_contract" not in kinds(backend.transform_dataset("orders", repeated))
    assert "matches_contract" not in kinds(backend.transform_dataset(
        "orders", {"raw_query": {"sql": "SELECT region, product FROM input"}}))
    materialized = backend.materialize_result("orders", repeated, "region_products")
    assert materialized["status"] == "success" and "matches_contract" not in kinds(materialized)
    refused = backend.export_result("region_products", str(Path(backend.workspace.root) / "repeated.csv"))
    assert refused["code"] == "CONTRACT_MISMATCH"

    grouped = {"group_by": ["region"], "measures": [{"function": "min", "field": "product", "alias": "product"}]}
    queried = {"raw_query": {"sql": "SELECT region, min(product) AS product FROM input GROUP BY region"}}
    assert "matches_contract" in kinds(backend.transform_dataset("orders", grouped))
    assert "matches_contract" in kinds(backend.transform_dataset("orders", queried))
    done = backend.materialize_result("orders", queried, "first_product")
    assert kinds(done) == ["matches_contract"]
    exported = backend.export_result("first_product", str(Path(backend.workspace.root) / "unique.csv"))
    assert exported["status"] == "success" and exported["contract"]["status"] == "satisfied"


def test_a_preview_missing_a_declared_column_gets_no_contract_advice(backend, orders):
    backend.declare_output(["region", "revenue"], rows="at_least_one")
    preview = backend.transform_dataset("orders", {"group_by": ["region"], "measures": [{"function": "sum", "field": "amount", "alias": "total"}]})
    assert preview["status"] == "success" and "advice" not in preview


def test_a_contract_mismatch_at_export_carries_the_reshape_and_the_export(backend, orders, tmp_path):
    backend.declare_output(["region", "revenue"], rows="at_least_one")
    wide = backend.materialize_result("orders", {"group_by": ["region", "product"], "metric": "revenue"}, "wide")
    assert wide["status"] == "success"
    target = tmp_path / "answer.csv"
    refused = backend.export_result("wide", str(target), overwrite=True)
    assert refused["code"] == "CONTRACT_MISMATCH"
    found = advice(refused, "reshape_to_contract")
    reshape, export = found["rewrite"]
    assert reshape["arguments"] == {"source": "wide", "transform": {"select": ["region", "revenue"]}, "name": "answer_oc_1",
                                    "description": "the declared deliverable"}
    assert export["arguments"] == {"dataset": "answer_oc_1", "path": str(target), "format": "csv", "overwrite": True}
    _, exported = run(backend, found["rewrite"])
    assert exported["columns"] == ["region", "revenue"] and target.exists()


def test_a_contract_mismatch_that_needs_data_explains_without_a_rewrite(backend, orders, tmp_path):
    backend.declare_output(["region", "revenue", "orders"], rows="at_least_one")
    backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "narrow")
    refused = backend.export_result("narrow", str(tmp_path / "answer.csv"))
    found = advice(refused, "contract_mismatch")
    assert "'orders' is not in the dataset" in found["explanation"] and "rewrite" not in found


def workspace(backend, tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "doc").mkdir(parents=True)
    write_csv(root / "orders.csv", "order_id,region,amount", ["1,East,10", "2,West,20"])
    (root / "doc" / "notes.md").write_text("# orders\n\nShop orders.\n", encoding="utf-8")
    assert backend.import_workspace(str(root))["status"] == "success"
    return root


def test_a_document_named_as_a_dataset_is_pointed_at_attach_metadata(backend, tmp_path):
    workspace(backend, tmp_path)
    response = backend.describe_dataset("doc/notes.md")
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "document_as_dataset")
    assert found["rewrite"] == [{"tool": "attach_metadata", "arguments": {"source": "doc/notes.md"}}]
    (attached,) = run(backend, found["rewrite"])
    assert attached["status"] == "success"
    as_source = backend.transform_dataset("doc/notes.md", {"limit": 1})
    assert kinds(as_source) == ["document_as_dataset"]
    as_file = backend.describe_dataset("shipments.csv")
    assert as_file["code"] == "NOT_FOUND" and "rewrite" not in advice(as_file, "file_as_dataset")
    assert backend.describe_dataset("orders.csv")["status"] == "success"  # a lenient name match needs no advice


def test_every_operation_is_advised_not_only_the_three_that_asked(backend, tmp_path):
    """Dispatch is the decorator's, so a refusal raised anywhere in an operation is taught."""
    workspace(backend, tmp_path)
    for response in (backend.get_provenance("doc/notes.md"), backend.delete_dataset("doc/notes.md"),
                     backend.update_metadata("doc/notes.md", description="x"), backend.publish_dataset("doc/notes.md")):
        assert response["code"] == "NOT_FOUND"
        assert advice(response, "document_as_dataset")["rewrite"] == [
            {"tool": "attach_metadata", "arguments": {"source": "doc/notes.md"}}]


def test_the_recorded_error_carries_the_same_advice_as_the_response(backend, tmp_path):
    """A refusal inside an operation is advised before the operation record is written."""
    workspace(backend, tmp_path)
    response = backend.delete_dataset("doc/notes.md")
    assert kinds(response) == ["document_as_dataset"]
    # delete_dataset resolves before it opens an operation; the transform path opens one first.
    refused = backend.materialize_result("doc/notes.md", {"limit": 1}, "copy")
    assert kinds(refused) == ["document_as_dataset"]
    failed = [op for op in backend.store.list_operations() if op.status == "failed"]
    assert failed and failed[-1].error.get("advice") == refused["advice"]


def test_a_document_is_found_with_a_leading_slash_or_by_its_file_name(backend, tmp_path):
    workspace(backend, tmp_path)
    assert backend.attach_metadata("/doc/notes.md")["status"] == "success"
    assert backend.attach_metadata("./doc/notes.md")["status"] == "success"
    assert backend.attach_metadata("notes.md")["status"] == "success"
    assert backend.attach_metadata("missing.md")["code"] == "NOT_FOUND"


def test_a_bare_field_as_a_filter_is_explained(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "amount", "sort": "-amount"})
    assert response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "predicate_not_boolean")
    assert "is not null" in found["explanation"] and "rewrite" not in found


def test_a_declaration_without_rows_is_refused_with_the_grammar(backend):
    refused = backend.declare_output(["treatmentname"])
    assert refused["code"] == "INVALID_INTENT" and refused["field"] == "rows"
    found = advice(refused, "declaration_rows")
    assert "one_per" in found["explanation"] and "rewrite" not in found
    order = backend.declare_output(["treatmentname"], rows="one", order_by=[{"column": "x", "direction": "up-ish"}])
    assert advice(order, "declaration_order")["explanation"].startswith("order_by names")


# -- refusal families of the 2026-09-09 runs: facts from the raise site, rewrites from here ----

def payments(backend, tmp_path: Path):
    path = write_csv(tmp_path / "payments.csv", "order_no,order_note,paid",
                     ["1001,card,true", "1002,wire,false", "1003,card,true"])
    assert backend.import_dataset(str(path))["status"] == "success"


def test_an_infix_function_becomes_a_call(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "customer contains 'Corp' and not product starts_with 'Gad'"})
    assert response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "function_as_infix")
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"]["transform"]["filter"] == "contains(customer, 'Corp') and not starts_with(product, 'Gad')"
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["row_count"] >= 1


def test_a_text_measure_that_is_an_aggregate_call_becomes_an_object(backend, orders):
    response = backend.transform_dataset("orders", {"group_by": ["region"], "metric": "max(amount) as peak"})
    assert response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "measure_as_object")
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"]["transform"]["metric"] == {"function": "max", "field": "amount", "alias": "peak"}
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["region", "peak"]


def test_a_measure_without_a_field_is_filled_in_or_named(backend, orders, tmp_path):
    star = backend.transform_dataset("orders", {"aggregate": {"group_by": ["region"],
                                                              "measures": [{"function": "max", "field": "*", "alias": "n"}]}})
    found = advice(star, "measure_needs_field")
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"]["transform"]["aggregate"]["measures"] == [{"function": "count", "alias": "n"}]
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["region", "n"]
    several = backend.transform_dataset("orders", {"aggregate": {"group_by": ["region"], "measures": [{"function": "max"}]}})
    told = advice(several, "measure_needs_field")
    assert "order_id, quantity, unit_price, amount" in told["explanation"] and "rewrite" not in told
    write_csv(tmp_path / "scores.csv", "name,score", ["a,1", "b,2"])
    assert backend.import_dataset(str(tmp_path / "scores.csv"))["status"] == "success"
    one = backend.transform_dataset("scores", {"aggregate": {"group_by": ["name"], "measures": [{"function": "max"}]}})
    filled = advice(one, "measure_needs_field")
    assert filled["rewrite"][0]["arguments"]["transform"]["aggregate"]["measures"] == [{"function": "max", "field": "score"}]
    run(backend, filled["rewrite"])


def test_an_aggregate_body_under_group_by_moves_to_the_aggregate_step(backend, orders):
    body = {"group_by": ["month"], "measures": [{"function": "max", "field": "amount", "alias": "peak"}]}
    response = backend.transform_dataset("orders", {"derive": {"month": "month(order_date)"}, "group_by": body})
    assert response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "aggregate_body_misplaced")
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"]["transform"] == {"derive": {"month": "month(order_date)"}, "aggregate": body}
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["month", "peak"]


def test_a_literal_of_the_wrong_type_is_retyped_only_when_that_loses_nothing(backend, orders):
    quoted = backend.transform_dataset("orders", {"filter": "order_id = '1001'"})
    assert quoted["code"] == "TYPE_MISMATCH"
    found = advice(quoted, "compare_literal_type")
    assert found["rewrite"][0]["arguments"]["transform"]["filter"] == "order_id = 1001"
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["row_count"] == 1
    bare = backend.transform_dataset("orders", {"filter": "region = 1"})
    assert advice(bare, "compare_literal_type")["rewrite"][0]["arguments"]["transform"]["filter"] == "region = '1'"
    run(backend, advice(bare, "compare_literal_type")["rewrite"])
    lossy = backend.transform_dataset("orders", {"filter": "order_id = '025-44842'"})
    told = advice(lossy, "compare_literal_type")
    assert "rewrite" not in told and "does not read as integer" in told["explanation"]
    assert "region" in told["explanation"]


def test_after_a_join_the_right_key_is_selected_from_the_left_one(backend, orders, tmp_path):
    payments(backend, tmp_path)
    transform = {"join": {"right": "payments", "on": {"order_id": "order_no"}}, "select": ["order_no", "order_note", "paid"]}
    response = backend.transform_dataset("orders", transform)
    assert response["code"] == "INVALID_TRANSFORM" and response["message"].startswith("Duplicate output field 'order_note'")
    found = advice(response, "join_scope_names")
    assert "fuzzy name match" in found["explanation"] and "equals 'order_id'" in found["explanation"]
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"]["transform"]["select"] == ["order_id as order_no", "order_note", "paid"]
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["order_no", "order_note", "paid"]
    assert kinds(response) == ["join_scope_names"]  # the general field advice does not stack on it


def refunds(backend, tmp_path: Path):
    """Order numbers kept as text, as a spreadsheet export or an extracted document often keeps ids."""
    path = tmp_path / "refunds.json"
    path.write_text(json.dumps([{"order_id": "1002", "reason": "damaged"}, {"order_id": "1004", "reason": "late"}]))
    response = backend.import_dataset(str(path))
    assert response["status"] == "success", response


def test_join_keys_of_different_types_are_cast_in_a_derive_before_the_join(backend, orders, tmp_path):
    refunds(backend, tmp_path)
    transform = {"join": {"right": "orders", "on": {"order_id": "order_id"}}, "select": ["order_id", "reason", "amount"]}
    response = backend.transform_dataset("refunds", transform)
    assert response["message"] == "Cannot join order_id (string) with order_id (integer)."
    found = advice(response, "join_key_types")
    assert "order_id is string and order_id in orders is integer" in found["explanation"]
    assert "fails the transform" in found["explanation"] and "try_cast(order_id as integer)" in found["explanation"]
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"]["transform"] == [  # the steps as the backend normalized them
        {"type": "derive", "name": "order_id_as_integer", "expression": "cast(order_id as integer)"},
        {"type": "join", "join": {"right": "orders", "on": {"order_id_as_integer": "order_id"}}},
        {"type": "select", "select": ["order_id", "reason", "amount"]},
    ]
    (result,) = run(backend, found["rewrite"])
    assert sorted(result["result"]["rows"]) == [["1002", "damaged", 297.0], ["1004", "late", 450.0]]
    assert kinds(response) == ["join_key_types"]


def test_a_semi_join_on_a_shared_name_of_different_types_gets_the_same_cast(backend, orders, tmp_path):
    refunds(backend, tmp_path)
    response = backend.transform_dataset("orders", [{"semi_join": {"right": "refunds", "on": "order_id"}},
                                                    {"select": ["order_id", "amount"]}])
    found = advice(response, "join_key_types")
    assert found["rewrite"][0]["arguments"]["transform"] == [
        {"type": "derive", "name": "order_id_as_string", "expression": "cast(order_id as string)"},
        {"type": "semi_join", "right": "refunds", "on": {"order_id_as_string": "order_id"}},
        {"type": "select", "select": ["order_id", "amount"]},
    ]
    (result,) = run(backend, found["rewrite"])
    assert sorted(result["result"]["rows"]) == [[1002, 297.0], [1004, 450.0]]


def test_the_cast_goes_to_the_refused_join_not_an_earlier_one_with_the_same_on(backend, orders, tmp_path):
    refunds(backend, tmp_path)
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps([{"order_id": "1002", "note": "call back"}, {"order_id": "1004", "note": "resend"}]))
    assert backend.import_dataset(str(notes))["status"] == "success"
    response = backend.transform_dataset("refunds", [{"join": {"right": "notes", "on": "order_id"}},
                                                     {"join": {"right": "orders", "on": "order_id"}},
                                                     {"select": ["order_id", "note", "amount"]}])
    assert response["message"] == "Cannot join order_id (string) with order_id (integer)."
    found = advice(response, "join_key_types")
    assert found["rewrite"][0]["arguments"]["transform"] == [
        {"type": "join", "right": "notes", "on": "order_id"},
        {"type": "derive", "name": "order_id_as_integer", "expression": "cast(order_id as integer)"},
        {"type": "join", "right": "orders", "on": {"order_id_as_integer": "order_id"}},
        {"type": "select", "select": ["order_id", "note", "amount"]},
    ]
    (result,) = run(backend, found["rewrite"])
    assert sorted(result["result"]["rows"]) == [["1002", "call back", 297.0], ["1004", "resend", 450.0]]


def deliveries(backend, tmp_path: Path):
    path = write_csv(tmp_path / "deliveries.csv", "order_id,carrier", ["1002,post", "1004,courier"])
    assert backend.import_dataset(str(path))["status"] == "success"


def test_a_second_join_on_the_same_text_key_reuses_the_cast_the_first_rewrite_made(backend, orders, tmp_path):
    refunds(backend, tmp_path)
    deliveries(backend, tmp_path)
    transform = [{"join": {"right": "orders", "on": "order_id"}}, {"join": {"right": "deliveries", "on": "order_id"}},
                 {"select": ["order_id", "amount", "carrier"]}]
    first = advice(backend.transform_dataset("refunds", transform), "join_key_types")
    second_response = backend.transform_dataset("refunds", first["rewrite"][0]["arguments"]["transform"])
    assert second_response["message"] == "Cannot join order_id (string) with order_id (integer)."
    second = advice(second_response, "join_key_types")
    assert "order_id_as_integer, derived earlier as cast(order_id as integer), is already integer" in second["explanation"]
    assert second["rewrite"][0]["arguments"]["transform"] == [
        {"type": "derive", "name": "order_id_as_integer", "expression": "cast(order_id as integer)"},
        {"type": "join", "right": "orders", "on": {"order_id_as_integer": "order_id"}},
        {"type": "join", "right": "deliveries", "on": {"order_id_as_integer": "order_id"}},
        {"type": "select", "select": ["order_id", "amount", "carrier"]},
    ]
    (result,) = run(backend, second["rewrite"])
    assert sorted(result["result"]["rows"]) == [["1002", 297.0, "post"], ["1004", 450.0, "courier"]]


def test_a_column_that_already_has_the_casts_name_keeps_it_and_the_cast_takes_another(backend, orders, tmp_path):
    refunds(backend, tmp_path)
    transform = [{"derive": {"name": "order_id_as_integer", "expression": "length(order_id)"}},
                 {"join": {"right": "orders", "on": "order_id"}}, {"select": ["order_id", "order_id_as_integer", "amount"]}]
    found = advice(backend.transform_dataset("refunds", transform), "join_key_types")
    assert found["rewrite"][0]["arguments"]["transform"] == [
        {"type": "derive", "name": "order_id_as_integer", "expression": "length(order_id)"},
        {"type": "derive", "name": "order_id_as_integer_2", "expression": "cast(order_id as integer)"},
        {"type": "join", "right": "orders", "on": {"order_id_as_integer_2": "order_id"}},
        {"type": "select", "select": ["order_id", "order_id_as_integer", "amount"]},
    ]
    (result,) = run(backend, found["rewrite"])
    assert sorted(result["result"]["rows"]) == [["1002", 4, 297.0], ["1004", 4, 450.0]]


def test_after_a_raw_query_in_a_compact_object_the_cast_runs_between_the_query_and_the_join(backend, orders, tmp_path):
    refunds(backend, tmp_path)
    response = backend.transform_dataset("refunds", {"raw_query": {"sql": "SELECT order_id, reason FROM input"},
                                                     "join": {"right": "orders", "on": "order_id"},
                                                     "select": ["order_id", "reason", "amount"]})
    found = advice(response, "join_key_types")
    assert found["rewrite"][0]["arguments"]["transform"] == [
        {"type": "raw_query", "raw_query": {"sql": "SELECT order_id, reason FROM input"}},
        {"type": "derive", "name": "order_id_as_integer", "expression": "cast(order_id as integer)"},
        {"type": "join", "join": {"right": "orders", "on": {"order_id_as_integer": "order_id"}}},
        {"type": "select", "select": ["order_id", "reason", "amount"]},
    ]
    (result,) = run(backend, found["rewrite"])
    assert result["used_raw_query"] is True
    assert sorted(result["result"]["rows"]) == [["1002", "damaged", 297.0], ["1004", "late", 450.0]]


def test_after_a_join_a_qualified_name_is_unqualified(backend, orders, tmp_path):
    payments(backend, tmp_path)
    steps = [{"join": {"right": "payments", "on": {"order_id": "order_no"}}},
             {"select": ["order_id", "payments.paid as paid", "orders.region"]}]
    response = backend.transform_dataset("orders", steps)
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "join_scope_names")
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"]["transform"][1]["select"] == ["order_id", "paid", "region"]
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["order_id", "paid", "region"]


def test_a_field_used_before_the_step_that_creates_it_is_merged_into_one_object(backend, orders):
    steps = [{"aggregate": {"group_by": ["month"], "measures": [{"function": "max", "field": "amount", "alias": "peak"}]}},
             {"derive": {"expression": "month(order_date) as month"}},
             {"filter": "region = 'West'"},
             {"select": ["month", "peak"]}]
    response = backend.transform_dataset("orders", steps)
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "field_created_later")
    assert "created in step 2 but used in step 1" in found["explanation"]
    (rewritten,) = found["rewrite"]
    assert list(rewritten["arguments"]["transform"]) == ["aggregate", "derive", "filter", "select"]
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["month", "peak"]
    twice = backend.transform_dataset("orders", [{"filter": "month = 8"}, {"derive": {"month": "month(order_date)"}},
                                                 {"filter": "region = 'West'"}])
    told = advice(twice, "field_created_later")
    assert "rewrite" not in told and "Reorder" in told["explanation"]


def test_an_unknown_field_is_explained_with_the_scope_and_never_rewritten(backend, orders):
    response = backend.transform_dataset("orders", {"select": ["order_id", "amounts_total"]})
    assert response["code"] == "NOT_FOUND"
    told = advice(response, "field_not_in_scope")
    assert "rewrite" not in told
    assert "the fields there are order_id, order_date, region" in told["explanation"]
    assert "'amount' look alike but are other fields" in told["explanation"]
    assert "materialize_result keeps it" in told["explanation"]


def test_a_join_written_as_the_source_becomes_a_join_step(backend, orders, tmp_path):
    payments(backend, tmp_path)
    as_text = backend.transform_dataset('{"left": "orders", "right": "payments", "on": "order_id == order_no"}',
                                        {"select": ["order_id", "paid"]})
    assert as_text["status"] == "error" and as_text["code"] == "INVALID_INTENT"
    found = advice(as_text, "source_as_dataset")
    (rewritten,) = found["rewrite"]
    assert rewritten["arguments"] == {"source": "orders", "transform": [
        {"join": {"right": "payments", "on": {"order_id": "order_no"}}}, {"select": ["order_id", "paid"]}]}
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["order_id", "paid"]
    as_dict = backend.transform_dataset({"left": "orders", "right": "payments", "on": {"order_id": "order_no"}}, {"limit": 1})
    assert advice(as_dict, "source_as_dataset")["rewrite"][0]["arguments"]["source"] == "orders"
    keyed = backend.transform_dataset('{"left": "orders", "right": "payments", "on": "order_id == order_no"}',
                                      {"select": ["order_no", "paid"]})
    found = advice(keyed, "source_as_dataset")
    assert found["rewrite"][0]["arguments"]["transform"][1]["select"] == ["order_id as order_no", "paid"]
    (result,) = run(backend, found["rewrite"])
    assert names(result) == ["order_no", "paid"]


def test_a_call_without_its_transform_is_refused_and_never_taken_for_an_empty_one(backend, orders):
    response = backend.transform_dataset("orders", None)
    assert response["status"] == "error" and response["code"] == "INVALID_INTENT"
    found = advice(response, "transform_required")
    assert "empty list" in found["explanation"] and "rewrite" not in found
    copied = backend.materialize_result("orders", None, "orders_copy")
    assert copied["code"] == "INVALID_INTENT" and advice(copied, "transform_required")
    assert backend.list_datasets()["count"] == 1  # nothing was copied under the name meant for a result
    assert backend.transform_dataset("orders", [])["result"]["row_count"] == 12


def test_a_query_written_as_the_source_becomes_a_raw_query_first_step(backend, orders, tmp_path):
    payments(backend, tmp_path)
    sql = "SELECT o.order_id, p.paid FROM o JOIN p ON o.order_id = p.order_no ORDER BY o.order_id"
    # With the raw_query key and no transform, or as bare sql/inputs text: the first input is the source and stays bound.
    keyed = backend.transform_dataset({"raw_query": {"sql": sql, "inputs": {"o": "orders", "p": "payments"}}}, None)
    assert keyed["code"] == "INVALID_INTENT"
    (rewrite,) = advice(keyed, "source_as_dataset")["rewrite"]
    assert rewrite["arguments"] == {"source": "orders", "transform": [
        {"raw_query": {"sql": sql, "inputs": {"o": "orders", "p": "payments"}}}]}
    (result,) = run(backend, [rewrite])
    assert result["used_raw_query"] and result["result"]["row_count"] == 3
    # A binding named input is the source's, so it leaves the inputs; the later steps follow the query.
    bare = backend.transform_dataset(
        '{"sql": "SELECT input.order_id, p.paid FROM input JOIN p ON input.order_id = p.order_no", '
        '"inputs": {"p": "payments", "input": "orders"}}', {"sort": "-order_id", "limit": 1})
    (rewrite,) = advice(bare, "source_as_dataset")["rewrite"]
    assert rewrite["arguments"]["source"] == "orders"
    assert rewrite["arguments"]["transform"][0]["raw_query"]["inputs"] == {"p": "payments"}
    assert rewrite["arguments"]["transform"][1:] == [{"sort": "-order_id", "limit": 1}]
    (result,) = run(backend, [rewrite])
    assert result["result"]["rows"][0][0] == 1003
    # A query that binds nothing names no dataset: explained, not rewritten.
    unbound = backend.transform_dataset({"raw_query": {"sql": "SELECT name FROM sqlite_master", "inputs": {}}}, None)
    found = advice(unbound, "source_as_dataset")
    assert "physical table name" in found["explanation"] and "rewrite" not in found


def test_a_join_as_source_names_its_left_dataset_under_input_or_from(backend, orders, tmp_path):
    payments(backend, tmp_path)
    # The transform already joins the right dataset: only the source changes.
    joined = backend.transform_dataset({"input": "orders", "right": "payments"},
                                       {"join": {"right": "payments", "on": {"order_id": "order_no"}}, "select": ["order_id", "paid"]})
    found = advice(joined, "source_as_dataset")
    assert "already joins payments" in found["explanation"]
    (rewrite,) = found["rewrite"]
    assert rewrite["arguments"] == {"source": "orders", "transform": {
        "join": {"right": "payments", "on": {"order_id": "order_no"}}, "select": ["order_id", "paid"]}}
    (result,) = run(backend, [rewrite])
    assert names(result) == ["order_id", "paid"] and result["result"]["row_count"] == 3
    # Without the join in the transform and with the keys given, the join becomes the first step.
    keyed = backend.transform_dataset({"from": "orders", "right": "payments", "on": ["order_id = order_no"], "how": "left"}, None)
    (rewrite,) = advice(keyed, "source_as_dataset")["rewrite"]
    assert rewrite["arguments"] == {"source": "orders", "transform": [
        {"join": {"right": "payments", "on": {"order_id": "order_no"}, "how": "left"}}]}
    (result,) = run(backend, [rewrite])
    assert result["result"]["row_count"] == 12
    # Without keys there is nothing to rewrite: the join step shape, with on, is explained.
    unkeyed = backend.transform_dataset({"input": "orders", "right": "payments"}, {"select": ["order_id"]})
    found = advice(unkeyed, "source_as_dataset")
    assert "needs the fields it joins on" in found["explanation"] and "rewrite" not in found
    nameless = backend.transform_dataset({"right": "payments", "on": ["order_id = order_no"]}, None)
    found = advice(nameless, "source_as_dataset")
    assert "Name the left dataset" in found["explanation"] and "rewrite" not in found


def test_a_dataset_keyed_to_its_steps_or_placeholders_keyed_to_datasets_are_split_into_source_and_transform(backend, orders, tmp_path):
    payments(backend, tmp_path)
    keyed = backend.transform_dataset({"orders": {"filter": "amount > 100", "select": ["order_id", "amount"]}}, [{"limit": 2}])
    (rewrite,) = advice(keyed, "source_as_dataset")["rewrite"]
    assert rewrite["arguments"] == {"source": "orders", "transform": [
        {"filter": "amount > 100", "select": ["order_id", "amount"]}, {"limit": 2}]}
    (result,) = run(backend, [rewrite])
    assert names(result) == ["order_id", "amount"] and result["result"]["row_count"] == 2
    placeholders = backend.materialize_result(
        {"o": "orders", "p": "payments"},
        [{"raw_query": "SELECT o.order_id, p.paid FROM o JOIN p ON o.order_id = p.order_no"}], "paid_orders")
    (rewrite,) = advice(placeholders, "source_as_dataset")["rewrite"]
    assert rewrite["tool"] == "materialize_result" and rewrite["arguments"]["source"] == "orders"
    assert rewrite["arguments"]["transform"][0]["raw_query"]["inputs"] == {"o": "orders", "p": "payments"}
    (result,) = run(backend, [rewrite])
    assert result["status"] == "success" and result["dataset"]["name"] == "paid_orders"
    # Placeholders without a query to bind them, a bare transform object, and rows: explained, not rewritten.
    for source, transform, phrase in (
        ({"o": "orders", "p": "payments"}, {"limit": 1}, "raw_query first step whose inputs"),
        ({"join": {"right": "payments", "on": {"order_id": "order_no"}}, "select": ["order_id"]}, None, "is a transform"),
        ({"config": {"code": "600859"}, "rows": [{"year": 1995, "amount": 1.5}]}, None, "Rows written inline"),
    ):
        found = advice(backend.transform_dataset(source, transform), "source_as_dataset")
        assert phrase in found["explanation"] and "rewrite" not in found, found


def test_a_source_object_the_resolver_accepts_is_not_taken_for_an_inline_body(backend, orders, tmp_path):
    """{name}, {id} and the like are references; the refusal is elsewhere and the advice for it must not be masked."""
    payments(backend, tmp_path)
    for reference in ({"name": "orders"}, {"id": orders["id"]}, {"dataset": "orders"}):
        missing = backend.transform_dataset(reference, None)
        assert [a["kind"] for a in missing["advice"]] == ["transform_required"], missing
        unknown = backend.transform_dataset(reference, {"select": ["nope"]})
        assert [a["kind"] for a in unknown["advice"]] == ["field_not_in_scope"], unknown
    # A reference key beside a join or a query still names the source of the rewrite.
    joined = backend.transform_dataset({"dataset": "orders", "right": "payments", "on": {"order_id": "order_no"}},
                                       {"select": ["order_id", "paid"]})
    (rewrite,) = advice(joined, "source_as_dataset")["rewrite"]
    assert rewrite["arguments"]["source"] == "orders" and rewrite["arguments"]["transform"][0]["join"]["right"] == "payments"
    (result,) = run(backend, [rewrite])
    assert result["result"]["row_count"] == 3
    named = backend.transform_dataset({"name": "orders", "sql": "SELECT count(*) AS n FROM input"}, None)
    (rewrite,) = advice(named, "source_as_dataset")["rewrite"]
    assert rewrite["arguments"] == {"source": "orders", "transform": [{"raw_query": {"sql": "SELECT count(*) AS n FROM input"}}]}
    (result,) = run(backend, [rewrite])
    assert result["result"]["rows"] == [[12]]
    # Step keys written beside the query stay as the compact object's other steps.
    limited = backend.transform_dataset({"sql": "SELECT order_id FROM o ORDER BY order_id", "inputs": {"o": "orders"}, "limit": 1}, None)
    (rewrite,) = advice(limited, "source_as_dataset")["rewrite"]
    assert rewrite["arguments"]["transform"] == [{"raw_query": {"sql": "SELECT order_id FROM o ORDER BY order_id", "inputs": {"o": "orders"}}, "limit": 1}]
    (result,) = run(backend, [rewrite])
    assert result["result"]["rows"] == [[1001]]


def test_an_unknown_function_close_to_an_accepted_name_is_renamed(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "lenght(customer) > 5 and customer != 'lenght(x)'",
                                                    "select": ["customer"], "limit": 2})
    assert response["code"] == "INVALID_TRANSFORM" and response["details"]["function"] == "lenght"
    found = advice(response, "unknown_function")
    assert "length is, as length(string)" in found["explanation"]
    (rewrite,) = found["rewrite"]
    assert rewrite["arguments"]["transform"] == {"filter": "length(customer) > 5 and customer != 'lenght(x)'",
                                                 "select": ["customer"], "limit": 2}
    (result,) = run(backend, [rewrite])
    assert result["result"]["rows"] == [["Acme Corp"], ["Globex"]]


def test_a_close_name_is_offered_only_when_the_call_resolves_under_it(backend, orders, tmp_path):
    """strptime is not strftime, and isnull(x, y) is not is_null(x): such calls go to raw_query instead (from review)."""
    path = write_csv(tmp_path / "events.csv", "id,seen", ["1,March 1 2024", "2,March 5 2024"])  # text, not a date
    assert backend.import_dataset(str(path))["status"] == "success"
    parsed = backend.transform_dataset("events", {"derive": {"d": "strptime(seen, '%B %d %Y')"}, "select": ["id", "d"]})
    assert "renamed" not in parsed["details"]
    (rewrite,) = advice(parsed, "unknown_function")["rewrite"]
    assert rewrite["arguments"]["transform"][0] == {"raw_query": {"sql": "SELECT *, strptime(seen, '%B %d %Y') AS \"d\" FROM input"}}
    (result,) = run(backend, [rewrite])
    assert result["result"]["rows"] == [[1, "2024-03-01T00:00:00"], [2, "2024-03-05T00:00:00"]]
    two = backend.transform_dataset("orders", {"derive": {"x": "isnull(customer, 'none')"}})
    (rewrite,) = advice(two, "unknown_function")["rewrite"]
    assert "renamed" not in two["details"] and "isnull(customer, 'none')" in rewrite["arguments"]["transform"][0]["raw_query"]["sql"]
    # An aggregate where a scalar is expected is not sent as a query over input, which could not run it.
    first = backend.transform_dataset("orders", {"derive": {"x": "first(customer)"}})
    found = advice(first, "unknown_function")
    assert "is an aggregate" in found["explanation"] and "rewrite" not in found
    two_arg = backend.transform_dataset("orders", {"derive": {"x": "datediff(order_date, order_date)"}})
    assert two_arg["code"] == "INVALID_TRANSFORM" and "date_diff('day', start, end)" in two_arg["message"]


def test_an_unknown_function_in_the_first_step_becomes_a_raw_query_and_elsewhere_is_explained(backend, orders):
    derived = backend.transform_dataset("orders", [{"derive": {"initial": "regexp_extract(customer, '^[A-Z]')"}},
                                                   {"select": ["customer", "initial"]}, {"limit": 2}])
    found = advice(derived, "unknown_function")
    (rewrite,) = found["rewrite"]
    assert rewrite["arguments"]["transform"][0] == {"raw_query": {"sql": "SELECT *, regexp_extract(customer, '^[A-Z]') AS \"initial\" FROM input"}}
    (result,) = run(backend, [rewrite])
    assert result["used_raw_query"] and result["result"]["rows"] == [["Acme Corp", "A"], ["Globex", "G"]]
    filtered = backend.transform_dataset("orders", {"filter": "regexp_matches(customer, '^A')", "select": ["customer"]})
    (rewrite,) = advice(filtered, "unknown_function")["rewrite"]
    assert rewrite["arguments"]["transform"][0] == {"raw_query": {"sql": "SELECT * FROM input WHERE regexp_matches(customer, '^A')"}}
    (result,) = run(backend, [rewrite])
    assert result["result"]["row_count"] == 3
    later = backend.transform_dataset("orders", [{"filter": "amount > 100"}, {"derive": {"initial": "regexp_extract(customer, '^[A-Z]')"}}])
    found = advice(later, "unknown_function")
    assert "raw_query first step runs any DuckDB function" in found["explanation"] and "rewrite" not in found


def test_an_aggregate_that_picks_one_value_per_group_becomes_min_and_others_are_explained(backend, orders):
    response = backend.transform_dataset("orders", {"aggregate": {"group_by": ["region"], "measures": [
        {"function": "any_value", "field": "customer", "alias": "someone"}, {"fn": "count", "alias": "n"}]}})
    assert response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "unknown_aggregate")
    assert "min picks one value per group" in found["explanation"]
    (rewrite,) = found["rewrite"]
    assert rewrite["arguments"]["transform"]["aggregate"]["measures"][0]["function"] == "min"
    (result,) = run(backend, [rewrite])
    assert names(result) == ["region", "someone", "n"] and result["result"]["row_count"] == 4
    median = backend.transform_dataset("orders", {"aggregate": {"group_by": ["region"], "measures": [{"function": "median", "field": "amount"}]}})
    found = advice(median, "unknown_aggregate")
    assert "median(x) AS name FROM input GROUP BY key" in found["explanation"] and "rewrite" not in found
    # min is offered only where it applies: not on a boolean field, and any is a boolean aggregate, not one value.
    flagged = backend.transform_dataset("orders", {"derive": {"west": "region = 'West'"}, "aggregate": {
        "group_by": ["region"], "measures": [{"function": "any", "field": "west", "alias": "hit"}]}})
    found = advice(flagged, "unknown_aggregate")
    assert "rewrite" not in found and flagged["details"]["min_applies"] is False
    boolean = backend.transform_dataset("orders", {"derive": {"west": "region = 'West'"}, "aggregate": {
        "group_by": ["region"], "measures": [{"function": "any_value", "field": "west", "alias": "hit"}]}})
    assert "rewrite" not in advice(boolean, "unknown_aggregate")


def test_a_dataset_nothing_matches_is_advised_with_the_names_there_are(backend, orders, tmp_path):
    payments(backend, tmp_path)
    response = backend.transform_dataset("sub_db", {"limit": 1})
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "dataset_not_found")
    assert "Datasets here: orders, payments" in found["explanation"] and "rewrite" not in found
    described = backend.describe_dataset("sqlite_master")
    assert "list_datasets shows them all" in advice(described, "dataset_not_found")["explanation"]
    # The right side of a join: the refused name, not the source's (from review).
    joined = backend.transform_dataset("orders", {"join": {"right": "shipments", "on": {"order_id": "id"}}})
    found = advice(joined, "dataset_not_found")
    assert found["explanation"].startswith("No dataset is named 'shipments'") and "rewrite" not in found
    empty = Backend(tmp_path / "empty")
    try:
        nothing = empty.transform_dataset("orders", {"limit": 1})
        assert "No dataset is imported yet" in advice(nothing, "dataset_not_found")["explanation"]
    finally:
        empty.close()


def test_a_path_that_does_not_exist_is_advised_with_where_it_was_looked_for(backend, tmp_path):
    (tmp_path / "data").mkdir()
    write_csv(tmp_path / "data" / "orders.csv", "id", ["1"])
    response = backend.import_workspace(str(tmp_path / "data" / "context"))
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "path_not_found")
    assert f"{tmp_path / 'data' / 'context'} does not exist" in found["explanation"]
    assert f"The nearest existing directory, {tmp_path / 'data'}, holds: orders.csv" in found["explanation"]
    assert "working directory" in found["explanation"] and "rewrite" not in found
    missing = backend.import_dataset(str(tmp_path / "data" / "db" / "x.csv"))
    assert advice(missing, "path_not_found")["explanation"].startswith(str(tmp_path / "data" / "db" / "x.csv"))


def test_a_join_without_keys_is_joined_on_the_one_shared_field(backend, orders, tmp_path):
    path = write_csv(tmp_path / "shipments.csv", "order_id,carrier", ["1001,ups", "1002,dhl"])
    assert backend.import_dataset(str(path))["status"] == "success"
    response = backend.transform_dataset("orders", {"join": {"right": "shipments", "how": "left"}, "select": ["order_id", "carrier"]})
    assert response["code"] == "INVALID_TRANSFORM" and response["details"]["shared_fields"] == ["order_id"]
    found = advice(response, "join_needs_on")
    (rewrite,) = found["rewrite"]
    assert rewrite["arguments"]["transform"]["join"] == {"right": "shipments", "how": "left", "on": "order_id"}
    (result,) = run(backend, [rewrite])
    assert result["result"]["row_count"] == 12 and names(result) == ["order_id", "carrier"]
    typed = backend.transform_dataset("orders", [{"type": "join", "right": "shipments"}])
    assert advice(typed, "join_needs_on")["rewrite"][0]["arguments"]["transform"] == [{"type": "join", "right": "shipments", "on": "order_id"}]
    payments(backend, tmp_path)  # shares no field name with orders
    none = backend.transform_dataset("orders", {"join": {"right": "payments"}})
    found = advice(none, "join_needs_on")
    assert "share no field name" in found["explanation"] and "rewrite" not in found


def test_malformed_steps_are_rewritten_when_the_repair_is_mechanical(backend, orders):
    strings = backend.transform_dataset("orders", ["filter", "amount > 100", {"aggregate": {"measures": [{"alias": "n", "function": "count"}]}}])
    assert strings["code"] == "INVALID_TRANSFORM"
    (rewrite,) = advice(strings, "step_as_strings")["rewrite"]
    assert rewrite["arguments"]["transform"] == [{"filter": "amount > 100"}, {"aggregate": {"measures": [{"alias": "n", "function": "count"}]}}]
    (result,) = run(backend, [rewrite])
    assert result["result"]["rows"] == [[10]]
    stray = backend.transform_dataset("orders", ["amount > 100"])
    assert "rewrite" not in advice(stray, "step_as_strings")
    joined = backend.transform_dataset("orders", ["join", "payments"])  # a name alone is not a join
    found = advice(joined, "step_as_strings")
    assert "A join is an object with the right dataset and the keys" in found["explanation"] and "rewrite" not in found
    same = backend.transform_dataset("orders", {"filter": "amount > 100", "where": "amount > 100", "limit": 1})
    assert same["details"]["values_equal"] is True
    (rewrite,) = advice(same, "one_key_per_step")["rewrite"]
    assert rewrite["arguments"]["transform"] == {"filter": "amount > 100", "limit": 1}
    both = backend.transform_dataset("orders", {"filter": "amount > 100", "where": "region = 'West'", "limit": 5})
    (rewrite,) = advice(both, "one_key_per_step")["rewrite"]
    assert rewrite["arguments"]["transform"] == {"limit": 5, "filter": "(amount > 100) and (region = 'West')"}
    (result,) = run(backend, [rewrite])
    assert result["result"]["row_count"] == 3
    sorts = backend.transform_dataset("orders", {"sort": "-amount", "order_by": "amount"})
    found = advice(sorts, "one_key_per_step")
    assert "Their values differ" in found["explanation"] and "rewrite" not in found
    typo = backend.transform_dataset("orders", [{"type": "filtr", "filter": "amount > 100"}, {"type": "limit", "limit": 1}])
    (rewrite,) = advice(typo, "unknown_step_type")["rewrite"]
    assert rewrite["arguments"]["transform"][0] == {"type": "filter", "filter": "amount > 100"}
    (result,) = run(backend, [rewrite])
    assert result["result"]["row_count"] == 1
    far = backend.transform_dataset("orders", [{"type": "xyzzy", "filter": "amount > 100"}])
    assert "rewrite" not in advice(far, "unknown_step_type")
    # A near type is offered only when the step's keys are ones it takes (from review).
    for step in ({"type": "set", "filter": "amount > 100"}, {"type": "query", "sql": "SELECT 1"}):
        near = backend.transform_dataset("orders", [step])
        assert "renamed" not in near["details"] and "rewrite" not in advice(near, "unknown_step_type"), near


def test_a_duplicate_output_field_is_named_once_and_a_derive_onto_a_name_is_explained(backend, orders):
    aliased = backend.transform_dataset("orders", {"group_by": ["region as region"], "measures": ["count"]})
    assert aliased["code"] == "INVALID_TRANSFORM"
    (rewrite,) = advice(aliased, "duplicate_output_field")["rewrite"]
    assert rewrite["arguments"]["transform"] == {"group_by": ["region"], "measures": ["count"]}
    (result,) = run(backend, [rewrite])
    assert result["result"]["row_count"] == 4
    twice = backend.transform_dataset("orders", {"select": ["region", "amount", "region"], "limit": 1})
    (rewrite,) = advice(twice, "duplicate_output_field")["rewrite"]
    assert rewrite["arguments"]["transform"] == {"select": ["region", "amount"], "limit": 1}
    derived = backend.transform_dataset("orders", {"derive": {"amount": "amount * 2"}})
    found = advice(derived, "duplicate_output_field")
    assert "a derive adds a field, it does not replace one" in found["explanation"] and "rewrite" not in found
    aliased = backend.transform_dataset("orders", {"select": ["amount as region", "region"]})
    found = advice(aliased, "duplicate_output_field")
    assert "by 'amount as region' and 'region'" in found["explanation"] and "rewrite" not in found


def test_a_constant_where_a_field_belongs_and_an_empty_select_are_explained(backend, orders):
    constant = backend.transform_dataset("orders", {"aggregate": {"group_by": [], "measures": [{"alias": "total", "fn": "sum", "of": 36}]}})
    assert constant["code"] == "INVALID_INTENT" and constant["details"]["received"] == 36
    found = advice(constant, "literal_as_field")
    assert "36 is a constant" in found["explanation"] and "SELECT 36 AS total FROM input LIMIT 1" in found["explanation"]
    assert '{"derive": {"total": "36"}}' in found["explanation"]
    taught = backend.transform_dataset("orders", {"derive": {"total": "36"}, "select": ["total"], "limit": 1})
    assert taught["result"]["rows"] == [[36]]  # the shape the advice teaches runs
    empty = backend.transform_dataset("orders", {"select": []})
    found = advice(empty, "select_needs_fields")
    assert "The fields here are order_id, order_date" in found["explanation"]


def test_an_amendment_without_a_reason_is_advised_with_what_differs(backend, orders):
    assert backend.declare_output(["region", "n"], rows={"one_per": ["region"]})["status"] == "success"
    response = backend.declare_output('["region", "n"]', rows="one")
    assert response["code"] == "CONFLICT"
    found = advice(response, "contract_amendment")
    assert "The row cardinality {'one_per': ['region']} would become 'one'" in found["explanation"]
    assert "send the declaration again with reason" in found["explanation"] and "rewrite" not in found
    columns = backend.declare_output(["region"], rows={"one_per": ["region"]})
    assert "Columns ['region', 'n'] would become ['region']" in advice(columns, "contract_amendment")["explanation"]


def test_an_unknown_document_is_pointed_at_the_one_that_exists(backend, tmp_path):
    root = tmp_path / "ws"
    (root / "doc").mkdir(parents=True)
    write_csv(root / "orders.csv", "order_id,amount", ["1,10"])
    (root / "doc" / "microlab.md").write_text("# orders\n\nShop orders.\n", encoding="utf-8")
    assert backend.import_workspace(str(root))["status"] == "success"
    response = backend.attach_metadata("knowledge.md")
    assert response["code"] == "NOT_FOUND"
    found = advice(response, "document_not_found")
    assert found["rewrite"] == [{"tool": "attach_metadata", "arguments": {"source": "doc/microlab.md"}}]
    run(backend, found["rewrite"])
    (root / "doc" / "patient.md").write_text("# patient\n", encoding="utf-8")
    assert backend.import_workspace(str(root))["status"] == "success"
    two = backend.attach_metadata("knowledge.md")
    told = advice(two, "document_not_found")
    assert "rewrite" not in told and "doc/microlab.md, doc/patient.md" in told["explanation"]
    close = backend.attach_metadata("patients.md")
    assert advice(close, "document_not_found")["rewrite"][0]["arguments"]["source"] == "doc/patient.md"


def test_an_export_onto_an_existing_file_is_offered_with_overwrite_and_its_cost_named(backend, orders, tmp_path):
    target = tmp_path / "answer.csv"
    assert backend.export_result("orders", str(target), format_spec={"decimals": 1})["status"] == "success"
    response = backend.export_result("orders", str(target), format_spec={"decimals": 1})
    assert response["code"] == "CONFLICT"
    found = advice(response, "file_exists")
    assert "keeps no copy of the earlier content" in found["explanation"]
    assert found["rewrite"] == [{"tool": "export_result", "arguments": {
        "dataset": "orders", "path": str(target), "format_spec": {"decimals": 1}, "overwrite": True}}]
    run(backend, found["rewrite"])
    failed = [op for op in backend.store.list_operations() if op.status == "failed"]
    assert failed and failed[-1].error.get("advice") == response["advice"]


def test_a_transform_sent_as_text_is_parsed_or_its_json_error_named(backend, orders):
    response = backend.transform_dataset("orders", '{"limit": 1}')
    assert response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "transform_as_text")
    assert found["rewrite"] == [{"tool": "transform_dataset", "arguments": {"source": "orders", "transform": {"limit": 1}}}]
    run(backend, found["rewrite"])
    broken = backend.transform_dataset("orders", '[{"filter": "amount > 1"')
    told = advice(broken, "transform_as_text")
    assert "rewrite" not in told and "not JSON" in told["explanation"]


def test_a_document_or_media_file_given_to_import_dataset_is_explained(backend, tmp_path):
    doc = tmp_path / "patient.md"
    doc.write_text("# patient\n\n| id | name |\n|---|---|\n| 1 | a |\n", encoding="utf-8")
    response = backend.import_dataset(str(doc))
    assert response["code"] == "INVALID_SCHEMA"
    found = advice(response, "document_as_dataset")
    assert "extracted outside the backend" in found["explanation"]
    assert found["rewrite"] == [{"tool": "attach_metadata", "arguments": {"source": str(doc)}}]
    run(backend, found["rewrite"])
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00")
    media = backend.import_dataset(str(clip))
    told = advice(media, "media_as_dataset")
    assert "rewrite" not in told and "does not read video" in told["explanation"]
    unknown = tmp_path / "data.xyz"
    unknown.write_text("x", encoding="utf-8")
    assert "rewrite" not in advice(backend.import_dataset(str(unknown)), "unsupported_format")


def test_a_taken_materialization_name_is_explained_with_what_is_there(backend, orders):
    assert backend.materialize_result("orders", {"limit": 1}, "first")["status"] == "success"
    response = backend.materialize_result("orders", {"limit": 2}, "first")
    assert response["code"] == "CONFLICT"
    told = advice(response, "name_taken")
    assert "rewrite" not in told and "'first' (ds_2, 1 rows)" in told["explanation"]


def test_a_derive_with_a_name_and_no_expression_is_pointed_at_the_aggregate_step(backend, orders):
    response = backend.transform_dataset("orders", [{"sort": "-order_date"}, {"derive": "max_date"},
                                                    {"filter": "order_date = max_date"}])
    assert response["code"] == "INVALID_TRANSFORM"
    assert response["details"]["received"] == {"name": "max_date", "expression": None}
    told = advice(response, "derive_without_expression")
    assert "rewrite" not in told
    assert "'max_date' names a result but gives nothing to compute" in told["explanation"]
    assert "aggregate step" in told["explanation"] and "semi_join" in told["explanation"]
    assert "reads like such a value" in told["explanation"]
    assert kinds(response) == ["derive_without_expression"]


def test_an_infix_function_with_a_call_on_the_left_becomes_a_call(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "lower(customer) contains 'corp'"})
    assert response["code"] == "INVALID_TRANSFORM"
    found = advice(response, "function_as_infix")
    assert found["rewrite"][0]["arguments"]["transform"]["filter"] == "contains(lower(customer), 'corp')"
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["row_count"] >= 1


def test_stray_characters_after_a_complete_expression_are_dropped(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "region = 'West' and amount > 100\u0ac0\u0ab2"})
    assert response["code"] == "INVALID_TRANSFORM" and response["message"].startswith("Unexpected character")
    found = advice(response, "stray_characters")
    assert "not part of any token" in found["explanation"]
    assert found["rewrite"][0]["arguments"]["transform"]["filter"] == "region = 'West' and amount > 100"
    run(backend, found["rewrite"])
    # a tail that could still be expression text is not cut
    unfinished = backend.transform_dataset("orders", {"filter": "region = 'West' and amount > 100 \u0ac0 'x'"})
    assert "stray_characters" not in kinds(unfinished)


def amounts_as_text(backend, tmp_path: Path):
    path = write_csv(tmp_path / "amounts.csv", "id,amount", ["1,10.5", "2,", "3,n/a", "4,40"])
    assert backend.import_dataset(str(path), name="amounts")["status"] == "success"


def test_backtick_names_and_extract_are_respelled(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "`amount` > 500 AND EXTRACT(DAY FROM order_date) = 26",
                                                    "select": ["order_id"]})
    found = advice(response, "sql_spelling")
    assert found["rewrite"][0]["arguments"]["transform"]["filter"] == '"amount" > 500 AND day(order_date) = 26'
    (result,) = run(backend, found["rewrite"])
    assert sorted(result["result"]["rows"]) == [[1006], [1010]]


def test_between_is_respelled_as_a_pair_of_comparisons(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "amount BETWEEN 100 AND 500 and region != 'x between y'",
                                                    "select": ["order_id"], "sort": "order_id"})
    found = advice(response, "sql_spelling")
    assert found["rewrite"][0]["arguments"]["transform"]["filter"] == (
        "(amount >= 100 and amount <= 500) and region != 'x between y'")
    (result,) = run(backend, found["rewrite"])
    assert [row[0] for row in result["result"]["rows"]] == [1001, 1002, 1004, 1005, 1007, 1008, 1009, 1011, 1012]

    negated = backend.transform_dataset("orders", {"filter": "quantity * unit_price not between 100 and 500",
                                                   "select": ["order_id"], "sort": "order_id"})
    rewrite = advice(negated, "sql_spelling")["rewrite"]
    assert rewrite[0]["arguments"]["transform"]["filter"] == (
        "(quantity * unit_price < 100 or quantity * unit_price > 500)")
    assert [row[0] for row in run(backend, rewrite)[0]["result"]["rows"]] == [1003, 1006, 1010]


def test_extract_epoch_is_counted_in_seconds_with_date_diff(backend, tmp_path):
    path = write_csv(tmp_path / "jobs.csv", "job,started,finished",
                     ["build,2026-09-01 10:00:00,2026-09-01 10:01:30", "deploy,2026-09-01 11:00:00,2026-09-01 12:00:00"])
    assert backend.import_dataset(str(path))["status"] == "success"
    response = backend.transform_dataset("jobs", {"derive": {"seconds": "EXTRACT(EPOCH FROM (finished - started))",
                                                             "at": "extract(epoch from started)"},
                                                  "select": ["job", "seconds", "at"]})
    found = advice(response, "sql_spelling")
    assert found["rewrite"][0]["arguments"]["transform"]["derive"] == {
        "seconds": "date_diff('second', started, finished)",
        "at": "date_diff('second', cast('1970-01-01' as timestamp), started)"}
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["rows"] == [["build", 90, 1788256800], ["deploy", 3600, 1788260400]]


def test_a_between_or_epoch_with_no_mechanical_respelling_is_explained(backend, orders):
    unfinished = advice(backend.transform_dataset("orders", {"filter": "amount between 100"}), "sql_spelling")
    assert "x >= a and x <= b" in unfinished["explanation"] and "rewrite" not in unfinished
    summed = advice(backend.transform_dataset("orders", {"derive": {"s": "extract(epoch from order_date - order_date + order_date)"}}),
                    "sql_spelling")
    assert "date_diff('second', start, end)" in summed["explanation"] and "rewrite" not in summed


def test_ilike_and_like_on_a_function_result_become_contains(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "region = 'West' AND (LOWER(customer) LIKE '%acme%' OR customer ILIKE '%UMBRELLA%')",
                                                    "select": ["order_id", "customer"]})
    found = advice(response, "like_as_function")
    assert found["rewrite"][0]["arguments"]["transform"]["filter"] == (
        "region = 'West' AND (contains(LOWER(customer), 'acme') OR contains(lower(customer), 'umbrella'))")
    (result,) = run(backend, found["rewrite"])
    assert sorted(result["result"]["rows"]) == [[1001, "Acme Corp"], [1004, "Umbrella"]]


def test_case_in_the_first_derive_becomes_a_raw_query_first_step(backend, orders):
    response = backend.transform_dataset("orders", [{"derive": {"size": "CASE WHEN quantity >= 10 THEN 'big' ELSE 'small' END"}},
                                                    {"select": ["order_id", "size"]}, {"sort": "order_id"}, {"limit": 2}])
    found = advice(response, "sql_in_expression")
    assert found["explanation"].startswith("CASE is SQL that semantic expressions do not have.")
    assert found["rewrite"][0]["arguments"]["transform"][0] == {
        "raw_query": {"sql": "SELECT *, CASE WHEN quantity >= 10 THEN 'big' ELSE 'small' END AS \"size\" FROM input"}}
    (result,) = run(backend, found["rewrite"])
    assert result["used_raw_query"] is True and result["result"]["rows"] == [[1001, "big"], [1002, "small"]]


def test_a_window_in_the_first_filter_becomes_a_qualify_query(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "row_number() OVER (PARTITION BY region ORDER BY amount DESC) = 1",
                                                    "select": ["region", "amount"]})
    found = advice(response, "sql_in_expression")
    assert found["rewrite"][0]["arguments"]["transform"] == [  # the normalized steps, the first as the query
        {"raw_query": {"sql": "SELECT * FROM input QUALIFY row_number() OVER (PARTITION BY region ORDER BY amount DESC) = 1"}},
        {"type": "select", "select": ["region", "amount"]}]
    (result,) = run(backend, found["rewrite"])
    assert sorted(result["result"]["rows"]) == [["East", 900.0], ["North", 450.0], ["South", 495.0], ["West", 450.0]]


def test_a_scalar_subquery_over_the_source_becomes_a_query_and_one_over_another_dataset_is_explained(backend, orders, tmp_path):
    response = backend.transform_dataset("orders", {"filter": "amount = (SELECT MAX(amount) FROM input)", "select": ["order_id"]})
    found = advice(response, "sql_in_expression")
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["rows"] == [[1006]]

    customers(backend, tmp_path)
    other = advice(backend.transform_dataset("orders", {"filter": "customer = (SELECT MIN(name) FROM customers)"}), "sql_in_expression")
    assert "rewrite" not in other and "under inputs" in other["explanation"]
    later = advice(backend.transform_dataset("orders", [{"filter": "region = 'West'"},
                                                        {"derive": {"size": "CASE WHEN quantity >= 10 THEN 'big' END"}}]),
                   "sql_in_expression")
    assert "rewrite" not in later and "first step, a filter or a derive" in later["explanation"]


def test_a_cast_that_meets_a_value_it_cannot_convert_is_retried_with_try_cast(backend, tmp_path):
    amounts_as_text(backend, tmp_path)
    response = backend.transform_dataset("amounts", [{"derive": {"x": "cast(amount as double)"}}, {"select": ["id", "x"]}])
    assert response["code"] == "EXECUTION_FAILED"
    found = advice(response, "cast_conversion")
    assert found["rewrite"][0]["arguments"]["transform"] == [{"derive": {"x": "try_cast(amount as double)"}}, {"select": ["id", "x"]}]
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["rows"] == [[1, 10.5], [2, None], [3, None], [4, 40.0]]

    postfix = advice(backend.transform_dataset("amounts", {"filter": "amount::double > 20"}), "cast_conversion")
    assert postfix["rewrite"][0]["arguments"]["transform"] == {"filter": "try_cast(amount as double) > 20"}
    (kept,) = run(backend, postfix["rewrite"])
    assert kept["result"]["rows"] == [[4, "40"]]


def test_a_window_after_a_filter_is_explained_not_moved_ahead_of_it(backend, orders):
    # In a compact object the filter runs first; a query in its place would rank every order, then filter.
    compact = backend.transform_dataset("orders", {"filter": "region = 'West'",
                                                   "derive": {"r": "row_number() OVER (ORDER BY amount DESC, order_id)"},
                                                   "select": ["order_id", "r"], "sort": "r"})
    found = advice(compact, "sql_in_expression")
    assert "rewrite" not in found
    listed = backend.transform_dataset("orders", [{"type": "filter", "filter": "region = 'West'"},
                                                  {"type": "filter", "filter": "amount = (SELECT MAX(amount) FROM input)"}])
    assert "rewrite" not in advice(listed, "sql_in_expression")


def test_a_window_derive_before_a_filter_keeps_the_filter_after_the_query(backend, orders):
    response = backend.transform_dataset("orders", [{"derive": {"r": "row_number() OVER (ORDER BY amount DESC, order_id)"}},
                                                    {"filter": "region = 'West'"}, {"select": ["order_id", "r"]}, {"sort": "r"}])
    found = advice(response, "sql_in_expression")
    assert found["rewrite"][0]["arguments"]["transform"] == [
        {"raw_query": {"sql": 'SELECT *, row_number() OVER (ORDER BY amount DESC, order_id) AS "r" FROM input'}},
        {"type": "filter", "filter": "region = 'West'"}, {"type": "select", "select": ["order_id", "r"]},
        {"type": "sort", "sort": "r"}]
    (result,) = run(backend, found["rewrite"])
    assert result["result"]["rows"] == [[1004, 4], [1009, 9], [1001, 10]]  # ranked over all orders, as written


def test_respelling_and_try_cast_leave_string_literals_as_written(backend, tmp_path):
    labels = write_csv(tmp_path / "labels.csv", "id,label", ["1,Use `code`", '2,"Use ""code"""', "3,EXTRACT(DAY FROM x)"])
    assert backend.import_dataset(str(labels), name="labels")["status"] == "success"
    ticked = advice(backend.transform_dataset("labels", {"filter": "`label` = 'Use `code`'"}), "sql_spelling")
    assert ticked["rewrite"][0]["arguments"]["transform"] == {"filter": "\"label\" = 'Use `code`'"}
    (rows,) = run(backend, ticked["rewrite"])
    assert rows["result"]["rows"] == [[1, "Use `code`"]]
    extract = advice(backend.transform_dataset("labels", {"filter": "`label` = 'EXTRACT(DAY FROM x)'"}), "sql_spelling")
    assert extract["rewrite"][0]["arguments"]["transform"] == {"filter": "\"label\" = 'EXTRACT(DAY FROM x)'"}

    amounts_as_text(backend, tmp_path)
    tagged = advice(backend.transform_dataset("amounts", {"derive": {"tag": "concat('cast(amount as int)::x=', cast(amount as int))"},
                                                          "select": ["id", "tag"]}), "cast_conversion")
    assert tagged["rewrite"][0]["arguments"]["transform"]["derive"] == {
        "tag": "concat('cast(amount as int)::x=', try_cast(amount as int))"}
    (tags,) = run(backend, tagged["rewrite"])
    assert [tag for _, tag in tags["result"]["rows"]] == [  # the literal prefix is kept on every row
        "cast(amount as int)::x=11", "cast(amount as int)::x=", "cast(amount as int)::x=", "cast(amount as int)::x=40"]
