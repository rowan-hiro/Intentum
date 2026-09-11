"""The sandbox a raw_query statement is parsed, described and run in (MADR 0002).

Agent-written SQL never runs on the engine's connection, nor in the server's
process. Parsing, which evaluates nothing, happens here. Every step that binds
the statement (the describe during resolution and the re-check during
validation) or runs it happens in a process of its own, sandbox_worker.py, on
a DuckDB database that holds exactly one table per placeholder, copied from
the bound dataset version and named after the placeholder, and nothing else.
That database is opened with external access disabled, extension loading and
installation off and the configuration locked, so no statement can read a
file, reach the network, attach a database, load code or change a setting,
whatever the rules in core/ir/raw_query.py missed. Because it holds no storage
table, the statement cannot name one, and the errors DuckDB raises there speak
of placeholders only.

The server's deadline, when it sets one, ends the step's process. DuckDB
evaluates some expressions while binding without checking for interrupts, so
an interrupt could arrive while a statement binds and be lost before it runs;
ending the process bounds both phases and keeps the statement's memory out of
the server's. Each step costs a process start (about a tenth of a second).

The statement's result is written into the sandbox database, which the engine
then attaches read-only so the steps after the query and the materialization
read it through the normal path. The database lives in a temporary directory
inside the workspace and is removed with it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator

import duckdb

from ...core.errors import ExecutionFailedError, InvalidTransformError
from ...core.ir.raw_query import FunctionReference, QueryShape, TableReference
from ...core.models.entities import LogicalType
from .engine import DuckDBEngine, _literal, physical_to_logical, quote_ident
from .sandbox_worker import SANDBOX_SETTINGS

WORKER = Path(__file__).with_name("sandbox_worker.py")
# Placeholders start with a letter, so neither name can collide with one.
RESULT_TABLE = "_result"
ATTACHED_AS = "_raw_query"

_CREATE_AS_RE = re.compile(
    r"^\s*create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?(?:table|view)\s+(?:if\s+not\s+exists\s+)?"
    r"(?P<name>\"[^\"]+\"|[^\s(]+)\s+as\s+(?P<query>.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _connect() -> duckdb.DuckDBPyConnection:
    """An empty locked connection for parsing, which binds and evaluates nothing."""
    return duckdb.connect(":memory:", config=SANDBOX_SETTINGS)


def _message(exc: Exception) -> str:
    """DuckDB's message without the echo of the statement it appends."""
    text = str(exc).split("\n\nLINE ", 1)[0]
    return " ".join(text.split())


class QuerySandbox:
    """Parses, describes and runs raw_query statements for a DuckDB engine.

    ``timeout`` is the server's deadline in seconds for running one statement;
    None runs without one.
    """

    def __init__(self, engine: DuckDBEngine, *, timeout: float | None = None) -> None:
        self.engine = engine
        self.timeout = timeout

    # -- parsing ---------------------------------------------------------
    def inspect_query(self, sql: str) -> QueryShape:
        """What DuckDB's parser reads in ``sql``; nothing is bound or run."""
        command, dollars = _leading_keyword(sql), _dollar_names(sql)
        with closing(_connect()) as conn:
            try:
                statements = conn.extract_statements(sql)
            except duckdb.Error as exc:
                return QueryShape(statements=[], command=command, error=_message(exc),
                                  position=_error_position(conn, sql), dollar_names=dollars)
            shape = QueryShape(statements=[s.type.name for s in statements], command=command, dollar_names=dollars)
            if shape.statements == ["CREATE"]:
                shape.create_as = _create_as(conn, sql)
            if shape.statements != ["SELECT"]:
                return shape
            shape.parameters = sorted(str(p) for p in statements[0].named_parameters)
            tree = json.loads(conn.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
        if tree.get("error"):
            shape.error = str(tree.get("error_message") or "the statement could not be read")
            return shape
        _walk(tree.get("statements"), shape)
        return shape

    def describe_query(self, sql: str, relations: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str, LogicalType]]:
        """The statement's output columns, bound against empty tables with the placeholders' schemas."""
        job = {"op": "describe", "relations": {p: [list(c) for c in columns] for p, columns in relations.items()},
               "sql": sql}
        return _described(_step(job, self.timeout))

    # -- running ---------------------------------------------------------
    @contextmanager
    def session(self, inputs: dict[str, str]) -> Iterator["QuerySession"]:
        """A sandbox holding a copy of each physical table under its placeholder; removed on exit."""
        with TemporaryDirectory(prefix=".raw-query-", dir=self.engine.path.parent) as directory:
            path = Path(directory) / "query.duckdb"
            self._copy_inputs(path, inputs)
            session = QuerySession(self.engine, path, self.timeout)
            try:
                yield session
            finally:
                session.close()

    def _copy_inputs(self, path: Path, inputs: dict[str, str]) -> None:
        conn = self.engine.conn
        conn.execute(f"ATTACH {_literal(str(path))} AS {quote_ident(ATTACHED_AS)}")
        try:
            for placeholder, table in inputs.items():
                conn.execute(f"CREATE TABLE {quote_ident(ATTACHED_AS)}.{quote_ident(placeholder)} AS "
                             f"SELECT * FROM {quote_ident(table)}")
        except duckdb.Error:
            # DuckDB's message would name the storage table; the placeholder is enough to act on.
            raise ExecutionFailedError(
                f"Could not copy the inputs {', '.join(inputs)} into the query's sandbox.") from None
        finally:
            conn.execute(f"DETACH DATABASE IF EXISTS {quote_ident(ATTACHED_AS)}")


