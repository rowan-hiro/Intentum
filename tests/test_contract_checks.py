"""Value checks in output contracts (MADR 0020): the agent declares not_null and inclusive ranges on the columns
the contract names, export_result counts the values that violate them before anything is written, and transform
advice counts them the same way before it says a result matches. The backend reads no task and changes no value.
The fixtures are made-up course grades and weather readings.
"""

import sqlite3

import pytest

from agent_backend import Backend


def kinds(response):
    return [a["kind"] for a in response.get("advice", [])]


def advice(response, kind):
    found = [a for a in response.get("advice", []) if a["kind"] == kind]
    assert found, f"no {kind} advice in {response}"
    return found[0]["explanation"]


@pytest.fixture
def grades(backend):
    response = backend.import_dataset(rows=[
        {"student": "ana", "course": "math", "score": 91.5, "graded_on": "2025-06-02"},
        {"student": "ben", "course": "math", "score": 78.0, "graded_on": "2025-06-03"},
        {"student": "cai", "course": "math", "score": 64.0, "graded_on": "2025-06-04"},
    ], name="grades")
    assert response["status"] == "success", response
    return response


@pytest.fixture
def exports(backend, tmp_path):
    backend.export_root = None
    return tmp_path / "exports"


def shaped(backend, source, name, columns):
    response = backend.materialize_result(source, {"select": list(columns)}, name)
    assert response["status"] == "success", response
    return name


def declare(backend, checks=None, columns=("student", "score"), **kwargs):
    kwargs.setdefault("rows", {"one_per": ["student"]})
    response = backend.declare_output(list(columns), checks=checks, **kwargs)
    assert response["status"] == "success", response
    return response


# -- declaring checks ---------------------------------------------------------------------------------------------

def test_checks_are_declared_as_a_list_an_object_or_inside_a_column(backend):
    listed = declare(backend, [{"column": "score", "not_null": True, "min": 0, "max": 100}])
    assert listed["contract"]["checks"] == [{"column": "score", "not_null": True, "min": 0, "max": 100}]
    assert "its values must hold: score is never null and lies in [0, 100]" in listed["summary"]
    keyed = backend.declare_output(["student", "score"], rows={"one_per": ["student"]},
                                   checks={"score": {"range": [0, 100], "not_null": True}})
    assert keyed["status"] == "success" and keyed.get("unchanged") is True  # the same declaration, written another way
    contract = backend.get_output_contract()
    assert contract["contract"]["checks"] == [{"column": "score", "not_null": True, "min": 0, "max": 100}]


def test_checks_written_inside_column_declarations(backend):
    response = backend.declare_output([{"name": "student", "not_null": True},
                                       {"name": "score", "type": "float", "min": 0}],
                                      rows="at_least_one", checks={"graded_on": {"min": "2025-01-01"}},
                                      order_by=["graded_on"])
    assert response["status"] == "success", response
    assert response["contract"]["checks"] == [{"column": "student", "not_null": True}, {"column": "score", "min": 0},
                                              {"column": "graded_on", "min": "2025-01-01"}]
    assert "score is at least 0" in response["summary"] and "graded_on is at least 2025-01-01" in response["summary"]


@pytest.mark.parametrize("checks, fragment", [
    ([{"column": "grade", "min": 0}], "which the contract does not name"),
    ([{"column": "score"}], "checks nothing"),
    ([{"column": "score", "min": "low"}], "neither a number nor an ISO date"),
    ([{"column": "score", "max": True}], "neither a number nor an ISO date"),
    ([{"column": "score", "min": 100, "max": 0}], "min 100 above max 0"),
    ([{"column": "score", "min": 0, "max": "2025-01-01"}], "mix a number with a date"),
    ([{"column": "score", "min": 0}, {"column": "Score", "max": 1}], "checked twice"),
    ([{"column": "score", "range": [0, 1], "min": 0}], "range beside min or max"),
    ([{"column": "score", "not_null": "yes"}], "must be true or false"),
    ([{"column": "score", "at_least": 0}], "Unknown key(s) ['at_least']"),
    ("score >= 0", "list of checks or an object keyed by column"),
])
def test_malformed_checks_are_refused_with_the_accepted_shape(backend, checks, fragment):
    response = backend.declare_output(["student", "score"], rows="at_least_one", checks=checks)
    assert response["status"] == "error" and response["code"] == "INVALID_INTENT" and response["field"] == "checks"
    assert fragment in response["message"]
    assert "not_null is true or false" in advice(response, "declaration_checks")


def test_a_range_on_a_column_declared_as_text_is_refused(backend):
    response = backend.declare_output([{"name": "student", "type": "string", "min": "a"}], rows="at_least_one")
    assert response["code"] == "INVALID_INTENT" and "neither a number" in response["message"]
    response = backend.declare_output([{"name": "student", "type": "string", "min": 1}], rows="at_least_one")
    assert response["code"] == "INVALID_INTENT" and "declared string" in response["message"]


