"""The sandbox a raw_query statement is parsed, described and run in (MADR 0002).

Agent-written SQL never runs on the engine's connection. Each statement gets a
DuckDB database of its own that holds exactly one table per placeholder,
copied from the bound dataset version and named after the placeholder, and
nothing else. It is opened with external access disabled, extension loading
and installation off and the configuration locked, so no statement can read a
file, reach the network, attach a database, load code or change a setting,
whatever the rules in core/ir/raw_query.py missed. Because the sandbox holds no
storage table, the statement cannot name one, and the errors DuckDB raises
there speak of placeholders only.

The statement's result is written into the sandbox database, which the engine
then attaches read-only so the steps after the query and the materialization
read it through the normal path. The database lives in a temporary directory
inside the workspace and is removed with it.

The server's deadline, when it sets one, guards every step that binds or runs
a statement. DuckDB does not check for interrupts while it binds (it evaluates
a COLUMNS lambda and folds constants there), so the guard keeps interrupting
until the work ends and raises as soon as a binding that ignored the interrupt
returns: nothing runs after the deadline has passed, but a single binding can
outlast it. Bounding that too would mean running each step in a process of its
own, which is not worth a process per query for an expression shape no
ordinary statement has.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator

import duckdb

from ...core.errors import ExecutionFailedError, InvalidTransformError
from ...core.ir.raw_query import FunctionReference, QueryShape, TableReference
from ...core.models.entities import LogicalType
from .engine import DuckDBEngine, _literal, physical_to_logical, quote_ident

SANDBOX_SETTINGS = {
    "enable_external_access": False,
    "autoload_known_extensions": False,
    "autoinstall_known_extensions": False,
    "allow_community_extensions": False,
    "lock_configuration": True,
}
# Placeholders start with a letter, so neither name can collide with one.
RESULT_TABLE = "_result"
ATTACHED_AS = "_raw_query"

_CREATE_AS_RE = re.compile(
    r"^\s*create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?(?:table|view)\s+(?:if\s+not\s+exists\s+)?"
    r"(?P<name>\"[^\"]+\"|[^\s(]+)\s+as\s+(?P<query>.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _connect(path: Path | None = None) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path) if path is not None else ":memory:", config=SANDBOX_SETTINGS)


def _message(exc: Exception) -> str:
    """DuckDB's message without the echo of the statement it appends."""
    text = str(exc).split("\n\nLINE ", 1)[0]
    return " ".join(text.split())


def _expired(timeout: float) -> ExecutionFailedError:
    return ExecutionFailedError(
        f"The query ran longer than the server's {timeout:g}-second deadline for a raw_query and was stopped.",
        recoverable=True,
        details={"raw_query": {"refused": "deadline", "timeout_seconds": timeout}},
    )


