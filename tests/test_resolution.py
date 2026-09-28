"""Tests targeting agent unreliability in references."""

from tests.conftest import write_csv

AGG = {"type": "aggregate", "group_by": ["region"], "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}]}


def test_exact_id_name_and_case_insensitive(backend, orders):
    for ref in ("ds_1", "orders", "ORDERS", "Orders", " orders "):
        response = backend.transform_dataset(ref, AGG)
        assert response["status"] == "success", (ref, response)
        assert response["source"]["id"] == "ds_1"


def test_loose_temporal_reference(backend, orders):
    response = backend.transform_dataset("the orders I imported today", AGG)
    assert response["status"] == "success", response
    assert response["source"]["id"] == "ds_1"
    assert any(n["field"] == "source" for n in response["resolution"])


def test_approximate_dataset_name(backend, orders):
    response = backend.describe_dataset("order")
    assert response["status"] == "success", response
    assert response["dataset"]["id"] == "ds_1"


def test_dataset_alias(backend, orders):
    backend.update_metadata("orders", aliases=["Shop Export"])
    response = backend.describe_dataset("shop export")
    assert response["status"] == "success", response
    assert response["dataset"]["id"] == "ds_1"


def test_ambiguous_reference_returns_candidates(backend, tmp_path, clock):
    write_csv(tmp_path / "a.csv", "region,amount", ["West,1"])
    write_csv(tmp_path / "b.csv", "region,amount", ["East,2"])
    clock.advance(days=-1)
    backend.import_dataset(str(tmp_path / "a.csv"), name="sales_2026_08_26")
    clock.advance(days=1)
    backend.import_dataset(str(tmp_path / "b.csv"), name="sales_daily")

    response = backend.transform_dataset("sales", {"select": ["region"]})
    assert response["status"] == "needs_resolution"
    assert response["code"] == "AMBIGUOUS_REFERENCE"
    assert response["field"] == "source"
    assert {c["id"] for c in response["candidates"]} == {"ds_1", "ds_2"}
    assert response["recoverable"] is True

    # temporal hint disambiguates
    yesterday = backend.transform_dataset("yesterday's sales", {"select": ["region"]})
    assert yesterday["status"] == "success", yesterday
    assert yesterday["source"]["name"] == "sales_2026_08_26"

    # recency word breaks the tie explicitly and reports it
    latest = backend.transform_dataset("the latest sales", {"select": ["region"]})
    assert latest["status"] == "success", latest
    assert latest["source"]["name"] == "sales_daily"
    assert any("most recently created" in n["reason"] for n in latest["resolution"])


def test_result_i_created_earlier(backend, orders):
    backend.materialize_result("orders", AGG, "regional_sales")
    response = backend.describe_dataset("the result I created earlier")
    assert response["status"] == "success", response
    assert response["dataset"]["name"] == "regional_sales"


def test_unknown_dataset_lists_available(backend, orders):
    response = backend.describe_dataset("customers")
    assert response["status"] == "error"
    assert response["code"] == "NOT_FOUND"
    assert response["details"]["available"] == [{"id": "ds_1", "name": "orders"}]


def _library(backend):
    """Tables of a library catalogue: most share the lib prefix, as a schema's tables often do."""
    for name in ("lib_loans", "lib_members", "lib_branch_hours", "catalog_titles"):
        assert backend.import_dataset(rows=[{"id": 1, "note": "x"}], name=name)["status"] == "success"


def test_a_name_that_shares_only_a_prefix_is_not_found_rather_than_ambiguous(backend):
    _library(backend)
    response = backend.describe_dataset("lib_reservations")
    assert response["code"] == "NOT_FOUND", response
    assert response["message"] == ("No dataset matches 'lib_reservations': no dataset name, alias, description or "
                                   "column has 'reservations'.")
    assert response["details"]["absent_words"] == ["reservations"]
    assert [c["name"] for c in response["candidates"]] == ["lib_loans", "lib_members", "lib_branch_hours"]
    assert [a["kind"] for a in response["advice"]] == ["dataset_not_found"]


def test_a_name_that_shares_one_word_does_not_resolve_to_that_dataset(backend, orders):
    _library(backend)
    for reference, absent in (("branch_closures", "closures"), ("orders_west", "west")):
        response = backend.describe_dataset(reference)
        assert response["code"] == "NOT_FOUND", (reference, response)
        assert response["details"]["absent_words"] == [absent]
        assert "rewrite" not in response["advice"][0]


def test_a_name_whose_words_a_dataset_accounts_for_still_resolves(backend, orders):
    _library(backend)
    for reference, name in (("lib_branch", "lib_branch_hours"),  # a prefix of the name
                            ("orderz", "orders"),  # a typo close to the name
                            ("orders.csv", "orders"),  # the file it came from
                            ("lib_loan", "lib_loans")):
        response = backend.describe_dataset(reference)
        assert response["status"] == "success", (reference, response)
        assert response["dataset"]["name"] == name
    # a description is not a name: its words need not all be names
    described = backend.describe_dataset("the orders from the west region")
    assert described["status"] == "success" and described["dataset"]["name"] == "orders"


def test_deleted_dataset_reference(backend, orders):
    backend.delete_dataset("orders")
    response = backend.transform_dataset("orders", AGG)
    assert response["code"] == "NOT_FOUND"
    assert response["details"]["status"] == "deleted"
    assert response["details"]["restorable"] is True
    assert "restore_dataset" in response["hint"]


def test_column_aliases_and_case(backend, orders):
    response = backend.transform_dataset("orders", {"group_by": ["Region"], "metric": "Revenue"})
    assert response["status"] == "success", response
    assert [c["name"] for c in response["result"]["columns"]] == ["region", "revenue"]
    reasons = {n["reference"]: n["reason"] for n in response["resolution"]}
    assert "Region" in reasons and "Revenue" in reasons


def test_ambiguous_column_reference(backend, tmp_path):
    path = write_csv(tmp_path / "t.csv", "region,amount,total", ["West,1,2"])
    backend.import_dataset(str(path), name="t")
    response = backend.transform_dataset("t", {"group_by": ["region"], "metric": "revenue"})
    assert response["status"] == "needs_resolution"
    assert {c["name"] for c in response["candidates"]} == {"amount", "total"}


def test_invalid_column_name_lists_candidates(backend, orders):
    response = backend.transform_dataset("orders", {"select": ["region", "colour"]})
    assert response["code"] == "NOT_FOUND"
    assert "colour" in response["message"]
    assert "region" in [c["name"] for c in response["candidates"]]


def test_search_datasets(backend, orders):
    backend.materialize_result("orders", AGG, "regional_sales", description="Revenue per region")
    response = backend.search_datasets("revenue region")
    assert response["status"] == "success"
    assert response["results"][0]["name"] == "regional_sales"
    assert backend.search_datasets("customer")["results"][0]["name"] == "orders"