def test_changing_checks_on_an_open_contract_is_an_amendment(backend):
    declare(backend, [{"column": "score", "min": 0, "max": 100}])
    refused = backend.declare_output(["student", "score"], rows={"one_per": ["student"]})
    assert refused["code"] == "CONFLICT" and refused["field"] == "reason"
    assert "value checks on ['score'] would be dropped" in advice(refused, "contract_amendment")
    amended = backend.declare_output(["student", "score"], rows={"one_per": ["student"]},
                                     checks=[{"column": "score", "min": 50}], reason="only passing grades count")
    assert amended["status"] == "success" and amended["contract"]["revision"] == 2
    assert amended["contract"]["checks"] == [{"column": "score", "min": 50}]
    history = backend.get_output_contract()["history"]
    assert history[-1]["details"]["before"]["checks"] == [{"column": "score", "min": 0, "max": 100}]


# -- holding the export to them -----------------------------------------------------------------------------------

def test_an_export_whose_values_hold_records_the_counts(backend, grades, exports):
    declare(backend, [{"column": "score", "not_null": True, "min": 0, "max": 100},
                      {"column": "graded_on", "min": "2025-06-01", "max": "2025-06-30"}], order_by=["graded_on"])
    dataset = shaped(backend, "grades", "graded", ["student", "score", "graded_on"])
    response = backend.export_result(dataset, str(exports / "grades.csv"))
    assert response["status"] == "success", response
    verified = response["contract"]["verified"]["checks"]
    assert verified == [{"column": "score", "not_null": True, "min": 0, "max": 100, "null_values": 0,
                         "values_outside": 0, "observed_min": 64.0, "observed_max": 91.5},
                        {"column": "graded_on", "min": "2025-06-01", "max": "2025-06-30", "null_values": 0,
                         "values_outside": 0, "observed_min": "2025-06-02", "observed_max": "2025-06-04"}]
    events = [e for e in backend.get_output_contract()["history"] if e["event"] == "output_contract.satisfied"]
    assert events and events[0]["details"]["checks"] == verified
    assert (exports / "grades.csv").read_text().splitlines()[0] == "student,score"


def test_values_outside_a_range_or_null_under_not_null_are_refused_with_evidence(backend, exports):
    backend.import_dataset(rows=[{"station": s, "celsius": c} for s, c in
                                 [("n1", 12.0), ("n2", None), ("n3", 71.5), ("n4", -80.0), ("n5", 3.0)]],
                           name="readings")
    declare(backend, [{"column": "celsius", "not_null": True, "min": -60, "max": 60}], columns=("station", "celsius"),
            rows={"one_per": ["station"]})
    target = exports / "readings.csv"
    response = backend.export_result("readings", str(target))
    assert response["code"] == "CONTRACT_MISMATCH" and response["recoverable"] is True
    assert response["message"].startswith("readings fails the value checks of output contract")
    [problem] = response["details"]["problems"]
    assert problem["kind"] == "check" and problem["column"] == "celsius"
    evidence = problem["evidence"]
    assert evidence["null_values"] == 1 and evidence["values_outside"] == 2
    assert evidence["observed_min"] == -80.0 and evidence["observed_max"] == 71.5
    assert evidence["check"] == {"column": "celsius", "not_null": True, "min": -60, "max": 60}
    assert {r["station"] for r in evidence["rows"]} == {"n2", "n3", "n4"}
    assert "repair" not in response["details"] and "rewrite" not in str(response["advice"])
    assert "changes no value" in advice(response, "contract_mismatch")
    assert not target.exists()


