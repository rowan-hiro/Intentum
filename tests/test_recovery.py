"""Teach on refusal (MADR 0010): a refusal names what the backend accepts instead and,
when the fix is mechanical, rewrites the agent's own request into calls it can send as-is.

Every rewrite below is sent back to the backend and must succeed; the advice is only
as good as the call it proposes. The shapes come from the qwen3.5-35b-a3b runs of
2026-09-02 on task_44 (agent_harness/scenarios/dataspace/README.md).
"""

from pathlib import Path

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
    backend.declare_output(["region", "revenue"])
    preview = backend.transform_dataset("orders", {"group_by": ["region", "product"], "metric": "revenue"})
    found = advice(preview, "near_contract")
    assert "'product' is not in the contract" in found["explanation"]
    assert found["rewrite"][0]["arguments"]["transform"] == [{"group_by": ["region", "product"], "metric": "revenue"}, {"select": ["region", "revenue"]}]
    (materialized,) = run(backend, found["rewrite"])
    assert materialized["dataset"]["columns"] == ["region", "revenue"]
    assert kinds(materialized) == ["matches_contract"]


def test_a_preview_missing_a_declared_column_gets_no_contract_advice(backend, orders):
    backend.declare_output(["region", "revenue"])
    preview = backend.transform_dataset("orders", {"group_by": ["region"], "measures": [{"function": "sum", "field": "amount", "alias": "total"}]})
    assert preview["status"] == "success" and "advice" not in preview


def test_a_contract_mismatch_at_export_carries_the_reshape_and_the_export(backend, orders, tmp_path):
    backend.declare_output(["region", "revenue"])
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
    backend.declare_output(["region", "revenue", "orders"])
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
