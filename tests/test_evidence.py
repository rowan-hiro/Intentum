"""Evidence references from imported rows and cells to the artifacts they were read from (MADR 0021).

The agent reads a document outside the backend and imports what it read with references to where it read it.
The backend checks what it holds facts for (the artifact and its hash, rows, columns, and a span or quote against
a markdown or text document), records the rest as given (a page, a time, a quote in a PDF), and returns the
references through get_provenance, for the imported dataset and for datasets that carry its columns unchanged.
The document is a made-up co-op annual note; the observations are the agent's.
"""

import asyncio
import hashlib
import json

import pytest

from agent_backend import Backend
from agent_backend.core.ir import ImportIR
from agent_backend.mcp.server import create_server
from tests.conftest import write_csv

NOTE = """# Co-op annual note

In 2024 the bakery sold 1,240 loaves.
The cafe served 3,015 cups of tea.
The bakery closed for two weeks in August.
Membership grew to 412 households.
"""
ROWS = [{"unit": "bakery", "year": 2024, "sold": 1240},
        {"unit": "cafe", "year": 2024, "sold": 3015}]


def kinds(response):
    return [a["kind"] for a in response.get("advice", [])]


@pytest.fixture
def files(tmp_path):
    note = tmp_path / "annual_note.md"
    note.write_text(NOTE, encoding="utf-8")
    report = tmp_path / "board_report.pdf"
    report.write_bytes(b"%PDF-1.4 made-up report bytes")
    video = tmp_path / "open_day.mp4"
    video.write_bytes(b"made-up video bytes")
    return {"note": note, "report": report, "video": video}


def imported(backend, evidence, rows=ROWS, name="coop_sales"):
    response = backend.import_dataset(rows=rows, name=name, evidence=evidence)
    assert response["status"] == "success", response
    return response


def refused(backend, evidence, rows=ROWS, name="coop_sales"):
    response = backend.import_dataset(rows=rows, name=name, evidence=evidence)
    assert response["status"] in ("error", "needs_resolution"), response
    assert backend.list_datasets()["count"] == 0
    return response


# -- recording and checking -------------------------------------------------------------------------------------

def test_quotes_and_spans_in_a_text_document_are_checked(backend, files):
    start = NOTE.index("The cafe")
    response = imported(backend, [
        {"artifact": str(files["note"]), "rows": [0], "columns": ["sold"], "quote": "the bakery sold 1,240 loaves"},
        {"artifact": "annual_note.md", "row": 1, "span": [start, start + 34],
         "quote": "The cafe served\n3,015 cups of tea.", "note": "read from the second paragraph"},
    ])
    first, second = response["evidence"]
    assert first["artifact"]["name"] == "annual_note.md" and first["artifact"]["kind"] == "markdown"
    assert first["artifact"]["content_hash"] == hashlib.sha256(NOTE.encode()).hexdigest()
    located = NOTE.index("the bakery sold")
    assert first["locator"] == {"quote": "the bakery sold 1,240 loaves", "span": [located, located + 28]}
    assert set(first["checked"]) == {"artifact", "quote", "span", "columns", "rows"} and first["unchecked"] == []
    assert first["values"] == [{"row": 0, "cells": {"sold": 1240}}]
    assert second["locator"]["span"] == [start, start + 34] and {"span", "quote"} <= set(second["checked"])
    assert second["columns"] is None
    assert second["values"] == [{"row": 1, "cells": {"unit": "cafe", "year": 2024, "sold": 3015}}]
    assert second["note"] == "read from the second paragraph"
    provenance = backend.get_provenance("coop_sales")
    assert [e["id"] for e in provenance["evidence"]] == [first["id"], second["id"]]
    assert {e["association"] for e in provenance["evidence"]} == {"recorded"}
    audit = [e for e in provenance["audit"] if e["event"] == "evidence.recorded"]
    assert audit and audit[0]["details"]["references"] == [first["id"], second["id"]]


