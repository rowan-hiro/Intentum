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


def test_compound_select_moves_after_aggregate_when_it_uses_measure_alias(backend, orders):
    response = backend.transform_dataset(
        "orders",
        {
            "aggregate": {
                "group_by": ["region"],
                "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}],
            },
            "select": ["region", "revenue"],
            "sort": "region",
        },
        explain=True,
    )

    assert [step["type"] for step in response["explain"]["canonical_ir"]["steps"]] == [
        "aggregate", "select", "sort",
    ]
    assert rows(response) == [["East", 1791.0], ["North", 787.5], ["South", 991.0], ["West", 762.5]]


def test_compound_select_stays_before_aggregate_when_it_only_uses_input_fields(backend, orders):
    response = backend.transform_dataset(
        "orders",
        {
            "select": ["region", "amount"],
            "group_by": ["region"],
            "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}],
        },
        explain=True,
    )

    assert [step["type"] for step in response["explain"]["canonical_ir"]["steps"]] == ["select", "aggregate"]
    assert response["result"]["row_count"] == 4


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


def test_semi_join_keeps_matching_left_rows_without_duplicate_amplification(backend, orders, tmp_path):
    path = write_csv(
        tmp_path / "priority_customers.csv",
        "customer",
        ["Acme Corp", "Acme Corp", "Globex"],
    )
    right = backend.import_dataset(str(path), name="priority_customers")["dataset"]

    response = backend.transform_dataset(
        "orders",
        {
            "semi_join": {"right": "priority customers", "on": "customer"},
            "select": ["order_id", "customer"],
            "sort": "order_id",
        },
        explain=True,
    )

    assert rows(response) == [
        [1001, "Acme Corp"],
        [1002, "Globex"],
        [1006, "Acme Corp"],
        [1007, "Globex"],
        [1011, "Acme Corp"],
        [1012, "Globex"],
    ]
    ir_step = response["explain"]["canonical_ir"]["steps"][0]
    assert ir_step["type"] == "semi_join" and ir_step["right"]["dataset_id"] == right["id"]
    assert "SemiJoin(priority_customers v1 on customer = customer)" in response["plan"]
    assert "WHERE EXISTS" in response["explain"]["sql"]


def test_materialized_semi_join_records_both_inputs_in_lineage(backend, orders, tmp_path):
    path = write_csv(tmp_path / "allowed_regions.csv", "region", ["West", "East", "East"])
    right = backend.import_dataset(str(path), name="allowed_regions")["dataset"]

    response = backend.materialize_result(
        "orders",
        {"type": "semi_join", "right": "allowed_regions", "on": {"left": "region", "right": "region"}},
        "allowed_orders",
    )

    assert response["dataset"]["rows"] == 6
    assert set(response["lineage"]) == {orders["id"], right["id"]}
    provenance = backend.get_provenance("allowed_orders")
    assert {(item["id"], item["relationship"]) for item in provenance["inputs"]} == {
        (orders["id"], "derived_from"),
        (right["id"], "joined_with"),
    }


def test_semi_join_rejects_incomparable_keys(backend, orders, tmp_path):
    path = write_csv(tmp_path / "labels.csv", "label", ["one", "two"])
    backend.import_dataset(str(path), name="labels")

    response = backend.transform_dataset(
        "orders",
        {"type": "semi_join", "right": "labels", "on": {"left": "order_id", "right": "label"}},
    )

    assert response["code"] == "INVALID_TRANSFORM"
    assert "not comparable" in response["message"].lower() or "cannot join" in response["message"].lower()


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


def test_group_by_without_measures_returns_distinct_groups(backend, orders):
    response = backend.transform_dataset("orders", {"type": "aggregate", "group_by": ["region"]}, explain=True)

    assert sorted(rows(response)) == [["East"], ["North"], ["South"], ["West"]]
    assert response["explain"]["canonical_ir"]["steps"][0]["measures"] == []
    assert "HashAggregate(region)" in response["plan"]
    assert 'GROUP BY "region"' in response["explain"]["sql"]


def test_aggregate_without_groups_or_measures_is_invalid_transform(backend, orders):
    response = backend.transform_dataset("orders", {"type": "aggregate"})
    assert response["code"] == "INVALID_TRANSFORM"
    assert "group_by" in response["message"] and "measure" in response["message"]


