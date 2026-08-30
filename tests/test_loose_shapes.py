"""Loose intent shapes a model actually produced against the DataSpace tasks.

Each test here corresponds to a shape observed in the qwen3.5-35b-a3b runs of
2026-08-28 (examples/dataspace/README.md) that the resolver used to refuse.
"""

from tests.conftest import write_csv


def rows(response):
    assert response["status"] == "success", response
    return response["result"]["rows"]


def names(response):
    return [c["name"] for c in response["result"]["columns"]]


def test_list_of_single_key_steps_without_type(backend, orders):
    """[{"filter": ...}, {"select": [...]}, {"sort": ...}] — steps named by their key."""
    response = backend.transform_dataset(
        "orders",
        [{"filter": "amount > 300"}, {"select": ["region", "amount"]}, {"sort": "-amount"}, {"limit": 2}],
    )
    assert names(response) == ["region", "amount"]
    assert rows(response) == [["East", 900.0], ["East", 594.0]]


def test_untyped_compound_step_inside_a_list(backend, orders):
    response = backend.transform_dataset("orders", [{"group_by": ["region"], "metric": "revenue"}, {"sort": "-revenue"}, {"limit": 1}])
    assert rows(response) == [["East", 1791.0]]


def test_untyped_step_without_a_known_key_is_refused(backend, orders):
    response = backend.transform_dataset("orders", [{"pivot": ["region"]}])
    assert response["code"] == "INVALID_INTENT" and "pivot" in response["message"]


def test_sort_with_order_as_the_key_list(backend, orders):
    response = backend.transform_dataset("orders", {"type": "sort", "order": [{"field": "amount", "direction": "desc"}]})
    assert rows(response)[0][7] == 900.0


def test_select_aliases(backend, orders):
    """SQL-style aliasing in select, as a string and as an object."""
    response = backend.transform_dataset("orders", {"select": ["order_date as report_period", "amount"]})
    assert names(response) == ["report_period", "amount"]
    assert "Rename(order_date→report_period)" in response["plan"]

    quoted = backend.transform_dataset("orders", {"select": [{"field": "region", "as": "Zone"}, "amount"]})
    assert names(quoted) == ["zone", "amount"]
    assert any(n["resolved_to"] == "zone" for n in quoted["resolution"])


def test_select_alias_does_not_shadow_a_field_named_like_one(backend, tmp_path):
    path = write_csv(tmp_path / "odd.csv", "value as text,n", ["a,1"])
    backend.import_dataset(str(path))
    response = backend.transform_dataset("odd", {"select": ["value as text"]})
    assert names(response) == ["value as text"]


def test_derive_alias_inside_the_expression(backend, orders):
    response = backend.transform_dataset("orders", {"derive": "quantity * unit_price as line_total"})
    assert names(response)[-1] == "line_total"
    conflict = backend.transform_dataset("orders", {"type": "derive", "name": "gross", "expression": "amount * 2 as double"})
    assert conflict["code"] == "INVALID_TRANSFORM" and "twice" in conflict["message"]


def test_derive_as_a_list_of_objects(backend, orders):
    response = backend.transform_dataset(
        "orders",
        {"type": "derive", "derive": [{"name": "double", "expression": "amount * 2"}, {"name": "half", "expression": "double / 4"}]},
    )
    assert names(response)[-2:] == ["double", "half"]
    assert rows(response)[0][-2:] == [250.0, 62.5]


def test_derive_with_several_pairs(backend, orders):
    response = backend.transform_dataset("orders", {"derive": {"double": "amount * 2", "triple": "amount * 3"}})
    assert names(response)[-2:] == ["double", "triple"]


def test_concat_with_pipes(backend, orders):
    response = backend.transform_dataset("orders", {"derive": {"label": "region || '-' || product"}, "limit": 1})
    assert rows(response)[0][-1] == "West-Widget"


def test_string_slicing_functions(backend, orders):
    response = backend.transform_dataset(
        "orders",
        {"derive": [{"name": "initial", "expression": "left(region, 1)"},
                    {"name": "tail", "expression": "right(product, 3)"},
                    {"name": "middle", "expression": "substr(customer, 1, 4)"}],
         "limit": 1},
    )
    assert rows(response)[0][-3:] == ["W", "get", "Acme"]


def test_strftime_computes_a_date_part(backend, orders):
    response = backend.transform_dataset(
        "orders", {"derive": {"month": "strftime(order_date, '%Y-%m')"}, "group_by": ["month"], "metric": "count"}
    )
    assert rows(response) == [["2026-08", 12]]
    wrong = backend.transform_dataset("orders", {"derive": {"month": "strftime(region, '%Y-%m')"}})
    assert wrong["code"] == "TYPE_MISMATCH"


# -- shapes from the second measurement (examples/dataspace/README.md, 2026-08-28) --

