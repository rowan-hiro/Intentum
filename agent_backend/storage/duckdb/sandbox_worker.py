"""One raw_query sandbox step in a process of its own: bind a statement, or bind and run it (MADR 0002).

The backend runs this file as a script, not as a module of its package, so the
process imports DuckDB and nothing of the backend. It reads one job as JSON on
stdin and writes one JSON object on stdout. A process of its own is what lets
the server's deadline bound binding as well as running: DuckDB evaluates some
expressions while it binds (a COLUMNS lambda, constant folding) without
checking for interrupts, so the one stop that always works is ending the
process, which also keeps a statement's memory out of the server's.

Jobs:
    {"op": "describe", "relations": {placeholder: [[column, type], ...]}, "sql": ...}
        bind against empty tables with the placeholders' schemas
    {"op": "describe", "path": <sandbox database>, "sql": ...}
        bind against the tables in the sandbox database
    {"op": "run", "path": <sandbox database>, "sql": ..., "result": <table>}
        bind, then write the statement's result into <table> in the sandbox database
Replies: {"ok": true, "columns": [[name, type], ...]}, or
{"ok": false, "phase": "prepare" | "binding" | "execution", "message": ...}.
"""

from __future__ import annotations

import json
import sys

import duckdb

# No file, network, extension or setting access; storage/duckdb/sandbox.py relies on these.
SANDBOX_SETTINGS = {
    "enable_external_access": False,
    "autoload_known_extensions": False,
    "autoinstall_known_extensions": False,
    "allow_community_extensions": False,
    "lock_configuration": True,
}


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def message(exc: Exception) -> str:
    """DuckDB's message without the echo of the statement it appends."""
    text = str(exc).split("\n\nLINE ", 1)[0]
    return " ".join(text.split())


def failure(phase: str, text: str) -> dict:
    return {"ok": False, "phase": phase, "message": text}


def bind(conn: duckdb.DuckDBPyConnection, sql: str):
    try:
        relation = conn.sql(sql)
    except duckdb.Error as exc:
        return None, failure("binding", message(exc))
    if relation is None:
        return None, failure("binding", "The statement returns no rows.")
    return relation, None


def columns_of(relation) -> list[list[str]]:
    return [[str(name), str(kind)] for name, kind in zip(relation.columns, relation.types)]


def describe(job: dict) -> dict:
    with duckdb.connect(job.get("path") or ":memory:", config=SANDBOX_SETTINGS) as conn:
        for placeholder, columns in (job.get("relations") or {}).items():
            body = ", ".join(f"{quote_ident(name)} {kind}" for name, kind in columns)
            try:
                conn.execute(f"CREATE TABLE {quote_ident(placeholder)} ({body})")
            except duckdb.Error as exc:
                return failure("prepare", f"Could not prepare placeholder {placeholder!r} for the query: {message(exc)}")
        relation, refused = bind(conn, job["sql"])
        return refused or {"ok": True, "columns": columns_of(relation)}


def run(job: dict) -> dict:
    with duckdb.connect(job["path"], config=SANDBOX_SETTINGS) as conn:
        relation, refused = bind(conn, job["sql"])
        if refused:
            return refused
        columns = columns_of(relation)
        try:
            relation.create(job["result"])
        except duckdb.Error as exc:
            return failure("execution", message(exc))
        return {"ok": True, "columns": columns}


def main() -> None:
    job = json.loads(sys.stdin.read())
    reply = {"describe": describe, "run": run}[job["op"]](job)
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    main()