def test_a_null_passes_a_range_and_nan_does_not(backend, exports):
    backend.import_dataset(rows=[{"station": "n1", "celsius": 12.0}, {"station": "n2", "celsius": None}],
                           name="readings")
    declare(backend, [{"column": "celsius", "min": -60, "max": 60}], columns=("station", "celsius"),
            rows={"one_per": ["station"]})
    assert backend.export_result("readings", str(exports / "a.csv"))["status"] == "success"
    backend.materialize_result("readings", [{"derive": {"name": "odd", "expression": "celsius / 0 - celsius / 0"}},
                                            {"select": ["station", "odd"]}], "with_nan")
    declare(backend, [{"column": "odd", "min": 0}], columns=("station", "odd"), rows={"one_per": ["station"]})
    response = backend.export_result("with_nan", str(exports / "b.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    assert response["details"]["problems"][0]["evidence"]["values_outside"] == 1


def test_offending_rows_are_bounded(backend, exports):
    backend.import_dataset(rows=[{"station": f"s{i}", "celsius": 100.0 + i} for i in range(12)], name="hot")
    declare(backend, [{"column": "celsius", "max": 60}], columns=("station", "celsius"), rows="at_least_one")
    response = backend.export_result("hot", str(exports / "hot.csv"))
    evidence = response["details"]["problems"][0]["evidence"]
    assert evidence["values_outside"] == 12 and len(evidence["rows"]) == 5


def test_checks_on_temporal_and_organizing_columns(backend, grades, exports):
    declare(backend, [{"column": "graded_on", "max": "2025-06-03"}], order_by=["graded_on"])
    dataset = shaped(backend, "grades", "graded", ["student", "score", "graded_on"])
    response = backend.export_result(dataset, str(exports / "late.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    evidence = response["details"]["problems"][0]["evidence"]
    assert evidence["values_outside"] == 1 and evidence["rows"] == [{"student": "cai", "score": 64.0,
                                                                     "graded_on": "2025-06-04"}]


def test_a_range_that_cannot_hold_the_column_is_a_mismatch(backend, grades, exports):
    declare(backend, [{"column": "student", "min": 1}])
    response = backend.export_result(shaped(backend, "grades", "graded", ["student", "score"]), str(exports / "x.csv"))
    assert response["code"] == "CONTRACT_MISMATCH"
    assert "'student' is string" in response["details"]["problems"][0]["message"]
    declare(backend, [{"column": "graded_on", "min": 5}], order_by=["graded_on"], reason="a date column now")
    dated = shaped(backend, "grades", "dated", ["student", "score", "graded_on"])
    response = backend.export_result(dated, str(exports / "y.csv"))
    assert "'graded_on' is date" in response["details"]["problems"][0]["message"]


def test_an_empty_dataset_fails_only_its_row_cardinality(backend, grades, exports):
    backend.materialize_result("grades", [{"filter": "score > 1000"}, {"select": ["student", "score"]}], "nobody")
    declare(backend, [{"column": "score", "not_null": True, "min": 0}])
    response = backend.export_result("nobody", str(exports / "none.csv"))
    assert [p["kind"] for p in response["details"]["problems"]] == ["rows"]


def test_a_contract_without_checks_is_unchanged(backend, grades, exports):
    declare(backend)
    response = backend.export_result(shaped(backend, "grades", "graded", ["student", "score"]),
                                     str(exports / "plain.csv"))
    assert response["status"] == "success" and "checks" not in response["contract"]["verified"]
    assert "checks" not in response["contract"]


# -- advice on transforms -----------------------------------------------------------------------------------------

def test_a_result_is_said_to_match_only_after_its_checks_are_counted(backend, grades):
    declare(backend, [{"column": "score", "not_null": True, "min": 60}])
    passing = backend.transform_dataset("grades", {"select": ["student", "score"]})
    assert "matches_contract" in kinds(passing)
    backend.declare_output(["student", "score"], rows={"one_per": ["student"]},
                           checks=[{"column": "score", "min": 70}], reason="a stricter pass mark")
    failing = backend.transform_dataset("grades", {"select": ["student", "score"]})
    assert "matches_contract" not in kinds(failing)
    text = advice(failing, "contract_checks_failed")
    assert "score is at least 70; 1 value falls outside the range" in text and "changes no value" in text
    made = backend.materialize_result("grades", {"select": ["student", "score"]}, "graded")
    assert "graded fails value checks" in advice(made, "contract_checks_failed")
    queried = backend.transform_dataset("grades", {"raw_query": "SELECT student, score FROM input"})
    assert "contract_checks_failed" in kinds(queried)
    kept = backend.transform_dataset("grades", [{"filter": "score >= 70"}, {"select": ["student", "score"]}])
    assert "matches_contract" in kinds(kept)


def test_near_contract_is_offered_only_when_the_checks_hold(backend, grades):
    declare(backend, [{"column": "score", "min": 70}])
    extra = backend.transform_dataset("grades", {"select": ["student", "score", "course"]})
    assert "near_contract" not in kinds(extra) and "contract_checks_failed" in kinds(extra)
    fine = backend.transform_dataset("grades", [{"filter": "score >= 70"}, {"select": ["student", "score", "course"]}])
    assert "near_contract" in kinds(fine)


# -- storage ------------------------------------------------------------------------------------------------------

def test_checks_survive_a_reopened_workspace_and_an_older_one_migrates(tmp_path, clock):
    workspace = tmp_path / "ws"
    backend = Backend(workspace, clock=clock)
    backend.declare_output(["student", "score"], rows="at_least_one", checks={"score": {"min": 0}})
    backend.close()
    reopened = Backend(workspace, clock=clock)
    try:
        assert reopened.get_output_contract()["contract"]["checks"] == [{"column": "score", "min": 0}]
    finally:
        reopened.close()
    old = tmp_path / "old"
    Backend(old, clock=clock).close()
    with sqlite3.connect(old / "metadata.sqlite") as conn:
        conn.execute("ALTER TABLE output_contracts DROP COLUMN checks_json")
    migrated = Backend(old, clock=clock)
    try:
        response = migrated.declare_output(["a"], rows="one", checks={"a": {"not_null": True}})
        assert response["status"] == "success" and response["contract"]["checks"] == [{"column": "a", "not_null": True}]
    finally:
        migrated.close()


def test_checks_reach_the_backend_through_the_mcp_tool(backend):
    import asyncio

    from agent_backend.mcp.server import create_server

    server = create_server(backend)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert "checks" in tools["declare_output"].input_schema["properties"]
    result = asyncio.run(server.call_tool("declare_output", {
        "columns": ["student", "score"], "rows": "at_least_one", "checks": {"score": {"min": 0, "max": 100}}}))
    assert result.structured_content["contract"]["checks"] == [{"column": "score", "min": 0, "max": 100}]
