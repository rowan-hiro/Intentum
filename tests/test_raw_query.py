"""raw_query (MADR 0002): read-only SQL over placeholder-bound inputs, the fallback step of the canonical IR.

The data is the shop fixture: orders, a customers table and a later batch of
orders with the same columns. The shapes are the ones the semantic steps
cannot express: a window over groups, every row tied at a maximum, a union,
a CTE that compares groups with their average, a join of several inputs.
Every refusal is a structured result that names the accepted shape
(MADR 0010), and every rewrite a refusal proposes is sent back and must
succeed.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from pathlib import Path

import pytest

from agent_backend import Backend
from agent_backend.core.errors import BackendError, ExecutionFailedError
from agent_backend.core.ir import OutputMode, OutputSpec, TransformIR
from agent_backend.core.models.entities import LogicalType
from agent_backend.mcp.server import INSTRUCTIONS, create_server
from agent_backend.storage.duckdb import QuerySandbox
from tests.conftest import ORDERS_CSV, write_csv

server_main = importlib.import_module("agent_backend.mcp.server.main")


def rows(response):
    assert response["status"] == "success", response
    return response["result"]["rows"]


def refused(response):
    assert response["status"] == "error", response
    return response["details"]["raw_query"]["refused"]


def advice(response, kind):
    found = [a for a in response.get("advice", []) if a["kind"] == kind]
    assert found, f"no {kind} advice in {response}"
    return found[0]


def run(backend, rewrite):
    """Send a rewrite back, call by call, the way an agent would."""
    responses = []
    for step in rewrite:
        tool, args = step["tool"], dict(step["arguments"])
        if tool == "transform_dataset":
            response = backend.transform_dataset(args.pop("source"), args.pop("transform"), **args)
        elif tool == "materialize_result":
            response = backend.materialize_result(args.pop("source"), args.pop("transform"), args.pop("name"), **args)
        else:
            raise AssertionError(f"unexpected tool in rewrite: {tool}")
        assert response["status"] == "success", response
        responses.append(response)
    return responses


def sandboxes_left(backend) -> list[Path]:
    return list(backend.workspace.root.glob(".raw-query-*"))


@pytest.fixture
def shop(backend, orders, tmp_path):
    customers = write_csv(tmp_path / "customers.csv", "name,segment,country", [
        "Acme Corp,enterprise,DE", "Globex,enterprise,US", "Initech,smb,US", "Umbrella,enterprise,GB", "Hooli,smb,US",
    ])
    late = write_csv(tmp_path / "late_orders.csv", "order_id,order_date,region,customer,product,quantity,unit_price,amount", [
        "1013,2026-08-27,West,Acme Corp,Gizmo,2,450.00,900.00",
        "1014,2026-08-27,East,Initech,Widget,4,12.50,50.00",
    ])
    ids = {"orders": orders["id"]}
    for path in (customers, late):
        response = backend.import_dataset(str(path))
        assert response["status"] == "success", response
        ids[response["dataset"]["name"]] = response["dataset"]["id"]
    return ids


# -- the long tail the semantic steps cannot express ---------------------------------------------

def test_the_latest_order_per_customer_is_a_window_over_groups(backend, shop):
    qualify = backend.transform_dataset("orders", {"raw_query": (
        "SELECT customer, order_id, amount FROM input "
        "QUALIFY row_number() OVER (PARTITION BY customer ORDER BY order_date DESC, order_id DESC) = 1 "
        "ORDER BY customer")})
    assert rows(qualify) == [["Acme Corp", 1011, 450.0], ["Globex", 1012, 100.0], ["Hooli", 1009, 187.5],
                             ["Initech", 1008, 396.0], ["Umbrella", 1010, 594.0]]
    assert qualify["used_raw_query"] is True
    numbered = backend.transform_dataset("orders", [
        {"raw_query": "SELECT customer, order_id, amount FROM (SELECT *, row_number() OVER "
                      "(PARTITION BY customer ORDER BY order_date DESC, order_id DESC) AS rn FROM input) WHERE rn = 1"},
        {"sort": "customer"},
    ])
    assert rows(numbered) == rows(qualify)
    running = backend.transform_dataset("orders", {"raw_query": (
        "SELECT order_id, sum(amount) OVER (PARTITION BY region ORDER BY order_id) AS running, "
        "rank() OVER (ORDER BY amount DESC) AS amount_rank FROM input WHERE region = 'East' ORDER BY order_id")})
    assert rows(running) == [[1002, 297.0, 3], [1006, 1197.0, 1], [1010, 1791.0, 2]]


def test_a_tie_aware_extremum_keeps_every_row_at_the_maximum(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": (
        "SELECT order_id, customer, unit_price FROM input "
        "WHERE unit_price = (SELECT max(unit_price) FROM input) ORDER BY order_id")})
    assert rows(response) == [[1004, "Umbrella", 450.0], [1006, "Acme Corp", 450.0], [1011, "Acme Corp", 450.0]]
    # sort and limit, the semantic way to an extremum, keeps one of the three
    assert len(rows(backend.transform_dataset("orders", {"sort": "-unit_price", "limit": 1}))) == 1


def test_a_union_of_two_same_shaped_datasets_derives_from_both(backend, shop):
    created = backend.materialize_result("orders", {"raw_query": {
        "sql": "SELECT * FROM input UNION ALL SELECT * FROM late ORDER BY order_id",
        "inputs": {"late": "late_orders"}}}, "all_orders")
    assert created["status"] == "success", created
    assert created["dataset"]["rows"] == 14
    assert created["dataset"]["columns"] == ["order_id", "order_date", "region", "customer", "product", "quantity",
                                             "unit_price", "amount"]
    assert created["lineage"] == [shop["orders"], shop["late_orders"]]
    inputs = backend.get_provenance("all_orders")["inputs"]
    assert {(i["name"], i["relationship"]) for i in inputs} == {("orders", "derived_from"), ("late_orders", "derived_from")}
    described = backend.describe_dataset("all_orders")
    assert [c["type"] for c in described["schema"]] == ["integer", "date", "string", "string", "string", "integer",
                                                        "float", "float"]
    assert {u["name"] for u in described["lineage"]["upstream"]} == {"orders", "late_orders"}
    assert backend.integrity_report()["ok"], backend.integrity_report()


def test_a_cte_compares_each_group_with_the_average_of_all_groups(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": (
        "WITH spend AS (SELECT customer, sum(amount) AS spent FROM input GROUP BY customer) "
        "SELECT customer, spent FROM spend WHERE spent > (SELECT avg(spent) FROM spend) ORDER BY spent DESC")})
    assert rows(response) == [["Acme Corp", 1475.0], ["Umbrella", 1044.0]]


def test_a_join_of_several_inputs_is_followed_by_semantic_steps(backend, shop):
    created = backend.materialize_result("orders", [
        {"raw_query": {"sql": "SELECT c.segment, sum(o.amount) AS revenue, count(*) AS orders "
                              "FROM input o JOIN customers c ON o.customer = c.name GROUP BY c.segment",
                       "inputs": {"customers": "customers"}}},
        {"sort": "-revenue"},
        {"rename": {"orders": "order_count"}},
    ], "segment_revenue")
    assert created["status"] == "success", created
    assert created["dataset"]["columns"] == ["segment", "revenue", "order_count"]
    assert created["preview"]["rows"] == [["enterprise", 3166.0, 8], ["smb", 1166.0, 4]]
    assert created["lineage"] == [shop["orders"], shop["customers"]]
    assert "RawQuery(input=orders v1, customers=customers v1) → Sort(revenue desc) → Rename(orders→order_count)" in created["plan"]
    assert backend.integrity_report()["ok"], backend.integrity_report()


def test_set_operations_case_like_and_regular_expressions_all_run(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT customer FROM input WHERE product LIKE 'W%' "
               "INTERSECT SELECT name FROM customers WHERE regexp_matches(segment, '^ent') "
               "EXCEPT SELECT customer FROM input "
               "WHERE CASE WHEN amount > 200 THEN true ELSE false END AND product = 'Widget'",
        "inputs": {"customers": "customers"}}})
    assert rows(response) == [["Acme Corp"]]
    pivot = backend.transform_dataset("orders", {"raw_query": (
        "PIVOT input ON product IN ('Widget', 'Gadget') USING sum(quantity) GROUP BY region ORDER BY region")})
    assert [c["name"] for c in pivot["result"]["columns"]] == ["region", "widget", "gadget"]
    assert rows(pivot)[0] == ["East", None, 9]
    recursive = backend.transform_dataset("orders", {"raw_query": (
        "WITH RECURSIVE days(d) AS (SELECT min(order_date) FROM input UNION ALL "
        "SELECT d + 1 FROM days WHERE d < DATE '2026-08-28') SELECT d FROM days ORDER BY d")})
    assert [r[0] for r in rows(recursive)] == ["2026-08-26", "2026-08-27", "2026-08-28"]


def test_the_step_may_be_written_in_every_form_the_other_steps_take(backend, shop):
    sql = "SELECT customer, amount FROM input WHERE region = 'West'"
    expected = rows(backend.transform_dataset("orders", {"raw_query": sql}))
    assert rows(backend.transform_dataset("orders", {"type": "raw_query", "sql": sql})) == expected
    assert rows(backend.transform_dataset("orders", [{"sql": sql}])) == expected
    assert rows(backend.transform_dataset("orders", {"raw_query": {"sql": sql}})) == expected
    # a compact object runs the query first, then its other keys in their usual order
    compact = backend.transform_dataset("orders", {"raw_query": sql, "sort": "-amount", "limit": 2})
    assert rows(compact) == [["Umbrella", 450.0], ["Hooli", 187.5]]
    # inputs may list names that are dataset references themselves
    listed = backend.transform_dataset("orders", {"raw_query": "SELECT count(*) AS n FROM input JOIN customers "
                                                               "ON input.customer = customers.name",
                                                  "inputs": ["customers"]})
    assert rows(listed) == [[12]]
    # a placeholder is an identifier in any script, like the rest of the language
    unicode = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT count(*) AS n FROM input JOIN 客户 ON input.customer = 客户.name WHERE 客户.segment = 'smb'",
        "inputs": {"客户": "customers"}}})
    assert rows(unicode) == [[4]]


def test_an_input_is_resolved_like_any_dataset_reference(backend, shop, tmp_path):
    loose = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT count(*) AS n FROM input o JOIN c ON o.customer = c.name", "inputs": {"c": "the customer list"}}})
    assert rows(loose) == [[12]]
    assert any(n["field"] == "transform.steps[0] (raw_query).inputs.c" and n["resolved_to"].startswith("customers")
               for n in loose["resolution"])
    for name in ("returns_north", "returns_south"):
        write_csv(tmp_path / f"{name}.csv", "order_id,reason", ["1001,damaged"])
        assert backend.import_dataset(str(tmp_path / f"{name}.csv"))["status"] == "success"
    ambiguous = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT * FROM input JOIN r USING (order_id)", "inputs": {"r": "returns"}}})
    assert ambiguous["status"] == "needs_resolution"
    assert ambiguous["field"] == "transform.steps[0] (raw_query).inputs.r"
    assert {c["name"] for c in ambiguous["candidates"]} == {"returns_north", "returns_south"}
    missing = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT * FROM input JOIN s USING (order_id)", "inputs": {"s": "shipments"}}})
    assert missing["code"] == "NOT_FOUND" and refused(missing) == "input_not_found"
    taught = advice(missing, "raw_query_input_not_found")
    assert "shipments" in taught["explanation"] and "customers" in taught["explanation"] and "rewrite" not in taught


# -- output schema --------------------------------------------------------------------------------

def test_the_output_schema_is_described_before_execution_and_mapped_to_the_backend_types(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": (
        "SELECT order_id, order_date, amount, amount > 300 AS big, customer, "
        "CAST(order_date AS TIMESTAMP) AS placed_at, count(*) OVER () AS n, max(amount) OVER (PARTITION BY region) "
        "FROM input")}, explain=True)
    columns = response["result"]["columns"]
    assert [(c["name"], c["type"]) for c in columns] == [
        ("order_id", "integer"), ("order_date", "date"), ("amount", "float"), ("big", "boolean"),
        ("customer", "string"), ("placed_at", "timestamp"), ("n", "integer"),
        ("max_amount_over_partition_by_region", "float")]
    assert {"field": "transform.steps[0] (raw_query).columns", "reference": "max(amount) OVER (PARTITION BY region)",
            "resolved_to": "max_amount_over_partition_by_region", "reason": "normalized to snake_case"} in response["resolution"]
    step = response["explain"]["canonical_ir"]["steps"][0]
    assert step["columns"][-1] == "max(amount) OVER (PARTITION BY region)"
    assert step["output_schema"][-1]["name"] == "max_amount_over_partition_by_region"
    # the next step reads the normalized name
    assert rows(backend.transform_dataset("orders", [
        {"raw_query": "SELECT region, max(amount) FROM input GROUP BY region"},
        {"sort": "-max_amount"}, {"limit": 1}])) == [["East", 900.0]]


def test_a_column_outside_the_backend_types_is_refused_with_the_cast_that_fixes_it(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": "SELECT customer, list(order_id) AS ids FROM input GROUP BY customer"})
    assert refused(response) == "output_type"
    assert response["details"]["raw_query"]["columns"] == [{"name": "ids", "type": "BIGINT[]"}]
    assert "CAST" in advice(response, "raw_query_output_type")["explanation"]
    fixed = backend.transform_dataset("orders", {"raw_query": "SELECT customer, CAST(list(order_id ORDER BY order_id) AS VARCHAR) "
                                                              "AS ids FROM input GROUP BY customer ORDER BY customer"})
    assert rows(fixed)[0] == ["Acme Corp", "[1001, 1006, 1011]"]


def test_two_output_columns_with_one_name_are_refused(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT o.customer, c.name AS Customer FROM input o JOIN customers c ON o.customer = c.name",
        "inputs": {"customers": "customers"}}})
    assert refused(response) == "output_name"
    assert "AS" in advice(response, "raw_query_output_name")["explanation"]


def test_previews_stay_within_the_preview_limit(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": "SELECT * FROM input ORDER BY order_id"}, preview_limit=3)
    assert [r[0] for r in rows(response)] == [1001, 1002, 1003]
    assert response["result"]["row_count"] == 12 and response["result"]["truncated"] is True


# -- the operation record: fingerprint, replay, explain, lineage ---------------------------------

def test_the_same_request_replays_and_a_different_sql_text_is_a_different_request(backend, shop):
    request = {"raw_query": {"sql": "SELECT customer, sum(amount) AS spent FROM input GROUP BY customer",
                             "inputs": {}}}
    first = backend.materialize_result("orders", request, "spend")
    again = backend.materialize_result("orders", request, "spend")
    assert again["idempotent_replay"] is True and again["used_raw_query"] is True
    assert again["dataset"]["id"] == first["dataset"]["id"]
    assert backend.list_datasets()["count"] == 4
    other = backend.materialize_result("orders", {"raw_query": "SELECT customer, sum(amount) AS spent FROM input GROUP BY 1"},
                                       "spend")
    assert other["code"] == "CONFLICT"
    preview = {"raw_query": "SELECT count(*) AS n FROM input"}
    once = backend.transform_dataset("orders", preview, idempotency_key="count-orders")
    twice = backend.transform_dataset("orders", preview, idempotency_key="count-orders")
    assert twice["idempotent_replay"] is True and twice["result"] == once["result"]
    # the fingerprint holds the SQL text and the bound versions
    ir = backend.get_operation(first["operation_id"])["canonical_ir"]
    assert ir["steps"][0]["sql"] == request["raw_query"]["sql"]
    assert [(b["placeholder"], b["dataset"]["dataset_id"], b["dataset"]["version"]) for b in ir["steps"][0]["inputs"]] == [
        ("input", shop["orders"], 1)]
    assert TransformIR.model_validate(ir).logical_fingerprint() == backend.describe_dataset("spend")["dataset"]["metadata"]["intent_fingerprint"]
    assert backend.integrity_report()["ok"], backend.integrity_report()


def test_the_recorded_canonical_ir_replays_without_the_model(backend, shop):
    created = backend.materialize_result("orders", {"raw_query": {
        "sql": "SELECT c.country, count(*) AS orders FROM input o JOIN customers c ON o.customer = c.name "
               "GROUP BY c.country ORDER BY c.country", "inputs": {"customers": "customers"}}}, "orders_by_country")
    recorded = TransformIR.model_validate(backend.get_operation(created["operation_id"])["canonical_ir"])
    replay = recorded.model_copy(update={"output": OutputSpec(mode=OutputMode.PREVIEW, preview_limit=100)})
    backend.validator.validate_transform(replay)
    versions = {ref.dataset_id: backend.store.get_version(ref.dataset_id, ref.version) for ref in replay.referenced_datasets()}
    result = backend.executor.execute_transform(replay, backend.planner.plan_transform(replay, versions, None, None))
    assert result.rows == created["preview"]["rows"] == [["DE", 3], ["GB", 2], ["US", 7]]
    assert not sandboxes_left(backend)


def test_explain_lineage_and_the_operation_record_carry_the_query(backend, shop):
    sql = "SELECT o.order_id, c.segment FROM input o JOIN customers c ON o.customer = c.name WHERE o.amount > 400"
    created = backend.materialize_result("orders", {"raw_query": {"sql": sql, "inputs": {"customers": "customers"}}},
                                         "large_orders", explain=True)
    assert created["status"] == "success", created
    assert created["used_raw_query"] is True and created["explain"]["used_raw_query"] is True
    step = created["explain"]["canonical_ir"]["steps"][0]
    assert step["type"] == "raw_query" and step["sql"] == sql and step["columns"] == ["order_id", "segment"]
    assert [(b["placeholder"], b["dataset"]["name"], b["dataset"]["version"]) for b in step["inputs"]] == [
        ("input", "orders", 1), ("customers", "customers", 1)]
    plan = created["explain"]["execution_plan"]["steps"]
    assert plan[0]["kind"] == "RawQuery" and plan[0]["details"]["sql"] == sql
    assert plan[0]["details"]["inputs"]["customers"] == {"dataset_id": shop["customers"], "name": "customers", "version": 1}
    assert [s["kind"] for s in plan].count("RegisterLineage") == 2
    assert sql in created["explain"]["sql"] and "read-only sandbox" in created["explain"]["sql"]
    operation = backend.get_operation(created["operation_id"])
    assert operation["used_raw_query"] is True and operation["canonical_ir"]["steps"][0]["sql"] == sql
    provenance = backend.get_provenance("large_orders")
    assert provenance["produced_by"]["used_raw_query"] is True
    assert {i["name"] for i in provenance["inputs"]} == {"orders", "customers"}
    assert {d["name"] for d in backend.describe_dataset("customers")["lineage"]["downstream"]} == {"large_orders"}
    # a semantic transform does not carry the flag
    semantic = backend.transform_dataset("orders", {"limit": 1})
    assert "used_raw_query" not in semantic
    assert "used_raw_query" not in backend.get_operation(semantic["operation_id"])


def test_no_storage_name_reaches_a_response_without_explain(backend, shop):
    responses = [
        backend.transform_dataset("orders", {"raw_query": "SELECT amout FROM input"}),
        backend.transform_dataset("orders", {"raw_query": "SELECT CAST(region AS INTEGER) AS r FROM input"}),
        backend.transform_dataset("orders", {"raw_query": "SELECT * FROM ds_1_v1"}),
        backend.materialize_result("orders", {"raw_query": "SELECT region, count(*) AS n FROM input GROUP BY region"}, "n_by_region"),
    ]
    assert [r["status"] for r in responses] == ["error", "error", "error", "success"]
    assert responses[1]["code"] == "EXECUTION_FAILED" and responses[1]["recoverable"] is True
    assert advice(responses[1], "raw_query_execution")
    for response in responses:
        text = json.dumps(response)
        assert "ds_1_v1" not in text and "ds_2_v1" not in text and "_result" not in text, text
    assert "input" in responses[0]["message"] or "amount" in responses[0]["message"]
    assert not sandboxes_left(backend)


def test_a_step_that_fails_over_the_query_result_leaves_nothing_behind(backend, shop):
    response = backend.materialize_result("orders", [
        {"raw_query": "SELECT region FROM input"},
        {"derive": {"bad": {"cast": "region", "to": "integer"}}},
    ], "broken")
    assert response["code"] == "EXECUTION_FAILED", response
    assert "_result" not in json.dumps(response) and "-- raw_query" in response["details"]["sql"]
    assert backend.store.get_dataset_by_name("broken") is None
    assert not sandboxes_left(backend)
    assert backend.integrity_report()["ok"], backend.integrity_report()


# -- refusals -------------------------------------------------------------------------------------

REFUSALS = [
    ("create", "CREATE TABLE big AS SELECT * FROM input", "statement"),
    ("drop", "DROP TABLE input", "statement"),
    ("alter", "ALTER TABLE input ADD COLUMN note VARCHAR", "statement"),
    ("insert", "INSERT INTO input SELECT * FROM input", "statement"),
    ("update", "UPDATE input SET amount = 0", "statement"),
    ("delete", "DELETE FROM input WHERE amount < 100", "statement"),
    ("attach", "ATTACH 'other.duckdb' AS other", "statement"),
    ("copy", "COPY input TO 'out.csv'", "statement"),
    ("load", "LOAD httpfs", "statement"),
    ("install", "INSTALL httpfs", "statement"),
    ("pragma", "PRAGMA table_info('input')", "statement"),
    ("set", "SET threads = 1", "statement"),
    ("call", "CALL pragma_version()", "statement"),
    ("describe", "DESCRIBE input", "statement"),
    ("multiple", "SELECT * FROM input; SELECT 1", "statements"),
    ("read_csv", "SELECT * FROM read_csv('orders.csv')", "file_function"),
    ("read_csv_auto", "SELECT * FROM read_csv_auto('orders.csv')", "file_function"),
    ("read_parquet", "SELECT * FROM read_parquet('orders.parquet')", "file_function"),
    ("read_json", "SELECT * FROM read_json('orders.json')", "file_function"),
    ("read_json_auto", "SELECT * FROM read_json_auto('orders.json')", "file_function"),
    ("read_text", "SELECT * FROM read_text('/etc/hostname')", "file_function"),
    ("read_blob", "SELECT * FROM read_blob('/etc/hostname')", "file_function"),
    ("glob", "SELECT * FROM glob('*.csv')", "file_function"),
    ("sqlite_scan", "SELECT * FROM sqlite_scan('shop.sqlite', 'orders')", "file_function"),
    ("network", "SELECT * FROM 'https://example.com/orders.parquet'", "replacement_scan"),
    ("replacement_scan", "SELECT * FROM 'orders.csv'", "replacement_scan"),
    ("table_function", "SELECT * FROM query('SELECT 1')", "table_function"),
    ("catalog_function", "SELECT * FROM duckdb_tables()", "table_function"),
    ("physical_table", "SELECT * FROM ds_1_v1", "physical_table"),
    ("qualified", "SELECT * FROM main.input", "qualified_table"),
    ("catalog_view", "SELECT * FROM information_schema.tables", "qualified_table"),
    ("unknown_placeholder", "SELECT * FROM input JOIN nowhere USING (customer)", "unknown_placeholder"),
    ("parameter", "SELECT * FROM input WHERE region = $region", "parameter"),
    ("positional_parameter", "SELECT * FROM input WHERE amount > ?", "parameter"),
    ("syntax", "SELEC * FROM input", "parse"),
    ("empty", "   ", "empty"),
    ("cte_shadows", "WITH input AS (SELECT 1 AS x) SELECT * FROM input", "cte_shadows_placeholder"),
    ("source_unused", "SELECT 1 AS x", "source_unused"),
]


@pytest.mark.parametrize("sql,expected", [(sql, expected) for _, sql, expected in REFUSALS],
                         ids=[name for name, _, _ in REFUSALS])
def test_every_refusal_is_structured_and_names_the_accepted_shape(backend, shop, sql, expected):
    response = backend.transform_dataset("orders", {"raw_query": sql})
    assert response["code"] == "INVALID_TRANSFORM" and response["recoverable"] is True, response
    assert refused(response) == expected
    assert response["field"].startswith("transform.steps[0] (raw_query)")
    taught = advice(response, f"raw_query_{expected}")
    assert "placeholder" in taught["explanation"] or "SELECT" in taught["explanation"], taught
    assert "ds_1_v1" not in json.dumps(response)
    # the refusal is recorded as a failed operation carrying the same advice
    (failed,) = [op for op in backend.store.list_operations(limit=100) if op.status == "failed"]
    assert failed.error["advice"] == response["advice"]
    assert not sandboxes_left(backend)
    assert backend.integrity_report()["ok"]


def test_a_semantic_step_before_a_raw_query_is_refused_and_the_prefix_materialized_first(backend, shop):
    response = backend.transform_dataset("orders", [
        {"filter": "region in ('East', 'West')"},
        {"raw_query": "SELECT customer, amount FROM input QUALIFY rank() OVER (ORDER BY amount DESC) = 1"},
    ])
    assert refused(response) == "not_first"
    found = advice(response, "raw_query_not_first")
    prepared, query = run(backend, found["rewrite"])
    assert found["rewrite"][0]["arguments"]["transform"] == {"filter": "region in ('East', 'West')"}
    assert rows(query) == [["Acme Corp", 900.0]]


def test_a_dataset_read_without_a_binding_is_bound_by_the_rewrite(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": (
        "SELECT c.segment, count(*) AS n FROM input o JOIN customers c ON o.customer = c.name GROUP BY 1 ORDER BY 1")})
    assert refused(response) == "unknown_placeholder"
    assert response["details"]["raw_query"]["datasets"] == {"customers": "customers"}
    found = advice(response, "raw_query_unknown_placeholder")
    assert found["rewrite"][0]["arguments"]["transform"]["raw_query"]["inputs"] == {"customers": "customers"}
    (result,) = run(backend, found["rewrite"])
    assert rows(result) == [["enterprise", 8], ["smb", 4]]


def test_the_source_read_by_its_own_name_is_bound_under_that_name(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": "SELECT count(*) AS n FROM orders"})
    assert refused(response) == "unknown_placeholder"
    (result,) = run(backend, advice(response, "raw_query_unknown_placeholder")["rewrite"])
    assert rows(result) == [[12]]
    assert result["used_raw_query"] is True


def test_an_input_the_sql_never_reads_is_dropped_by_the_rewrite(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT count(*) AS n FROM input", "inputs": {"customers": "customers", "late": "late_orders"}}})
    assert refused(response) == "unused_input"
    assert response["details"]["raw_query"]["names"] == ["customers", "late"]
    found = advice(response, "raw_query_unused_input")
    assert "inputs" not in found["rewrite"][0]["arguments"]["transform"]["raw_query"]
    (result,) = run(backend, found["rewrite"])
    assert rows(result) == [[12]]


def test_a_query_that_never_reads_its_source_gets_the_dataset_it_reads_as_source(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": {
        "sql": "SELECT segment, count(*) AS n FROM c GROUP BY segment ORDER BY segment", "inputs": {"c": "customers"}}})
    assert refused(response) == "source_unused"
    found = advice(response, "raw_query_source_unused")
    assert found["rewrite"][0]["arguments"]["source"] == "customers"
    (result,) = run(backend, found["rewrite"])
    assert rows(result) == [["enterprise", 3], ["smb", 2]]
    assert result["source"]["name"] == "customers"


def test_a_query_that_reads_no_dataset_is_pointed_at_rows_written_inline(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": "SELECT * FROM (VALUES ('b', 2)) t(name, v)"})
    assert refused(response) == "source_unused"
    found = advice(response, "raw_query_source_unused")
    assert "import_dataset(rows=" in found["explanation"] and "rewrite" not in found
    imported = backend.import_dataset(rows=[{"name": "b", "v": 2}], name="answer")
    assert imported["status"] == "success" and imported["dataset"]["rows"] == 1


def test_a_placeholder_written_as_a_parameter_loses_its_dollar(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": "SELECT count(*) AS n FROM $input WHERE note = '$input'"})
    assert refused(response) == "parse"
    found = advice(response, "raw_query_parse")
    # only the placeholder loses its $; the one inside the string literal is text and stays
    assert found["rewrite"][0]["arguments"]["transform"]["raw_query"]["sql"] == \
        "SELECT count(*) AS n FROM input WHERE note = '$input'"
    fixed = backend.transform_dataset("orders", {"raw_query": "SELECT count(*) AS n FROM $input WHERE region = 'West'"})
    (result,) = run(backend, advice(fixed, "raw_query_parse")["rewrite"])
    assert rows(result) == [[3]]
    # when the statement would still not parse without the $, the refusal explains and proposes nothing
    broken = backend.transform_dataset("orders", {"raw_query": "SELECT count(*) FROM $input WHER region = 'West'"})
    assert refused(broken) == "parse" and "rewrite" not in advice(broken, "raw_query_parse")


def test_create_table_as_becomes_materialize_result_with_that_name(backend, shop):
    response = backend.transform_dataset("orders", {"raw_query": (
        "CREATE TABLE big_orders AS SELECT order_id, amount FROM input WHERE amount >= 450")})
    assert refused(response) == "statement"
    found = advice(response, "raw_query_statement")
    assert found["rewrite"][0]["tool"] == "materialize_result"
    (created,) = run(backend, found["rewrite"])
    assert created["dataset"]["name"] == "big_orders" and created["dataset"]["rows"] == 5
    assert backend.integrity_report()["ok"], backend.integrity_report()


@pytest.mark.parametrize("inputs", [{"input": "customers"}, {"1st": "customers"}, ["the customers"], {"a b": "customers"},
                                    {"ds_2_v1": "customers"}])
def test_a_placeholder_name_is_a_table_name_other_than_input(backend, shop, inputs):
    response = backend.transform_dataset("orders", {"raw_query": {"sql": "SELECT * FROM input", "inputs": inputs}})
    assert refused(response) == "placeholder_name"
    assert "letter" in advice(response, "raw_query_placeholder_name")["explanation"]


def test_the_validator_rechecks_a_tampered_statement(backend, shop):
    created = backend.transform_dataset("orders", {"raw_query": "SELECT customer FROM input"}, explain=True)
    ir = TransformIR.model_validate(created["explain"]["canonical_ir"])
    step = ir.steps[0]
    for tampered in (step.model_copy(update={"sql": "SELECT * FROM read_text('/etc/hostname')"}),
                     step.model_copy(update={"sql": "SELECT region AS customer FROM input", "columns": ["region"]}),
                     step.model_copy(update={"columns": ["region"]})):
        with pytest.raises(BackendError) as info:
            backend.validator.validate_transform(ir.model_copy(update={"steps": [tampered]}))
        assert info.value.code in ("INVALID_TRANSFORM", "INVALID_SCHEMA")


# -- the sandbox and the deadline -----------------------------------------------------------------

def test_the_sandbox_itself_has_no_external_access(backend, shop, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    attempts = [
        f"SELECT * FROM read_text('{secret}')",
        f"SELECT * FROM '{secret}'",
        f"ATTACH '{tmp_path / 'other.duckdb'}' AS other",
        f"COPY input TO '{tmp_path / 'out.csv'}'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET threads = 1",
        "SELECT * FROM ds_1_v1",
    ]
    with backend.queries.session({"input": "ds_1_v1"}) as session:
        for sql in attempts:
            with pytest.raises(BackendError):
                session.run(sql)
        assert session.describe("SELECT count(*) AS n FROM input") == [("n", "BIGINT", "integer")]
    assert not (tmp_path / "out.csv").exists() and not (tmp_path / "other.duckdb").exists()
    assert not sandboxes_left(backend)
    assert backend.engine.list_tables() == ["ds_1_v1", "ds_2_v1", "ds_3_v1"]


def test_a_long_raw_query_is_stopped_at_the_server_deadline(tmp_path, clock):
    backend = Backend(tmp_path / "workspace", clock=clock, query_timeout=0.5)
    try:
        assert backend.import_dataset(str(ORDERS_CSV))["status"] == "success"
        started = time.monotonic()
        response = backend.transform_dataset("orders", {"raw_query": (
            "SELECT sum(a.range * b.range) AS s FROM input, range(200000) a, range(200000) b")})
        elapsed = time.monotonic() - started
        assert response["status"] == "error" and response["code"] == "EXECUTION_FAILED", response
        assert response["recoverable"] is True
        assert response["details"]["raw_query"] == {"refused": "deadline", "timeout_seconds": 0.5}
        assert "0.5 seconds" in advice(response, "raw_query_deadline")["explanation"]
        assert elapsed < 10
        # the backend is intact: the next query runs and nothing is left behind
        assert rows(backend.transform_dataset("orders", {"raw_query": "SELECT count(*) AS n FROM input"})) == [[12]]
        assert not sandboxes_left(backend)
        assert backend.integrity_report()["ok"], backend.integrity_report()
    finally:
        backend.close()


# DuckDB evaluates a COLUMNS lambda while it binds, once per column, and does not check for interrupts there,
# so this statement outlasts a deadline while binding and is stopped as soon as the binding returns.
BINDING_BOMB = "SELECT COLUMNS(c -> list_sum(range(30000000)) > 0) FROM input"

# The deadline the bomb is meant to outlast. One binding of the bomb over eight columns takes about a quarter of a
# second on the fastest machine it was measured on (Apple M5, DuckDB 1.5.5), and a healthy statement goes through
# all of its sandbox steps in under twenty milliseconds there, so the deadline sits between the two with room on
# both sides. At the 0.5 seconds the execution test above uses, the binding returns inside the deadline on that
# machine: nothing is refused while describing, and the statement is stopped later, as an interrupted execution.
BINDING_DEADLINE = 0.05


def recording(calls, name, function):
    def wrapper(*args, **kwargs):
        calls.append(name)
        return function(*args, **kwargs)
    return wrapper


def test_a_binding_that_outlasts_the_deadline_is_stopped_when_it_returns(tmp_path, clock, monkeypatch):
    backend = Backend(tmp_path / "workspace", clock=clock, query_timeout=BINDING_DEADLINE)
    try:
        assert backend.import_dataset(str(ORDERS_CSV))["status"] == "success"
        reached = []
        for step in ("describe_query", "session"):
            monkeypatch.setattr(backend.queries, step, recording(reached, step, getattr(backend.queries, step)))
        started = time.monotonic()
        response = backend.transform_dataset("orders", {"raw_query": BINDING_BOMB})
        elapsed = time.monotonic() - started
        assert response["status"] == "error" and response["code"] == "EXECUTION_FAILED", response
        assert response["recoverable"] is True
        assert response["details"]["raw_query"] == {"refused": "deadline", "timeout_seconds": BINDING_DEADLINE}
        # The documented limit: binding ignores the interrupts, so it overran the deadline before it was stopped.
        assert elapsed > BINDING_DEADLINE
        # Where it was stopped: as the first binding returned. The statement was not described a second time and no
        # sandbox was opened for it, so this is the binding's refusal and not an interrupted execution.
        assert reached == ["describe_query"]
        assert rows(backend.transform_dataset("orders", {"raw_query": "SELECT count(*) AS n FROM input"})) == [[12]]
        assert not sandboxes_left(backend)
    finally:
        backend.close()


def test_nothing_runs_after_the_deadline_has_passed(backend, shop):
    """Describing for validation, describing over the inputs and running all stop once the deadline has passed."""
    sandbox = QuerySandbox(backend.engine, timeout=BINDING_DEADLINE)
    relations = {"input": [(f"c{i}", "INTEGER") for i in range(8)]}
    steps = [lambda: sandbox.describe_query(BINDING_BOMB, relations)]
    with sandbox.session({"input": "ds_1_v1"}) as session:
        steps += [lambda: session.describe(BINDING_BOMB), lambda: session.run(BINDING_BOMB)]
        for step in steps:
            with pytest.raises(ExecutionFailedError) as info:
                step()
            assert info.value.details["raw_query"] == {"refused": "deadline", "timeout_seconds": BINDING_DEADLINE}
        assert session.describe("SELECT count(*) AS n FROM input") == [("n", "BIGINT", LogicalType.INTEGER)]
    assert not sandboxes_left(backend)
    assert backend.engine.list_tables() == ["ds_1_v1", "ds_2_v1", "ds_3_v1"]


@pytest.mark.parametrize("sql, name", [
    # a non-recursive CTE's body does not see its own name: DuckDB binds that reference to the catalog
    ("WITH duckdb_databases AS (SELECT * FROM duckdb_databases) "
     "SELECT path FROM input, duckdb_databases WHERE path IS NOT NULL", "duckdb_databases"),
    ("WITH sqlite_master AS (SELECT * FROM sqlite_master) SELECT name FROM input, sqlite_master", "sqlite_master"),
    # a CTE defined inside a subquery is not in scope outside it
    ("SELECT input.* FROM input, (WITH duckdb_tables AS (SELECT 1 AS k) SELECT * FROM duckdb_tables) s, duckdb_tables",
     "duckdb_tables"),
    # a CTE sees only the CTEs defined before it
    ("WITH a AS (SELECT * FROM b), b AS (SELECT * FROM input) SELECT * FROM a", "b"),
])
def test_a_cte_name_covers_only_the_references_in_its_scope(backend, shop, sql, name):
    response = backend.transform_dataset("orders", {"raw_query": sql})
    assert refused(response) == "unknown_placeholder"
    assert response["details"]["raw_query"]["names"] == [name]
    assert ".raw-query-" not in json.dumps(response) and "_result" not in json.dumps(response)


def test_ctes_are_read_where_duckdb_binds_them(backend, shop):
    chained = ("WITH a AS (SELECT customer, amount FROM input), "
               "b AS (SELECT customer, sum(amount) AS total FROM a GROUP BY customer) SELECT * FROM b ORDER BY customer")
    by_steps = {"aggregate": {"group_by": ["customer"], "measures": [{"function": "sum", "field": "amount", "alias": "total"}]},
                "sort": "customer"}
    assert rows(backend.transform_dataset("orders", {"raw_query": chained})) == rows(backend.transform_dataset("orders", by_steps))
    in_subquery = ("WITH big AS (SELECT order_id FROM input WHERE amount > 500) "
                   "SELECT order_id FROM input WHERE order_id IN (SELECT order_id FROM big) ORDER BY order_id")
    assert rows(backend.transform_dataset("orders", {"raw_query": in_subquery})) == rows(
        backend.transform_dataset("orders", {"filter": "amount > 500", "select": ["order_id"], "sort": "order_id"}))
    recursive = ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
                 "SELECT n FROM r, (SELECT count(*) AS k FROM input) ORDER BY n")
    assert rows(backend.transform_dataset("orders", {"raw_query": recursive})) == [[1], [2], [3]]


def test_there_is_no_deadline_unless_the_server_sets_one(backend, tmp_path):
    assert backend.query_timeout is None and backend.queries.timeout is None
    with pytest.raises(ValueError):
        Backend(tmp_path / "other", query_timeout=0)


def test_the_cli_passes_its_query_timeout_to_the_backend(tmp_path, monkeypatch):
    seen: list[float | None] = []

    class Server:
        def run(self, transport):
            assert transport == "stdio"

    def fake_create_server(backend):
        seen.append(backend.queries.timeout)
        return Server()

    monkeypatch.setattr(server_main, "create_server", fake_create_server)
    monkeypatch.delenv("AGENT_BACKEND_QUERY_TIMEOUT", raising=False)
    server_main.main(["--workspace", str(tmp_path / "a"), "--query-timeout", "2.5"])
    server_main.main(["--workspace", str(tmp_path / "b")])
    monkeypatch.setenv("AGENT_BACKEND_QUERY_TIMEOUT", "4")
    server_main.main(["--workspace", str(tmp_path / "c")])
    assert seen == [2.5, None, 4.0]
    with pytest.raises(SystemExit):
        server_main.main(["--workspace", str(tmp_path / "d"), "--query-timeout", "0"])


# -- the MCP surface ------------------------------------------------------------------------------

def test_the_mcp_tools_expose_the_step_and_return_refusals_as_results(backend):
    server = create_server(backend)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert not set(tools) & {"execute_sql", "query", "sql", "raw_query"}
    for name in ("transform_dataset", "materialize_result"):
        description = tools[name].description
        assert "raw_query" in description and "input" in description and "inputs" in description
    assert "Use the other steps whenever they can express the request" in tools["transform_dataset"].description
    assert "raw_query" in INSTRUCTIONS and "semantic steps" in INSTRUCTIONS

    def call(tool, **arguments):
        result = asyncio.run(server.call_tool(tool, arguments))
        assert result.structured_content is not None and not result.is_error
        return result.structured_content

    assert call("import_dataset", path=str(ORDERS_CSV))["status"] == "success"
    ok = call("transform_dataset", source="orders", transform={"raw_query": "SELECT region, count(*) AS n FROM input "
                                                                             "GROUP BY region ORDER BY region"})
    assert ok["used_raw_query"] is True and ok["result"]["rows"][0] == ["East", 3]
    refusal = call("materialize_result", source="orders", transform={"raw_query": "DROP TABLE input"}, name="gone")
    assert refusal["status"] == "error" and refusal["details"]["raw_query"]["refused"] == "statement"
    assert refusal["advice"][0]["kind"] == "raw_query_statement"
