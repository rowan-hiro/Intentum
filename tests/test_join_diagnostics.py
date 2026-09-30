"""What a semantic join did to its inputs, reported on the successful response (MADR 0010).

A join can drop left rows that match nothing and multiply left rows whose key matches several right rows, and
the two can cancel in the row count. Every semantic join step reports its counts under ``joins`` as data facts;
advice follows only when rows were dropped or multiplied, and it never calls a join wrong. The fixtures are
small, made-up datasets: accounts and ledgers, orders and promotions, stations and readings.
"""

import importlib

import pytest

from agent_backend import Backend
from agent_backend.core.execution import Executor

server_main = importlib.import_module("agent_backend.mcp.server.main")


def kinds(response):
    return [a["kind"] for a in response.get("advice", [])]


def advice(response, kind):
    found = [a for a in response.get("advice", []) if a["kind"] == kind]
    assert found, f"no {kind} advice in {response}"
    return found[0]["explanation"]


def rows(backend, name, records):
    response = backend.import_dataset(rows=records, name=name)
    assert response["status"] == "success", response
    return response


@pytest.fixture
def probes(backend):
    rows(backend, "probe_left", [{"account": "A", "amount": 10}, {"account": "B", "amount": 20}])
    rows(backend, "probe_right", [{"account": "A", "category": "first"}, {"account": "A", "category": "second"}])


JOIN = {"join": {"right": "probe_right", "on": {"account": "account"}, "how": "inner"}}


def test_a_join_that_drops_one_row_and_adds_another_says_so(backend, probes):
    response = backend.transform_dataset("probe_left", JOIN, preview_limit=10)
    assert response["status"] == "success"
    assert response["result"]["row_count"] == 2
    assert response["result"]["rows"] == [["A", 10, "first"], ["A", 10, "second"]]
    [facts] = response["joins"]
    assert facts["step"] == "transform.steps[0] (join)" and facts["how"] == "inner"
    assert facts["right"]["name"] == "probe_right" and facts["on"] == {"account": "account"}
    assert (facts["left_rows"], facts["matched_left_rows"], facts["unmatched_left_rows"]) == (2, 1, 1)
    assert (facts["left_keys"], facts["matched_left_keys"], facts["unmatched_left_keys"]) == (2, 1, 1)
    assert facts["left_keys_with_multiple_matches"] == 1 and facts["rows_added_by_multiple_matches"] == 1
    assert facts["null_key_left_rows"] == 0 and facts["rows_out"] == 2 and facts["match_coverage"] == 0.5
    assert facts["unmatched_left_key_sample"] == [{"key": {"account": "B"}, "left_rows": 1}]
    assert facts["multiple_match_sample"] == [{"key": {"account": "A"}, "left_rows": 1, "right_rows": 2}]
    assert kinds(response) == ["join_multiplied_rows", "join_unmatched_rows"]
    multiplied = advice(response, "join_multiplied_rows")
    assert "account = 'A', which matches 2 rows" in multiplied and "added 1 row" in multiplied
    assert ("as many rows as its left input (2) only because it also dropped 1 unmatched left row"
            in multiplied)
    assert "needs no change" in multiplied
    unmatched = advice(response, "join_unmatched_rows")
    assert "1 of 2 left rows (1 key, such as account = 'B')" in unmatched and "not in the result" in unmatched
    assert "rewrite" not in str(response["advice"])


def test_the_same_join_materialized_reports_the_same_facts(backend, probes):
    made = backend.materialize_result("probe_left", JOIN, "probe_joined")
    assert made["status"] == "success" and made["dataset"]["rows"] == 2
    assert made["joins"][0]["unmatched_left_rows"] == 1 and made["joins"][0]["rows_added_by_multiple_matches"] == 1
    assert kinds(made) == ["join_multiplied_rows", "join_unmatched_rows"]
    operation = backend.get_operation(made["operation_id"])
    assert operation["result"]["joins"] == made["joins"]


