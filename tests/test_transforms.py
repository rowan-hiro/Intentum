from tests.conftest import write_csv


def rows(response):
    assert response["status"] == "success", response
    return response["result"]["rows"]


def test_aggregate_with_explicit_measures(backend, orders):
    response = backend.transform_dataset(
        "orders",
        {"type": "aggregate", "group_by": ["region"],
         "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}, {"function": "count", "alias": "orders"}]},
    )
    result = dict((r[0], (r[1], r[2])) for r in rows(response))
    assert result["East"] == (1791.0, 3)
    assert response["result"]["row_count"] == 4


def test_compact_form_with_sort_and_limit(backend, orders):
    response = backend.transform_dataset(
        "orders", {"filter": "quantity >= 5", "group_by": ["region"], "metric": "revenue", "sort": "-revenue", "limit": 2}
    )
    assert rows(response) == [["South", 595.0], ["East", 594.0]]
    assert "Filter" in response["plan"] and "Limit(2)" in response["plan"]


def test_filter_object_forms(backend, orders):
    a = backend.transform_dataset("orders", {"filter": {"field": "region", "op": "eq", "value": "West"}})
    b = backend.transform_dataset("orders", {"filter": {"region": "West"}})
    c = backend.transform_dataset("orders", {"filter": {"and": [{"field": "region", "op": "in", "value": ["West", "East"]}, "amount > 300"]}})
    assert rows(a) == rows(b)
    assert len(rows(a)) == 3
    assert {r[2] for r in rows(c)} == {"West", "East"} and all(r[7] > 300 for r in rows(c))


def test_filter_on_date_literal(backend, orders):
    response = backend.transform_dataset("orders", {"filter": "order_date >= '2026-08-26'"})
    assert response["result"]["row_count"] == 12
    bad = backend.transform_dataset("orders", {"filter": "order_date >= 'yesterday'"})
    assert bad["code"] == "TYPE_MISMATCH"


def test_derive_and_rename_and_select(backend, orders):
    response = backend.transform_dataset(
        "orders",
        [
            {"type": "derive", "name": "gross", "expression": "quantity * unit_price"},
            {"type": "rename", "rename": {"customer": "client"}},
            {"type": "select", "columns": ["client", "gross", "amount"]},
            {"type": "sort", "by": ["gross desc"]},
            {"type": "limit", "limit": 1},
        ],
        explain=True,
    )
    assert rows(response) == [["Acme Corp", 900.0, 900.0]]
    assert [c["name"] for c in response["result"]["columns"]] == ["client", "gross", "amount"]
    assert "WITH s0 AS" in response["explain"]["sql"]
    assert response["explain"]["canonical_ir"]["steps"][0]["type"] == "derive"


def test_join(backend, orders, tmp_path):
    path = write_csv(tmp_path / "regions.csv", "region,manager", ["West,Ann", "East,Bob"])
    backend.import_dataset(str(path), name="region_managers")
    response = backend.transform_dataset(
        "orders",
        {"join": {"right": "region managers", "on": "region", "how": "left"}, "group_by": ["manager"], "metric": "revenue", "sort": "manager"},
    )
    assert rows(response) == [["Ann", 762.5], ["Bob", 1791.0], [None, 1778.5]]


def test_type_mismatch_in_arithmetic(backend, orders):
    response = backend.transform_dataset("orders", {"derive": {"label": "region + 1"}})
    assert response["code"] == "TYPE_MISMATCH"
    assert "concat" in response["hint"]


def test_sum_of_string_is_type_mismatch(backend, orders):
    response = backend.transform_dataset("orders", {"group_by": ["region"], "measures": [{"function": "sum", "field": "customer"}]})
    assert response["code"] == "TYPE_MISMATCH"


def test_wrong_value_type_in_filter(backend, orders):
    response = backend.transform_dataset("orders", {"filter": {"field": "amount", "op": ">", "value": "lots"}})
    assert response["code"] == "TYPE_MISMATCH"


def test_missing_measure_is_invalid_transform(backend, orders):
    response = backend.transform_dataset("orders", {"type": "aggregate", "group_by": ["region"]})
    assert response["code"] == "INVALID_TRANSFORM"
    assert "measures" in response["hint"]


def test_unknown_step_type_and_unknown_keys(backend, orders):
    assert backend.transform_dataset("orders", {"type": "pivot"})["code"] == "INVALID_TRANSFORM"
    response = backend.transform_dataset("orders", {"type": "filter", "wehre": "amount > 1"})
    assert response["code"] == "INVALID_INTENT"
    assert "where" in response["details"]["allowed_keys"]


def test_string_transform_is_rejected_with_example(backend, orders):
    response = backend.transform_dataset("orders", "sum amount by region")
    assert response["code"] == "INVALID_TRANSFORM"
    assert "aggregate" in response["hint"]


def test_unknown_function_and_bad_expression(backend, orders):
    assert backend.transform_dataset("orders", {"derive": {"x": "sqrt(amount)"}})["code"] == "INVALID_TRANSFORM"
    assert backend.transform_dataset("orders", {"filter": "amount >"})["code"] == "INVALID_TRANSFORM"


def test_empty_transform_is_identity(backend, orders):
    response = backend.transform_dataset("orders", [])
    assert response["result"]["row_count"] == 12


def test_sort_and_filter_accept_model_style_keys(backend, orders):
    """Shapes a real model produced: sort keys under `order`, filter under `expression`."""
    a = backend.transform_dataset("orders", [
        {"type": "filter", "expression": "amount > 400"},
        {"type": "sort", "order": [{"field": "amount", "direction": "desc"}]},
        {"type": "select", "columns": ["order_id", "amount"]},
    ])
    assert rows(a) == [[1006, 900.0], [1010, 594.0], [1005, 495.0], [1004, 450.0], [1011, 450.0]]
    b = backend.transform_dataset("orders", [{"type": "sort", "by": "amount", "order": "desc"}, {"type": "limit", "limit": 1}])
    assert rows(b)[0][0] == 1006


def test_sort_by_aggregate_alias_after_aggregate(backend, orders):
    response = backend.transform_dataset("orders", [
        {"type": "aggregate", "group_by": ["product"], "measures": [{"avg": "unit_price", "as": "avg_price"}]},
        {"type": "sort", "by": [{"field": "avg_price", "direction": "desc"}]},
    ])
    assert [r[0] for r in rows(response)] == ["Gizmo", "Gadget", "Widget"]


def test_canonical_ir_is_strict(backend, orders):
    """Hand-tampered canonical IR is rejected by the validator."""
    from agent_backend.core.ir import TransformIR

    response = backend.transform_dataset("orders", {"select": ["region"]}, explain=True)
    ir_dict = response["explain"]["canonical_ir"]
    ir_dict["steps"][0]["fields"][0]["logical_type"] = "integer"
    ir = TransformIR.model_validate(ir_dict)
    import pytest

    from agent_backend.core.errors import TypeMismatchError

    with pytest.raises(TypeMismatchError):
        backend.validator.validate_transform(ir)

    ir_dict["extra"] = 1
    with pytest.raises(Exception):
        TransformIR.model_validate(ir_dict)