def test_a_quote_that_occurs_twice_is_checked_but_not_located(backend, files):
    # Matching is case-sensitive: "The bakery" occurs once, "bakery" twice.
    response = imported(backend, [{"artifact": str(files["note"]), "rows": [0], "quote": "The bakery"}])
    [ref] = response["evidence"]
    start = NOTE.index("The bakery")
    assert ref["locator"]["span"] == [start, start + 10] and "quote_occurrences" not in ref["locator"]
    twice = imported(backend, [{"artifact": str(files["note"]), "rows": [0], "quote": "bakery"}], name="twice")
    [ref] = twice["evidence"]
    assert ref["locator"]["quote_occurrences"] == 2 and "span" not in ref["locator"]
    assert "quote" in ref["checked"] and "span" not in ref["checked"]


@pytest.mark.parametrize("reference, field, fragment", [
    ({"quote": "the bakery sold 1,300 loaves"}, "evidence[0].quote", "does not occur"),
    ({"span": [0, 18], "quote": "Co-op annual report"}, "evidence[0].quote", "does not match the text"),
    ({"span": [10, 5000]}, "evidence[0].span", "runs past the end"),
    ({"span": [8, 8]}, "evidence[0].span", "0 <= start < end"),
    ({"page": 2}, None, None),
    ({"time_s": 3.0}, "evidence[0].time_s", "has no time"),
    ({"rows": [2]}, "evidence[0].rows", "the import holds 2 row(s)"),
    ({"rows": []}, "evidence[0].rows", "non-empty list"),
    ({"columns": ["revenue"]}, "evidence[0].columns", "does not have"),
    ({"where": "p. 3"}, "evidence[0]", "Unknown key(s) ['where']"),
    ({"content_hash": "0" * 64}, "evidence[0].content_hash", "does not match"),
])
def test_what_the_backend_holds_facts_for_is_refused_when_it_does_not_hold(backend, files, reference, field, fragment):
    evidence = [{"artifact": str(files["note"]), **reference}]
    if field is None:  # a page in a text document is recorded as given
        [ref] = imported(backend, evidence)["evidence"]
        assert ref["unchecked"] == ["page"]
        return
    response = refused(backend, evidence)
    assert response["field"] == field and fragment in response["message"]
    assert "evidence_reference" in kinds(response)


def test_a_matching_hash_is_checked(backend, files):
    digest = hashlib.sha256(NOTE.encode()).hexdigest()
    [ref] = imported(backend, [{"artifact": str(files["note"]), "content_hash": f"sha256:{digest.upper()}"}])["evidence"]
    assert "content_hash" in ref["checked"] and ref["rows"] is None and "values" not in ref


def test_pages_and_times_are_recorded_as_given(backend, files):
    response = imported(backend, [
        {"artifact": str(files["report"]), "rows": [0, 1], "page": 3, "quote": "Loaves sold: 1,240"},
        {"artifact": str(files["video"]), "rows": [1], "columns": ["sold"], "time_s": [12.5, 14.0]},
        {"artifact": str(files["video"]), "rows": [0], "time_s": 3},
    ])
    pdf, clip, moment = response["evidence"]
    assert pdf["artifact"]["kind"] == "pdf" and pdf["unchecked"] == ["page", "quote"]
    assert pdf["locator"] == {"page": 3, "quote": "Loaves sold: 1,240"}
    assert clip["artifact"]["kind"] == "video" and clip["locator"] == {"time_s": [12.5, 14.0]}
    assert clip["unchecked"] == ["time_s"] and clip["values"] == [{"row": 1, "cells": {"sold": 3015}}]
    assert moment["locator"] == {"time_s": 3.0}


@pytest.mark.parametrize("reference, fragment", [
    ({"artifact": "video", "page": 1}, "has no pages"),
    ({"artifact": "report", "time_s": [1, 2]}, "has no time"),
    ({"artifact": "report", "page": 0}, "1 or more"),
    ({"artifact": "video", "time_s": [5, 2]}, "0 <= start <= end"),
])
def test_a_place_the_artifact_cannot_have_is_refused(backend, files, reference, fragment):
    reference = {**reference, "artifact": str(files[reference["artifact"]])}
    response = refused(backend, [reference])
    assert fragment in response["message"] and "evidence_reference" in kinds(response)