def test_a_unique_fully_matched_join_reports_full_coverage_and_no_advice(backend):
    rows(backend, "ledger", [{"account": "A", "amount": 10}, {"account": "A", "amount": 5},
                             {"account": "B", "amount": 20}])
    rows(backend, "owners", [{"account": "A", "owner": "Ada"}, {"account": "B", "owner": "Bo"},
                             {"account": "C", "owner": "Cy"}])
    response = backend.transform_dataset("ledger", {"join": {"right": "owners", "on": {"account": "account"}}})
    [facts] = response["joins"]
    assert facts["match_coverage"] == 1.0 and facts["left_keys"] == 2 and facts["matched_left_keys"] == 2
    assert facts["left_keys_with_multiple_matches"] == 0 and facts["rows_added_by_multiple_matches"] == 0
    assert facts["unmatched_right_rows"] == 1 and facts["rows_out"] == 3 == response["result"]["row_count"]
    assert facts["unmatched_left_key_sample"] == [] and facts["multiple_match_sample"] == []
    assert not [k for k in kinds(response) if k.startswith("join_")]


def test_null_keys_are_counted_as_unmatched_and_never_sampled(backend):
    rows(backend, "readings", [{"station": "N1", "value": 1.5}, {"station": None, "value": 2.0},
                               {"station": None, "value": 2.5}, {"station": "S9", "value": 3.0}])
    rows(backend, "stations", [{"station": "N1", "city": "Oslo"}, {"station": None, "city": "unknown"}])
    response = backend.transform_dataset("readings", {"join": {"right": "stations", "on": {"station": "station"}}})
    [facts] = response["joins"]
    assert facts["left_rows"] == 4 and facts["null_key_left_rows"] == 2 and facts["unmatched_left_rows"] == 3
    assert facts["left_keys"] == 2 and facts["unmatched_left_keys"] == 1
    assert facts["unmatched_left_key_sample"] == [{"key": {"station": "S9"}, "left_rows": 1}]
    assert response["result"]["row_count"] == 1 == facts["rows_out"]
    text = advice(response, "join_unmatched_rows")
    assert "3 of 4 left rows (1 key, such as station = 'S9'; 2 rows with a null key, which never matches)" in text
    assert "different identifiers" in text  # a quarter of the left rows matched


def test_only_null_keys_unmatched_skips_the_sample_query(backend, monkeypatch):
    rows(backend, "readings", [{"station": "N1", "value": 1.5}, {"station": None, "value": 2.0}])
    rows(backend, "stations", [{"station": "N1", "city": "Oslo"}])
    queries: list[str] = []
    original = backend.engine.query

    def counting(sql, limit=None):
        queries.append(sql)
        return original(sql, limit)

    monkeypatch.setattr(backend.engine, "query", counting)
    response = backend.transform_dataset("readings", {"join": {"right": "stations", "on": {"station": "station"}}})
    assert response["joins"][0]["null_key_left_rows"] == 1 and response["joins"][0]["unmatched_left_key_sample"] == []
    assert sum("unmatched AS (" in sql for sql in queries) == 0
    assert sum("jm AS (" in sql for sql in queries) == 1


def test_a_left_join_keeps_unmatched_rows_with_nulls(backend, probes):
    response = backend.transform_dataset("probe_left", {"join": {"right": "probe_right", "on": {"account": "account"},
                                                                 "how": "left"}})
    assert response["result"]["row_count"] == 3 == response["joins"][0]["rows_out"]
    text = advice(response, "join_unmatched_rows")
    assert "are kept with nulls in the columns it adds" in text and "A left join keeps" not in text
    assert "only because" not in advice(response, "join_multiplied_rows")  # the row count did change