class QuerySession:
    """One statement's sandbox, filled with its inputs."""

    def __init__(self, engine: DuckDBEngine, path: Path, timeout: float | None) -> None:
        self._engine = engine
        self._path = path
        self._timeout = timeout
        self._attached = False

    def describe(self, sql: str) -> list[tuple[str, str, LogicalType]]:
        """The statement's output columns over the real inputs, bound in the step's own process."""
        return _described(_step({"op": "describe", "path": str(self._path), "sql": sql}, self._timeout))

    def run(self, sql: str) -> tuple[str, list[tuple[str, str, LogicalType]]]:
        """Bind and run the statement into the sandbox, in one process under the deadline.

        Returns the relation the engine reads the result from and the columns the statement returned.
        """
        reply = _step({"op": "run", "path": str(self._path), "sql": sql, "result": RESULT_TABLE}, self._timeout)
        if not reply.get("ok"):
            raise ExecutionFailedError(
                f"The query failed while running: {reply.get('message')}",
                recoverable=True,
                details={"raw_query": {"refused": "execution", "message": reply.get("message")}},
            )
        described = _described(reply)
        self._engine.conn.execute(f"ATTACH {_literal(str(self._path))} AS {quote_ident(ATTACHED_AS)} (READ_ONLY)")
        self._attached = True
        return f"{quote_ident(ATTACHED_AS)}.{quote_ident(RESULT_TABLE)}", described

    def close(self) -> None:
        if self._attached:
            self._engine.conn.execute(f"DETACH DATABASE IF EXISTS {quote_ident(ATTACHED_AS)}")
            self._attached = False


