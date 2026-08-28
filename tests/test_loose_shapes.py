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