def test_a_many_to_many_join_is_reported_without_being_called_wrong(backend):
    rows(backend, "shifts", [{"site": "north", "worker": "ana"}, {"site": "north", "worker": "ben"},
                             {"site": "south", "worker": "cai"}])
    rows(backend, "tasks", [{"site": "north", "task": "load"}, {"site": "north", "task": "sort"},
                            {"site": "north", "task": "ship"}, {"site": "south", "task": "load"}])
    response = backend.transform_dataset("shifts", {"join": {"right": "tasks", "on": {"site": "site"}}})
    [facts] = response["joins"]
    assert facts["left_keys_with_multiple_matches"] == 1 and facts["rows_added_by_multiple_matches"] == 4
    assert facts["multiple_match_sample"] == [{"key": {"site": "north"}, "left_rows": 2, "right_rows": 3}]
    assert response["result"]["row_count"] == 7 == facts["rows_out"]
    assert kinds(response) == ["join_multiplied_rows"]
    text = advice(response, "join_multiplied_rows")
    assert "reduce tasks to one row per [site] first" in text and "semi_join" in text
    assert "a join meant to pair each left row with every match needs no change" in text
    assert "wrong" not in text and "incorrect" not in text


def test_the_left_input_is_the_relation_just_before_the_join(backend, probes):
    rows(backend, "labels", [{"category": "first", "label": "one"}])
    response = backend.transform_dataset("probe_left", [
        {"filter": "amount < 15"},
        {"join": {"right": "probe_right", "on": {"account": "account"}}},
        {"join": {"right": "labels", "on": {"category": "category"}, "how": "left"}},
    ])
    first, second = response["joins"]
    assert first["step"] == "transform.steps[1] (join)" and first["left_rows"] == 1
    assert first["unmatched_left_rows"] == 0 and first["rows_out"] == 2
    assert second["step"] == "transform.steps[2] (join)" and second["left_rows"] == 2
    assert second["unmatched_left_key_sample"] == [{"key": {"category": "second"}, "left_rows": 1}]
    assert advice(response, "join_multiplied_rows").startswith("The join with probe_right at transform.steps[1]")
    assert advice(response, "join_unmatched_rows").startswith("The join with labels at transform.steps[2]")


def test_several_keys_and_right_or_full_joins(backend):
    rows(backend, "budget", [{"dept": "ops", "year": 2024, "plan": 5}, {"dept": "ops", "year": 2025, "plan": 6}])
    rows(backend, "spend", [{"dept": "ops", "year": 2024, "spent": 4}, {"dept": "lab", "year": 2024, "spent": 9}])
    on = {"dept": "dept", "year": "year"}
    right = backend.transform_dataset("budget", {"join": {"right": "spend", "on": on, "how": "right"}})
    [facts] = right["joins"]
    assert facts["unmatched_left_key_sample"] == [{"key": {"dept": "ops", "year": 2025}, "left_rows": 1}]
    assert facts["unmatched_right_rows"] == 1 and facts["rows_out"] == 2 == right["result"]["row_count"]
    text = advice(right, "join_unmatched_rows")
    assert "(dept = 'ops', year = 2025)" in text and "1 row of spend match no left row" in text
    full = backend.transform_dataset("budget", {"join": {"right": "spend", "on": on, "how": "full"}})
    assert full["joins"][0]["rows_out"] == 3 == full["result"]["row_count"]
    assert "are kept with nulls in the columns it adds" in advice(full, "join_unmatched_rows")


def test_semi_joins_and_joins_in_or_after_a_raw_query_are_not_diagnosed(backend, probes):
    semi = backend.transform_dataset("probe_left", {"semi_join": {"right": "probe_right", "on": {"account": "account"}}})
    assert semi["status"] == "success" and "joins" not in semi
    sql = "SELECT l.account, r.category FROM input l JOIN probe_right r ON l.account = r.account"
    queried = backend.transform_dataset("probe_left", {"raw_query": {"sql": sql, "inputs": {"probe_right": "probe_right"}}})
    assert queried["status"] == "success" and "joins" not in queried
    after = backend.transform_dataset("probe_left", [{"raw_query": "SELECT * FROM input"}, JOIN])
    assert after["status"] == "success" and after["result"]["row_count"] == 2 and "joins" not in after
    assert not [k for k in kinds(after) if k.startswith("join_")]


