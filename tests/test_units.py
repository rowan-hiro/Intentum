"""Declared units followed through transforms (core/ir/units.py, MADR 0019).

A unit is what was declared for a column; the backend compares units as written (ignoring case and spacing),
converts nothing, and reports an addition, subtraction or comparison of two operands whose declared units differ
as unit_mismatch advice on the successful response. Nothing is refused and no value changes. The fixtures are a
made-up shop ledger and a household budget.
"""

import pytest


def kinds(response):
    return [a["kind"] for a in response.get("advice", [])]


def mismatch(response):
    found = [a for a in response.get("advice", []) if a["kind"] == "unit_mismatch"]
    assert found, f"no unit_mismatch advice in {response}"
    return found[0]["explanation"]


def units(response):
    return {c["name"]: c.get("unit") for c in response["result"]["columns"]}


def schema_units(backend, dataset):
    return {c["name"]: c.get("unit") for c in backend.describe_dataset(dataset)["schema"]}


@pytest.fixture
def ledger(backend):
    response = backend.import_dataset(
        rows=[{"sku": "tea", "revenue": 120.0, "cost": 70.0, "return_rate": 2.5, "bonus": 3.0, "quantity": 4},
              {"sku": "cup", "revenue": 80.0, "cost": 50.0, "return_rate": 5.0, "bonus": 1.0, "quantity": 8}],
        name="ledger",
        schema_hints={"revenue": {"unit": "USD"}, "cost": {"unit": " usd "}, "return_rate": {"unit": "percent"},
                      "quantity": {"unit": "pcs"}},
    )
    assert response["status"] == "success", response
    return response


def test_adding_a_currency_to_a_percentage_is_reported_and_computed_as_before(backend):
    backend.import_dataset(rows=[{"sales": 100.0, "return_rate": 5.0}], name="probe_measurements",
                           schema_hints={"sales": {"unit": "USD"}, "return_rate": {"unit": "percent"}})
    response = backend.transform_dataset("probe_measurements",
                                         {"derive": {"name": "mixed_measure", "expression": "sales + return_rate"}})
    assert response["status"] == "success"
    assert response["result"]["rows"] == [[100.0, 5.0, 105.0]]
    assert units(response) == {"sales": "USD", "return_rate": "percent", "mixed_measure": None}
    text = mismatch(response)
    assert text.startswith("The derive of mixed_measure at transform.steps[0] adds sales (USD) and return_rate (percent).")
    assert "mixed_measure has no unit" in text and "converts nothing" in text and "rewrite" not in str(response["advice"])


def test_one_unit_written_two_ways_agrees_and_carries_over(backend, ledger):
    response = backend.transform_dataset("ledger", {"derive": {"name": "margin", "expression": "revenue - cost"}})
    assert "unit_mismatch" not in kinds(response)
    assert units(response)["margin"] == "USD"
    made = backend.materialize_result("ledger", {"derive": {"name": "margin", "expression": "revenue - cost"}},
                                      "margins")
    assert made["status"] == "success" and "unit_mismatch" not in kinds(made)
    assert schema_units(backend, "margins")["margin"] == "USD"


def test_an_undeclared_unit_is_unknown_and_never_warns(backend, ledger):
    response = backend.transform_dataset("ledger", [{"derive": {"name": "paid", "expression": "revenue + bonus"}},
                                                    {"derive": {"name": "shifted", "expression": "revenue + 1"}}])
    assert "unit_mismatch" not in kinds(response)
    assert units(response)["paid"] is None and units(response)["shifted"] is None


def test_multiplication_and_division_neither_warn_nor_carry_a_unit(backend, ledger):
    response = backend.transform_dataset("ledger", [
        {"derive": {"name": "returned", "expression": "revenue * return_rate / 100"}},
        {"derive": {"name": "unit_price", "expression": "revenue / quantity"}},
        {"derive": {"name": "odd", "expression": "quantity % 3"}},
    ])
    assert "unit_mismatch" not in kinds(response)
    assert units(response)["returned"] is None and units(response)["unit_price"] is None
    assert units(response)["odd"] is None


def test_scale_and_currency_differences_are_reported_and_nothing_is_converted(backend):
    backend.import_dataset(rows=[{"年份": 2025, "支出": 1200.0, "预算": 0.2, "eur": 10.0, "usd": 11.0}],
                           name="household",
                           schema_hints={"支出": {"unit": "元"}, "预算": {"unit": "万元"},
                                         "eur": {"unit": "EUR"}, "usd": {"unit": "USD"}})
    response = backend.transform_dataset("household", [{"derive": {"name": "余额", "expression": "预算 - 支出"}},
                                                       {"derive": {"name": "total", "expression": "eur + usd"}}])
    assert response["result"]["rows"][0][-2:] == [0.2 - 1200.0, 21.0]
    text = mismatch(response)
    assert "subtracts 支出 (元) from 预算 (万元)" in text and "adds eur (EUR) and usd (USD)" in text
    assert "余额, total have no unit" in text