def test_strftime_accepts_the_pattern_first_and_says_so(backend, orders):
    """Thirteen refusals in the task_329 runs were strftime('%Y-%m-%d', column)."""
    response = backend.transform_dataset(
        "orders", {"derive": {"day": "strftime('%Y-%m-%d', order_date)"}, "select": ["day"], "limit": 1}
    )
    assert rows(response) == [["2026-08-26"]]
    assert [n["reason"] for n in response["resolution"]] == ["arguments reordered to the function's signature"]
    ir = backend.get_operation(response["operation_id"])["canonical_ir"]
    args = ir["steps"][0]["expression"]["args"]
    assert [a["kind"] for a in args] == ["column", "literal"]  # one canonical order in the IR


def test_type_error_on_a_function_names_its_signature(backend, orders):
    wrong = backend.transform_dataset("orders", {"derive": {"m": "strftime(region, '%Y-%m')"}})
    assert wrong["code"] == "TYPE_MISMATCH"
    assert "strftime(temporal, string)" in wrong["message"]
    assert wrong["details"] == {"signature": "strftime(temporal, string)", "argument": 1, "actual": "string"}
    arity = backend.transform_dataset("orders", {"derive": {"m": "strftime(order_date)"}})
    assert arity["code"] == "INVALID_TRANSFORM" and arity["details"]["signature"] == "strftime(temporal, string)"


def test_date_and_date_trunc_give_typed_calendar_keys(backend, orders):
    response = backend.transform_dataset(
        "orders", {"derive": {"month": "date_trunc('month', order_date)", "d": "date(order_date)"},
                   "group_by": ["month"], "metric": "count"}
    )
    assert rows(response) == [["2026-08-01", 12]]
    assert [c["type"] for c in response["result"]["columns"]] == ["date", "integer"]
    swapped = backend.transform_dataset("orders", {"derive": {"month": "date_trunc(order_date, 'month')"}, "select": ["month"], "limit": 1})
    assert rows(swapped) == [["2026-08-01"]]
    bad = backend.transform_dataset("orders", {"derive": {"h": "date_trunc('hour', order_date)"}})
    assert bad["code"] == "INVALID_TRANSFORM" and bad["details"]["allowed_parts"] == ["day", "week", "month", "quarter", "year"]


def test_group_by_a_named_expression_derives_it_first(backend, orders):
    """The model tried to group by the expression text itself; a named expression is the shape that works."""
    response = backend.transform_dataset(
        "orders", {"group_by": ["strftime(order_date, '%Y-%m') as month"], "measures": ["count"]}
    )
    assert names(response) == ["month", "count"]
    assert rows(response) == [["2026-08", 12]]
    assert response["plan"].startswith("Scan(orders v1) → Derive(month = ")
    assert any(n["reason"] == "derived before grouping" for n in response["resolution"])
    as_object = backend.transform_dataset(
        "orders", {"group_by": [{"month": "strftime(order_date, '%Y-%m')"}, "region"], "measures": ["count"], "sort": "region"}
    )
    assert names(as_object) == ["month", "region", "count"] and rows(as_object)[0] == ["2026-08", "East", 3]
    unnamed = backend.transform_dataset("orders", {"group_by": ["strftime(order_date, '%Y-%m')"], "measures": ["count"]})
    assert unnamed["code"] == "INVALID_TRANSFORM" and " as day" in unnamed["hint"]
    missing = backend.transform_dataset("orders", {"group_by": ["colour"], "measures": ["count"]})
    assert missing["code"] == "NOT_FOUND"


def test_in_list_with_brackets(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "region in ['East', 'North']", "group_by": ["region"], "metric": "count"})
    assert sorted(rows(response)) == [["East", 3], ["North", 3]]
    negated = backend.transform_dataset("orders", {"filter": "quantity not in [1, 2, 3]", "select": ["quantity"]})
    assert all(q > 3 for (q,) in rows(negated))
    mixed = backend.transform_dataset("orders", {"filter": "region in ['East', 'North')"})
    assert mixed["code"] == "INVALID_TRANSFORM" and "Expected ']'" in mixed["message"]


def test_step_body_nested_under_its_own_key(backend, orders):
    """{"aggregate": {"group_by": ..., "measures": ...}}, alone, in a list and inside a compound step."""
    body = {"group_by": ["region"], "measures": [{"function": "sum", "field": "amount", "alias": "total"}]}
    alone = backend.transform_dataset("orders", {"aggregate": body})
    assert names(alone) == ["region", "total"] and len(rows(alone)) == 4
    listed = backend.transform_dataset("orders", [{"filter": "amount > 300"}, {"aggregate": body}, {"sort": {"field": "total", "direction": "desc"}}, {"limit": {"limit": 1}}])
    assert rows(listed) == [["East", 1494.0]]
    compound = backend.transform_dataset("orders", {"filter": "amount > 300", "aggregate": body, "sort": "-total", "limit": 1})
    assert rows(compound) == [["East", 1494.0]]
    single_measure = backend.transform_dataset("orders", {"aggregate": {"function": "max", "field": "amount"}})
    assert names(single_measure) == ["max_amount"] and rows(single_measure) == [[900.0]]