def test_turning_diagnostics_off_changes_nothing_but_the_report(tmp_path, clock):
    responses = []
    for enabled in (True, False):
        backend = Backend(tmp_path / f"ws_{enabled}", clock=clock, join_diagnostics=enabled)
        try:
            rows(backend, "probe_left", [{"account": "A", "amount": 10}, {"account": "B", "amount": 20}])
            rows(backend, "probe_right", [{"account": "A", "category": "first"},
                                          {"account": "A", "category": "second"}])
            responses.append(backend.transform_dataset("probe_left", JOIN))
        finally:
            backend.close()
    on, off = responses
    assert on["result"] == off["result"]
    assert "joins" in on and "joins" not in off and not kinds(off)


def test_a_failed_diagnostic_never_fails_the_transform(backend, probes, monkeypatch):
    def broken(self, *args, **kwargs):
        raise RuntimeError("diagnostic failed")

    monkeypatch.setattr(Executor, "_join_facts", broken)
    response = backend.transform_dataset("probe_left", JOIN)
    assert response["status"] == "success" and response["result"]["row_count"] == 2
    assert "joins" not in response and not kinds(response)


def test_the_cli_turns_join_diagnostics_off(tmp_path, monkeypatch):
    seen: list[bool] = []

    class Server:
        def run(self, transport):
            assert transport == "stdio"

    def fake_create_server(backend):
        seen.append(backend.join_diagnostics)
        return Server()

    monkeypatch.setattr(server_main, "create_server", fake_create_server)
    monkeypatch.delenv("AGENT_BACKEND_JOIN_DIAGNOSTICS", raising=False)
    server_main.main(["--workspace", str(tmp_path / "a")])
    server_main.main(["--workspace", str(tmp_path / "b"), "--no-join-diagnostics"])
    monkeypatch.setenv("AGENT_BACKEND_JOIN_DIAGNOSTICS", "0")
    server_main.main(["--workspace", str(tmp_path / "c")])
    assert seen == [True, False, False]


def test_a_left_key_coerced_to_equal_several_right_keys_is_counted_once(backend):
    # 9007199254740993 has no double of its own: compared with the float key it equals 9007199254740992.0 too.
    rows(backend, "readings", [{"sensor": 9007199254740992.0, "value": 1.5}])
    rows(backend, "sensors", [{"sensor": 9007199254740992, "site": "north"},
                              {"sensor": 9007199254740993, "site": "south"}])
    response = backend.transform_dataset("readings", {"join": {"right": "sensors", "on": {"sensor": "sensor"}}})
    assert response["result"]["row_count"] == 2
    [facts] = response["joins"]
    assert (facts["left_rows"], facts["left_keys"], facts["matched_left_keys"]) == (1, 1, 1)
    assert facts["left_keys_with_multiple_matches"] == 1 and facts["rows_added_by_multiple_matches"] == 1
    assert facts["right_rows"] == 2 and facts["unmatched_right_rows"] == 0 and facts["rows_out"] == 2
    assert facts["multiple_match_sample"] == [{"key": {"sensor": 9007199254740992.0}, "left_rows": 1, "right_rows": 2}]
    assert kinds(response) == ["join_multiplied_rows"]
    # the other way round: two integer left keys both equal one float right key, and each matches it once
    reverse = backend.transform_dataset("sensors", {"join": {"right": "readings", "on": {"sensor": "sensor"}}})
    [facts] = reverse["joins"]
    assert reverse["result"]["row_count"] == 2 == facts["rows_out"]
    assert (facts["left_rows"], facts["left_keys"], facts["matched_left_keys"]) == (2, 2, 2)
    assert facts["left_keys_with_multiple_matches"] == 0 and facts["unmatched_right_rows"] == 0