class _Deadline:
    """Interrupts a connection once the deadline passes, and keeps interrupting until the work ends.

    ``check`` raises once it has passed, which is how a binding that ignored
    the interrupts is stopped when it returns, before anything runs.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, timeout: float | None) -> None:
        self._conn, self._timeout, self._done = conn, timeout, threading.Event()
        self.passed = False
        self._thread = threading.Thread(target=self._watch, daemon=True) if timeout else None

    def _watch(self) -> None:
        assert self._timeout is not None
        if self._done.wait(self._timeout):
            return
        self.passed = True
        while not self._done.wait(0.05):
            self._conn.interrupt()

    def check(self) -> None:
        if self.passed:
            assert self._timeout is not None
            raise _expired(self._timeout)

    def __enter__(self) -> "_Deadline":
        if self._thread is not None:
            self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=1)


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
        _order_keys(tree.get("statements"), shape, sql)
        return shape

    def describe_query(self, sql: str, relations: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str, LogicalType]]:
        """The statement's output columns, bound against empty tables with the placeholders' schemas."""
        with closing(_connect()) as conn:
            for placeholder, columns in relations.items():
                body = ", ".join(f"{quote_ident(name)} {physical}" for name, physical in columns)
                try:
                    conn.execute(f"CREATE TABLE {quote_ident(placeholder)} ({body})")
                except duckdb.Error as exc:
                    raise ExecutionFailedError(
                        f"Could not prepare placeholder {placeholder!r} for the query: {_message(exc)}") from None
            with _Deadline(conn, self.timeout) as deadline:
                described = _describe(conn, sql)
                deadline.check()
            return described

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
        self._conn: duckdb.DuckDBPyConnection | None = _connect(path)
        self._attached = False

    def describe(self, sql: str) -> list[tuple[str, str, LogicalType]]:
        assert self._conn is not None
        with _Deadline(self._conn, self._timeout) as deadline:
            described = _describe(self._conn, sql)
            deadline.check()
        return described

    def run(self, sql: str) -> str:
        """Run the statement into the sandbox; return the relation the engine reads its result from."""
        conn = self._conn
        assert conn is not None
        try:
            with _Deadline(conn, self._timeout) as deadline:
                relation = conn.sql(sql)
                # A deadline that passed while the statement bound stops here, before anything runs.
                deadline.check()
                if relation is None:
                    raise ExecutionFailedError("The statement is not a query and returned no rows.",
                                               details={"raw_query": {"refused": "execution"}})
                relation.create(RESULT_TABLE)
        except duckdb.InterruptException:
            assert self._timeout is not None
            raise _expired(self._timeout) from None
        except duckdb.Error as exc:
            raise ExecutionFailedError(
                f"The query failed while running: {_message(exc)}",
                recoverable=True,
                details={"raw_query": {"refused": "execution", "message": _message(exc)}},
            ) from None
        conn.close()
        self._conn = None
        self._engine.conn.execute(f"ATTACH {_literal(str(self._path))} AS {quote_ident(ATTACHED_AS)} (READ_ONLY)")
        self._attached = True
        return f"{quote_ident(ATTACHED_AS)}.{quote_ident(RESULT_TABLE)}"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._attached:
            self._engine.conn.execute(f"DETACH DATABASE IF EXISTS {quote_ident(ATTACHED_AS)}")
            self._attached = False


def _describe(conn: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[str, str, LogicalType]]:
    try:
        relation = conn.sql(sql)
    except duckdb.Error as exc:
        raise InvalidTransformError(f"The query does not bind: {_message(exc)}",
                                    details={"raw_query": {"refused": "binding", "message": _message(exc)}}) from None
    if relation is None:
        raise InvalidTransformError("The statement returns no rows to describe.",
                                    details={"raw_query": {"refused": "binding"}})
    return [(str(name), str(kind), physical_to_logical(str(kind))) for name, kind in zip(relation.columns, relation.types)]


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


def _from_tables(node: Any, ctes: set[str]) -> list[tuple[str, str]] | None:
    """The tables a FROM clause reads, as (name, alias); None when it reads anything else (a subquery, a CTE, a
    table function), where a column name need not mean the input column of that name."""
    if not isinstance(node, dict):
        return None
    if node.get("type") == "BASE_TABLE":
        name = str(node.get("table_name") or "")
        if node.get("schema_name") or node.get("catalog_name") or name.casefold() in ctes:
            return None
        return [(name, str(node.get("alias") or ""))]
    if node.get("type") == "JOIN":
        left, right = _from_tables(node.get("left"), ctes), _from_tables(node.get("right"), ctes)
        return None if left is None or right is None else left + right
    return None


def _character_offset(sql: str, location: int) -> int:
    """The offset into ``sql`` of a parser location, which DuckDB counts in UTF-8 bytes."""
    return len(sql.encode("utf-8")[:location].decode("utf-8", errors="ignore"))


def _order_keys(statements: Any, shape: QueryShape, sql: str) -> None:
    """The column references the outermost ORDER BY of a plain SELECT over tables sorts by, with their offsets.

    A bare name that the select list gives to a column (``a AS x ... ORDER BY x``) stands for that column; one it
    gives to anything else names the select item, not a column, and is left out.
    """
    node = statements[0].get("node") if isinstance(statements, list) and statements and isinstance(statements[0], dict) else None
    if not isinstance(node, dict) or node.get("type") != "SELECT_NODE":
        return
    tables = _from_tables(node.get("from_table"), shape.ctes)
    if not tables:
        return
    aliases: dict[str, tuple[str, ...] | None] = {}
    for item in node.get("select_list") or []:
        if isinstance(item, dict) and item.get("alias"):
            names = item.get("column_names") if item.get("class") == "COLUMN_REF" else None
            aliases[str(item["alias"]).casefold()] = tuple(str(n) for n in names) if names else None
    keys: list[tuple[tuple[str, ...], int]] = []
    for modifier in node.get("modifiers") or []:
        if not isinstance(modifier, dict) or modifier.get("type") != "ORDER_MODIFIER":
            continue
        for order in modifier.get("orders") or []:
            expression = order.get("expression") if isinstance(order, dict) else None
            if not isinstance(expression, dict) or expression.get("class") != "COLUMN_REF":
                continue
            names = tuple(str(n) for n in expression.get("column_names") or [])
            location = expression.get("query_location")
            if not names or not isinstance(location, int):
                continue
            if len(names) == 1 and names[0].casefold() in aliases:
                target = aliases[names[0].casefold()]
                if target is None:
                    continue
                if target[-1].casefold() != names[0].casefold():
                    names = target
            keys.append((names, _character_offset(sql, location)))
    shape.order_by, shape.order_from = keys, tables


def _first_literal(children: Any) -> str | None:
    if not isinstance(children, list) or not children or not isinstance(children[0], dict):
        return None
    value = children[0].get("value")
    if children[0].get("class") == "CONSTANT" and isinstance(value, dict) and isinstance(value.get("value"), str):
        return value["value"]
    return None