def test_validator_rejects_canonical_aggregate_without_groups_or_measures(backend, orders):
    import pytest

    from agent_backend.core.errors import InvalidTransformError
    from agent_backend.core.ir import TransformIR

    response = backend.transform_dataset("orders", [], explain=True)
    ir_dict = response["explain"]["canonical_ir"]
    ir_dict["steps"] = [{
        "type": "aggregate",
        "group_by": [],
        "measures": [],
        "output_schema": ir_dict["input_schema"],
    }]
    ir = TransformIR.model_validate(ir_dict)

    with pytest.raises(InvalidTransformError, match="group_by"):
        backend.validator.validate_transform(ir)


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


def test_cast_converts_in_a_derive_and_a_filter_and_reads_sql_type_names(backend, orders):
    response = backend.transform_dataset("orders", [
        {"derive": {"name": "order_ref", "expression": "cast(order_id as varchar(20))"}},
        {"filter": "cast(quantity as double) / 4 > 2"},
        {"select": ["order_ref", "quantity"]},
        {"sort": "order_ref"},
    ])
    assert response["status"] == "success", response
    assert response["result"]["columns"][0] == {"name": "order_ref", "type": "string"}
    assert all(isinstance(ref, str) and quantity > 8 for ref, quantity in response["result"]["rows"])


def test_cast_to_an_unknown_type_names_the_accepted_types(backend, orders):
    response = backend.transform_dataset("orders", {"derive": {"name": "x", "expression": "cast(amount as money)"}})
    assert response["code"] == "INVALID_TRANSFORM"
    assert "Unknown type 'money'" in response["message"] and "'integer'" in response["message"] and "varchar" in response["message"]


def amounts(backend, tmp_path):
    """Amounts kept as text, with a blank and a word among the numbers, as exported spreadsheets often hold them."""
    path = tmp_path / "amounts.csv"
    path.write_text("id,amount\n1,10.5\n2,\n3,n/a\n4,40\n")
    assert backend.import_dataset(str(path), name="amounts")["status"] == "success"


def test_try_cast_yields_null_where_cast_fails(backend, tmp_path):
    amounts(backend, tmp_path)
    safe = backend.transform_dataset("amounts", [{"derive": {"x": "try_cast(amount as double)"}}, {"select": ["id", "x"]}])
    assert safe["status"] == "success", safe
    assert safe["result"]["rows"] == [[1, 10.5], [2, None], [3, None], [4, 40.0]]
    assert safe["result"]["columns"][1] == {"name": "x", "type": "float"}
    assert "try_cast(amount as float)" in safe["plan"]
    kept = backend.transform_dataset("amounts", {"filter": "try_cast(amount as double) > 20"})
    assert kept["result"]["rows"] == [[4, "40"]]
    strict = backend.transform_dataset("amounts", [{"derive": {"x": "cast(amount as double)"}}])
    assert strict["code"] == "EXECUTION_FAILED" and "Conversion Error" in strict["message"]


def test_the_postfix_cast_is_a_strict_cast(backend, orders):
    response = backend.transform_dataset("orders", [{"filter": "quantity::double / 4 > 2"}, {"select": ["order_id"]},
                                                    {"sort": "order_id"}])
    assert response["result"]["rows"] == [[1001], [1007], [1009]]
    derived = backend.transform_dataset("orders", {"derive": {"code": "order_id::varchar(10)"}, "select": ["code"], "limit": 1})
    assert derived["result"]["columns"] == [{"name": "code", "type": "string"}]


def test_a_cast_in_group_by_is_not_taken_for_an_alias(backend, orders):
    # Read as an expression, it gets group_by's own rule and hint instead of a parse error about 'cast(quantity'.
    unnamed = backend.transform_dataset("orders", {"group_by": ["cast(quantity as double)"], "metric": "amount"})
    assert unnamed["message"] == "group_by cannot use the expression 'cast(quantity as double)' without a name."
    named = backend.transform_dataset("orders", {"group_by": ["cast(quantity as double) as q"], "metric": "amount"})
    assert named["status"] == "success" and named["result"]["columns"][0]["name"] == "q"


def test_adding_try_cast_leaves_the_cast_expression_and_its_fingerprints_as_they_were():
    from agent_backend.core.ir import CastExpr

    assert set(CastExpr.model_fields) == {"kind", "expr", "logical_type"}