def test_an_unknown_artifact_is_not_found_with_the_registered_ones(backend, files):
    imported(backend, [{"artifact": str(files["note"])}], name="first")
    response = backend.import_dataset(rows=ROWS, name="second", evidence=[{"artifact": "minutes.md"}])
    assert response["code"] == "NOT_FOUND" and response["field"] == "evidence[0].artifact"
    assert "annual_note.md" in response["message"]
    text = [a for a in response["advice"] if a["kind"] == "evidence_reference"][0]["explanation"]
    assert "annual_note.md" in text
    by_id = backend.import_dataset(rows=ROWS, name="third", evidence=[{"artifact": "art_1"}])
    assert by_id["status"] == "success" and by_id["evidence"][0]["artifact"]["name"] == "annual_note.md"


def test_a_changed_file_leaves_its_text_unchecked(backend, files):
    imported(backend, [{"artifact": str(files["note"])}], name="first")
    files["note"].write_text(NOTE + "Addendum.\n", encoding="utf-8")
    [ref] = imported(backend, [{"artifact": "annual_note.md", "quote": "Addendum."}], name="second")["evidence"]
    assert ref["unchecked"] == ["quote"] and "quote" not in ref["checked"]


# -- file imports, commits and replay ---------------------------------------------------------------------------

def test_a_file_import_checks_row_positions_after_the_load(backend, files, tmp_path):
    readings = write_csv(tmp_path / "readings.csv", "day,unit,sold",
                         ["2024-08-01,bakery,0", "2024-08-02,bakery,0", "2024-09-01,bakery,51"])
    response = backend.import_dataset(str(readings), evidence=[
        {"artifact": str(files["note"]), "rows": [0, 1], "quote": "closed for two weeks in August"}])
    assert response["status"] == "success", response
    [ref] = response["evidence"]
    assert ref["values"] == [{"row": 0, "cells": {"day": "2024-08-01", "unit": "bakery", "sold": 0}},
                             {"row": 1, "cells": {"day": "2024-08-02", "unit": "bakery", "sold": 0}}]
    tables_before = set(backend.engine.list_tables())
    outside = backend.import_dataset(str(readings), name="again", evidence=[
        {"artifact": str(files["note"]), "rows": [3]}])
    assert outside["code"] == "INVALID_INTENT" and outside["field"] == "evidence[0].rows"
    assert set(backend.engine.list_tables()) == tables_before
    assert [d["name"] for d in backend.list_datasets()["datasets"]] == ["readings"]


def test_a_failed_commit_leaves_no_reference_and_no_table(backend, files, monkeypatch):
    tables_before = set(backend.engine.list_tables())

    def broken(refs):
        raise RuntimeError("metadata store failed")

    monkeypatch.setattr(backend.store, "insert_evidence", broken)
    response = backend.import_dataset(rows=ROWS, name="coop_sales", evidence=[{"artifact": str(files["note"])}])
    assert response["status"] == "error"
    assert backend.list_datasets()["count"] == 0 and set(backend.engine.list_tables()) == tables_before
    assert backend.store.conn.execute("SELECT count(*) FROM evidence_refs").fetchone()[0] == 0


def test_evidence_is_part_of_the_replay_fingerprint_only_when_given(backend, files):
    plain = backend.import_dataset(rows=ROWS, name="coop_sales")
    again = backend.import_dataset(rows=ROWS, name="coop_sales")
    assert again.get("idempotent_replay") is True and again["operation_id"] == plain["operation_id"]
    ir = ImportIR(source_path="/x.json", format="json", name="coop_sales", content_hash="abc")
    payload = ir.model_dump(mode="json")
    payload.pop("refined_types")
    payload.pop("evidence")
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert ir.logical_fingerprint() == expected
    referenced = backend.import_dataset(rows=ROWS, name="coop_sales", evidence=[{"artifact": str(files["note"])}])
    assert referenced["code"] == "CONFLICT"  # the same rows with evidence are another request
    first = imported(backend, [{"artifact": str(files["note"]), "rows": [0]}], name="with_evidence")
    replay = backend.import_dataset(rows=ROWS, name="with_evidence", evidence=[{"artifact": "annual_note.md", "rows": [0]}])
    assert replay.get("idempotent_replay") is True and replay["evidence"] == first["evidence"]