def _step(job: dict[str, Any], timeout: float | None) -> dict[str, Any]:
    """Run one sandbox step in its own process; the deadline, when set, ends the process."""
    try:
        done = subprocess.run([sys.executable, "-I", str(WORKER)], input=json.dumps(job), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        phase = "was being run" if job["op"] == "run" else "was being bound"
        raise ExecutionFailedError(
            f"The query {phase} longer than the server's {timeout:g}-second deadline for a raw_query step and "
            "was stopped.",
            recoverable=True,
            details={"raw_query": {"refused": "deadline", "timeout_seconds": timeout}},
        ) from None
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        raise ExecutionFailedError(
            f"The query's sandbox process ended without a result (exit status {done.returncode}); the statement may "
            "have needed more memory than the host has.",
            recoverable=True,
            details={"raw_query": {"refused": "execution", "exit_status": done.returncode}},
        ) from None
    if not reply.get("ok") and reply.get("phase") == "prepare":
        raise ExecutionFailedError(str(reply.get("message")))
    return reply


def _described(reply: dict[str, Any]) -> list[tuple[str, str, LogicalType]]:
    """The columns a successful step reports, or the binding refusal it reports instead."""
    if not reply.get("ok"):
        text = str(reply.get("message"))
        raise InvalidTransformError(f"The query does not bind: {text}",
                                    details={"raw_query": {"refused": "binding", "message": text}})
    return [(str(name), str(kind), physical_to_logical(str(kind))) for name, kind in reply["columns"]]


# -- reading the parser's output ------------------------------------------------

def _tokens(sql: str) -> list[tuple[int, Any]]:
    try:
        return list(duckdb.tokenize(sql))
    except duckdb.Error:
        return []


def _leading_keyword(sql: str) -> str:
    """The first word of the statement, past comments (SELECT, WITH, PRAGMA, ...)."""
    for offset, _kind in _tokens(sql):
        match = re.match(r"[^\W\d]\w*", sql[offset:])
        return match.group(0).upper() if match else ""
    return ""


def _dollar_names(sql: str) -> list[tuple[int, str]]:
    """``$name`` written as one token pair, outside strings and comments: (offset of ``$``, name)."""
    tokens = _tokens(sql)
    found: list[tuple[int, str]] = []
    for (offset, kind), following in zip(tokens, tokens[1:]):
        if kind != duckdb.token_type.operator or not sql.startswith("$", offset) or following[0] != offset + 1:
            continue
        match = re.match(r"[^\W\d]\w*", sql[offset + 1:])
        if match:
            found.append((offset, match.group(0)))
    return found


def _error_position(conn: duckdb.DuckDBPyConnection, sql: str) -> int | None:
    try:
        error = json.loads(conn.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    except (duckdb.Error, ValueError, TypeError):
        return None
    position = str(error.get("position", ""))
    return int(position) if position.isdigit() else None


def _create_as(conn: duckdb.DuckDBPyConnection, sql: str) -> tuple[str, str] | None:
    """``CREATE TABLE name AS <select>``: the name and the select, when the select parses on its own."""
    match = _CREATE_AS_RE.match(sql)
    if match is None:
        return None
    query = match.group("query")
    try:
        statements = conn.extract_statements(query)
    except duckdb.Error:
        return None
    if [s.type.name for s in statements] != ["SELECT"]:
        return None
    return match.group("name").strip('"').rsplit(".", 1)[-1], query.strip()


def _walk(node: Any, shape: QueryShape, visible: frozenset[str] = frozenset()) -> None:
    """Collect table references, table functions, CTE names and parameters from the serialized statement.

    ``visible`` holds the CTE names a bare table reference at this point binds
    to, following DuckDB's scoping: a WITH clause's names are visible in its
    main query and, one by one, in the CTEs defined after them, and a CTE's own
    name is visible in its body only when that body is recursive. Anywhere else
    a same-named reference reads the catalog, so it is judged as a table.
    """
    if isinstance(node, list):
        for item in node:
            _walk(item, shape, visible)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind == "RECURSIVE_CTE_NODE" and isinstance(node.get("cte_name"), str):
        visible = visible | {node["cte_name"].casefold()}
    cte_map = node.get("cte_map")
    entries = cte_map.get("map") if isinstance(cte_map, dict) else None
    if entries:
        defined: set[str] = set()
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                _walk(entry.get("value"), shape, visible | defined)
                defined.add(entry["key"].casefold())
        shape.ctes |= defined
        visible = visible | defined
    if kind == "BASE_TABLE":
        table = TableReference(name=str(node.get("table_name") or ""), schema=str(node.get("schema_name") or ""),
                               catalog=str(node.get("catalog_name") or ""))
        if not table.qualified and table.name.casefold() in visible:
            table = TableReference(table.name, table.schema, table.catalog, cte=True)
        shape.tables.append(table)
    elif kind == "TABLE_FUNCTION":
        function = node.get("function") or {}
        shape.functions.append(FunctionReference(name=str(function.get("function_name") or ""),
                                                 schema=str(function.get("schema") or ""),
                                                 argument=_first_literal(function.get("children"))))
    elif kind == "SHOW_REF":
        shape.describes = True
    if node.get("class") == "PARAMETER":
        identifier = str(node.get("identifier"))
        if identifier not in shape.parameters:
            shape.parameters.append(identifier)
    for key, value in node.items():
        if key != "cte_map" and isinstance(value, (dict, list)):
            _walk(value, shape, visible)


def _first_literal(children: Any) -> str | None:
    if not isinstance(children, list) or not children or not isinstance(children[0], dict):
        return None
    value = children[0].get("value")
    if children[0].get("class") == "CONSTANT" and isinstance(value, dict) and isinstance(value.get("value"), str):
        return value["value"]
    return None