def test_a_comparison_of_different_units_in_a_filter_is_reported(backend, ledger):
    response = backend.transform_dataset("ledger", {"filter": "revenue > return_rate"})
    assert response["result"]["row_count"] == 2
    assert mismatch(response).startswith("The filter at transform.steps[0] compares revenue (USD) > return_rate (percent).")
    same = backend.transform_dataset("ledger", {"filter": "revenue > cost and revenue > 100"})
    assert "unit_mismatch" not in kinds(same)


def test_a_unit_follows_its_column_through_rename_select_sort_and_limit(backend, ledger):
    response = backend.transform_dataset("ledger", [
        {"rename": {"revenue": "sales"}},
        {"select": ["sku", "sales", "return_rate"]},
        {"sort": "-sales"},
        {"limit": 1},
        {"derive": {"name": "mixed", "expression": "sales - return_rate"}},
    ])
    assert units(response)["sales"] == "USD"
    assert "subtracts return_rate (percent) from sales (USD)" in mismatch(response)


def test_measures_keep_their_field_unit_and_counts_have_none(backend, ledger):
    response = backend.transform_dataset("ledger", [
        {"aggregate": {"group_by": ["sku"], "measures": [
            {"function": "sum", "field": "revenue", "alias": "total"},
            {"function": "avg", "field": "return_rate", "alias": "rate"},
            {"function": "max", "field": "cost", "alias": "top_cost"},
            {"count": "*", "as": "lines"}]}},
        {"derive": {"name": "profit", "expression": "total - top_cost"}},
        {"derive": {"name": "mixed", "expression": "total + rate"}},
    ])
    assert units(response) == {"sku": None, "total": "USD", "rate": "percent", "top_cost": " usd ", "lines": None,
                               "profit": "USD", "mixed": None}
    assert "adds total (USD) and rate (percent)" in mismatch(response)


def test_functions_negation_and_casts(backend, ledger):
    response = backend.transform_dataset("ledger", [
        {"derive": {"name": "rounded", "expression": "round(revenue, 1) - cost"}},
        {"derive": {"name": "whole", "expression": "cast(revenue as integer)"}},
        {"derive": {"name": "label", "expression": "cast(revenue as string)"}},
        {"derive": {"name": "refund", "expression": "-cost"}},
        {"derive": {"name": "mixed", "expression": "abs(revenue) + return_rate"}},
    ])
    found = units(response)
    assert found["rounded"] == "USD" and found["whole"] == "USD" and found["refund"] == " usd "
    assert found["label"] is None and found["mixed"] is None
    text = mismatch(response)
    assert "adds abs(revenue) (USD) and return_rate (percent)" in text and "rounded" not in text


def test_a_join_brings_the_right_columns_units(backend, ledger):
    backend.import_dataset(rows=[{"sku": "tea", "target": 100.0}, {"sku": "cup", "target": 90.0}], name="targets",
                           schema_hints={"target": {"unit": "USD"}})
    response = backend.transform_dataset("ledger", [
        {"join": {"right": "targets", "on": {"sku": "sku"}}},
        {"derive": {"name": "gap", "expression": "revenue - target"}},
        {"derive": {"name": "mixed", "expression": "return_rate + target"}},
    ])
    assert units(response)["target"] == "USD" and units(response)["gap"] == "USD"
    assert "adds return_rate (percent) and target (USD)" in mismatch(response)
    semi = backend.transform_dataset("ledger", [{"semi_join": {"right": "targets", "on": {"sku": "sku"}}},
                                                {"derive": {"name": "mixed", "expression": "revenue + return_rate"}}])
    assert "adds revenue (USD) and return_rate (percent)" in mismatch(semi)


def test_a_materialized_unit_is_checked_by_a_later_transform(backend, ledger):
    backend.materialize_result("ledger", [{"derive": {"name": "margin", "expression": "revenue - cost"}},
                                          {"select": ["sku", "margin", "return_rate"]}], "margins")
    response = backend.transform_dataset("margins", {"derive": {"name": "mixed", "expression": "margin + return_rate"}})
    assert "adds margin (USD) and return_rate (percent)" in mismatch(response)


def test_raw_query_outputs_have_no_unit(backend, ledger):
    response = backend.transform_dataset("ledger", [
        {"raw_query": "SELECT revenue, return_rate FROM input"},
        {"derive": {"name": "mixed", "expression": "revenue + return_rate"}},
    ])
    assert response["status"] == "success" and "unit_mismatch" not in kinds(response)
    assert set(units(response).values()) == {None}


def test_units_declared_after_import_are_checked(backend):
    backend.import_dataset(rows=[{"distance": 5.0, "duration": 2.0}], name="trips")
    before = backend.transform_dataset("trips", {"derive": {"name": "mixed", "expression": "distance + duration"}})
    assert "unit_mismatch" not in kinds(before)
    updated = backend.update_metadata("trips", columns={"distance": {"unit": "km"}, "duration": {"unit": "h"}})
    assert updated["status"] == "success", updated
    after = backend.transform_dataset("trips", {"derive": {"name": "mixed", "expression": "distance + duration"}})
    assert "adds distance (km) and duration (h)" in mismatch(after)
    assert after["result"]["rows"] == before["result"]["rows"]