def test_references_survive_a_reopened_workspace(tmp_path, clock, files):
    backend = Backend(tmp_path / "ws", clock=clock)
    ref = imported(backend, [{"artifact": str(files["note"]), "rows": [1], "columns": ["sold"]}])["evidence"][0]
    backend.close()
    reopened = Backend(tmp_path / "ws", clock=clock)
    try:
        assert reopened.get_provenance("coop_sales")["evidence"] == [ref]
    finally:
        reopened.close()


# -- carried to later datasets ----------------------------------------------------------------------------------

def carried(provenance):
    return {e["id"]: (e["association"], e.get("carried_as")) for e in provenance["evidence"]}


def test_a_column_carried_unchanged_keeps_its_references(backend, files):
    refs = imported(backend, [
        {"artifact": str(files["note"]), "rows": [0], "columns": ["sold"], "quote": "the bakery sold 1,240 loaves"},
        {"artifact": str(files["note"]), "rows": [1], "columns": ["unit"], "quote": "The cafe"},
    ])["evidence"]
    sold, unit = refs[0]["id"], refs[1]["id"]
    backend.materialize_result("coop_sales", [{"filter": "sold > 1000"}, {"sort": "-sold"}, {"limit": 5},
                                              {"rename": {"sold": "units_sold"}},
                                              {"derive": {"name": "dozens", "expression": "units_sold / 12"}}],
                               "ranked")
    provenance = backend.get_provenance("ranked")
    assert carried(provenance) == {sold: ("columns", {"sold": ["units_sold"]}), unit: ("columns", {"unit": ["unit"]})}
    backend.materialize_result("ranked", {"select": ["unit", "dozens"]}, "dozens_only")
    assert carried(backend.get_provenance("dozens_only")) == {sold: ("lineage", None),
                                                              unit: ("columns", {"unit": ["unit"]})}


def test_group_keys_are_carried_and_measures_are_not(backend, files):
    refs = imported(backend, [{"artifact": str(files["note"]), "columns": ["unit", "sold"]}])["evidence"]
    backend.materialize_result("coop_sales", {"group_by": ["unit"], "measures": [
        {"function": "sum", "field": "sold", "alias": "total"}]}, "totals")
    assert carried(backend.get_provenance("totals")) == {refs[0]["id"]: ("columns", {"unit": ["unit"]})}


def test_a_join_carries_the_right_columns_references(backend, files):
    backend.import_dataset(rows=ROWS, name="coop_sales")
    [ref] = imported(backend, [{"artifact": str(files["note"]), "rows": [0], "quote": "412 households"}],
                     rows=[{"unit": "bakery", "members": 412}], name="members")["evidence"]
    backend.materialize_result("coop_sales", {"join": {"right": "members", "on": {"unit": "unit"}}}, "joined")
    # The join key comes from the left side, so only the right column the join adds carries the reference.
    assert carried(backend.get_provenance("joined")) == {ref["id"]: ("columns", {"members": ["members"]})}
    queried = backend.materialize_result("members", {"raw_query": "SELECT unit, members FROM input"}, "queried")
    assert queried["status"] == "success"
    assert carried(backend.get_provenance("queried")) == {ref["id"]: ("lineage", None)}


def test_the_mcp_tool_takes_evidence(backend, files):
    server = create_server(backend)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert "evidence" in tools["import_dataset"].input_schema["properties"]
    result = asyncio.run(server.call_tool("import_dataset", {
        "rows": ROWS, "name": "coop_sales",
        "evidence": [{"artifact": str(files["note"]), "rows": [1], "quote": "3,015 cups"}]}))
    body = result.structured_content
    assert body["status"] == "success" and "quote" in body["evidence"][0]["checked"]
    provenance = asyncio.run(server.call_tool("get_provenance", {"dataset": "coop_sales"})).structured_content
    assert provenance["evidence"][0]["id"] == body["evidence"][0]["id"]


# -- review of PR 18 --------------------------------------------------------------------------------------------

def test_a_column_named_rowid_does_not_move_row_positions(backend, files, tmp_path):
    response = imported(backend, [{"artifact": str(files["note"]), "rows": [0], "columns": ["amount"]}],
                        rows=[{"rowid": 1, "amount": 10}, {"rowid": 0, "amount": 20}], name="shadowed")
    assert response["evidence"][0]["values"] == [{"row": 0, "cells": {"amount": 10}}]
    ledger = write_csv(tmp_path / "ledger.csv", "rowid,amount", ["2,30", "1,40", "0,50"])
    response = backend.import_dataset(str(ledger), evidence=[{"artifact": str(files["note"]), "rows": [0, 2]}])
    assert response["evidence"][0]["values"] == [{"row": 0, "cells": {"rowid": 2, "amount": 30}},
                                                 {"row": 2, "cells": {"rowid": 0, "amount": 50}}]


def test_a_column_named_row_does_not_hide_the_position(backend, files):
    response = imported(backend, [{"artifact": str(files["note"]), "rows": [2]}],
                        rows=[{"row": "A", "v": 1}, {"row": "B", "v": 2}, {"row": "C", "v": 3}], name="lettered")
    stored = [{"row": 2, "cells": {"row": "C", "v": 3}}]
    assert response["evidence"][0]["values"] == stored
    assert backend.get_provenance("lettered")["evidence"][0]["values"] == stored


def test_a_path_names_its_own_file_before_any_artifact_name(backend, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = tmp_path / "a" / "report.md", tmp_path / "b" / "report.md"
    first.write_text("Alpha wrote 12 pages.\n", encoding="utf-8")
    second.write_text("Beta wrote 30 pages.\n", encoding="utf-8")
    a = imported(backend, [{"artifact": str(first)}], name="from_a")["evidence"][0]["artifact"]
    b = imported(backend, [{"artifact": str(second), "quote": "Beta wrote 30 pages."}], name="from_b")["evidence"][0]
    assert b["artifact"]["id"] != a["id"] and "quote" in b["checked"]
    assert b["artifact"]["content_hash"] == hashlib.sha256(second.read_bytes()).hexdigest()
    ambiguous = backend.import_dataset(rows=ROWS, name="by_name", evidence=[{"artifact": "report.md"}])
    assert ambiguous["status"] == "needs_resolution"
    by_hash = imported(backend, [{"artifact": "report.md", "content_hash": a["content_hash"]}], name="by_hash")
    assert by_hash["evidence"][0]["artifact"]["id"] == a["id"]


def test_a_new_version_of_a_path_is_registered_and_the_old_one_stays_reachable(backend, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("Draft: 12 pages.\n", encoding="utf-8")
    old = imported(backend, [{"artifact": str(report)}], name="draft")["evidence"][0]["artifact"]
    report.write_text("Final: 14 pages.\n", encoding="utf-8")
    new_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    new = imported(backend, [{"artifact": str(report), "content_hash": new_hash, "quote": "Final: 14 pages."}],
                   name="final")["evidence"][0]
    assert new["artifact"]["id"] != old["id"] and new["artifact"]["content_hash"] == new_hash
    assert {"content_hash", "quote"} <= set(new["checked"])
    earlier = imported(backend, [{"artifact": str(report), "content_hash": old["content_hash"]}],
                       name="earlier")["evidence"][0]
    assert earlier["artifact"]["id"] == old["id"] and "content_hash" in earlier["checked"]
    stale = backend.import_dataset(rows=ROWS, name="stale", evidence=[{"artifact": str(report), "content_hash": "1" * 64}])
    assert stale["code"] == "CONFLICT" and stale["field"] == "evidence[0].content_hash"
    assert "evidence_reference" in kinds(stale)
