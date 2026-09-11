"""Rules for the raw_query step, shared by the resolver (inference) and the validator (re-check).

The semantic steps are the primary language (MADR 0002). A raw_query step is
the fallback for shapes they cannot express, such as a window over groups, a
tie-aware extremum or a union: one read-only SQL statement that reads its
inputs only through placeholders, each bound to a concrete dataset version.

Placeholders are table names in a namespace the backend controls. ``input`` is
the transform's source; every other name is declared under the step's
``inputs`` with a dataset reference. The statement is never rewritten to
substitute them: DuckDB's parser finds the table references, and the sandbox
the statement runs in holds exactly one table per placeholder and nothing
else. A placeholder therefore cannot collide with DuckDB's own ``$name`` or
``?`` parameters (which are refused), and a placeholder name inside a string
literal or a comment is never read as a table.

The rules judge a ``QueryShape``, the facts the storage layer's parser reads
from the statement, and the columns its ``DESCRIBE`` returns. What the rules
refuse carries the refused class and the facts behind it under
``details["raw_query"]``, so core/recovery can name the accepted shape
(MADR 0010).
"""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..errors import BackendError, InvalidTransformError
from ..models.entities import LogicalType
from ..naming import is_identifier, slugify
from .canonical import FieldRef

DEFAULT_PLACEHOLDER = "input"
# Table functions that compute rows from their arguments alone; every other input is a placeholder.
TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
# A placeholder starts with a letter (any script), so no placeholder can take a name the sandbox reserves.
PLACEHOLDER_RE = re.compile(r"^[^\W\d_]\w*$")
# The storage names the backend allocates for dataset versions (ds_N_vM); a query never sees one.
PHYSICAL_TABLE_RE = re.compile(r"^ds_\d+_v\d+$", re.IGNORECASE)

# Table functions that read files or the network; only the wording of their refusal differs from the rest.
_FILE_FUNCTION_RE = re.compile(r"^(read_\w+|\w+_scan|parquet_\w+|sniff_csv|glob|iceberg_\w+|delta_\w+|http_\w+)$",
                               re.IGNORECASE)
_FILE_SUFFIXES = (".csv", ".tsv", ".txt", ".parquet", ".json", ".jsonl", ".ndjson", ".db", ".sqlite",
                  ".sqlite3", ".duckdb", ".xlsx", ".gz", ".zst", ".arrow")
# What each statement type does that a read-only query must not, keyed by DuckDB's statement type or,
# for statements DuckDB turns into a SELECT (PRAGMA, DESCRIBE), by the leading keyword.
_COMMANDS = {
    "PRAGMA": "PRAGMA", "DESCRIBE": "DESCRIBE", "DESC": "DESCRIBE", "SHOW": "DESCRIBE", "SUMMARIZE": "DESCRIBE",
    "INSTALL": "INSTALL", "USE": "SET", "CHECKPOINT": "CALL", "EXPLAIN": "EXPLAIN",
}
_STATEMENTS = {
    "CREATE": "DDL", "DROP": "DDL", "ALTER": "DDL", "CREATE_FUNC": "DDL",
    "INSERT": "DML", "UPDATE": "DML", "DELETE": "DML", "MERGE_INTO": "DML",
    "COPY": "COPY", "COPY_DATABASE": "COPY", "EXPORT": "COPY",
    "ATTACH": "ATTACH", "DETACH": "ATTACH",
    "LOAD": "LOAD", "EXTENSION": "LOAD",
    "PRAGMA": "PRAGMA", "SET": "SET", "VARIABLE_SET": "SET", "CALL": "CALL",
    "EXPLAIN": "EXPLAIN", "TRANSACTION": "TRANSACTION", "PREPARE": "PREPARE", "EXECUTE": "PREPARE",
    "VACUUM": "MAINTENANCE", "ANALYZE": "MAINTENANCE",
}
_WHAT_IT_DOES = {
    "DDL": "creates, changes or drops objects",
    "DML": "writes rows",
    "COPY": "reads or writes files",
    "ATTACH": "opens another database",
    "LOAD": "loads extension code",
    "INSTALL": "installs extension code",
    "PRAGMA": "reads or changes engine settings",
    "SET": "changes settings",
    "CALL": "runs a procedure",
    "DESCRIBE": "describes a relation instead of querying it",
    "EXPLAIN": "explains a plan instead of returning rows",
    "TRANSACTION": "controls a transaction",
    "PREPARE": "prepares or runs another statement",
    "MAINTENANCE": "maintains the database",
}


@dataclass(frozen=True)
class TableReference:
    name: str
    schema: str = ""
    catalog: str = ""
    cte: bool = False  # binds to a CTE in scope where it is written, under DuckDB's CTE scoping

    @property
    def qualified(self) -> bool:
        return bool(self.schema or self.catalog)

    def text(self) -> str:
        return ".".join(part for part in (self.catalog, self.schema, self.name) if part)


@dataclass(frozen=True)
class FunctionReference:
    name: str
    schema: str = ""
    argument: str | None = None  # the first literal argument, such as a file path


@dataclass
class QueryShape:
    """What DuckDB's parser reads in a statement: the facts the rules below judge."""

    statements: list[str]  # statement types in order, as DuckDB names them (SELECT, CREATE, ...)
    command: str = ""  # the leading keyword as written (SELECT, WITH, PRAGMA, ...)
    error: str | None = None  # the parser's message when the text does not parse
    position: int | None = None
    tables: list[TableReference] = field(default_factory=list)
    functions: list[FunctionReference] = field(default_factory=list)
    ctes: set[str] = field(default_factory=set)  # every CTE name the statement defines, casefolded
    parameters: list[str] = field(default_factory=list)
    describes: bool = False  # DESCRIBE / SHOW / SUMMARIZE used as a relation
    dollar_names: list[tuple[int, str]] = field(default_factory=list)  # `$name` tokens: (offset of `$`, name)
    create_as: tuple[str, str] | None = None  # CREATE TABLE|VIEW name AS <select>: (name, select)


class QuerySession(Protocol):
    def describe(self, sql: str) -> list[tuple[str, str, LogicalType]]: ...
    def run(self, sql: str) -> tuple[str, list[tuple[str, str, LogicalType]]]: ...


class QueryEngine(Protocol):
    """What the rules need from the engine: a parser, DESCRIBE over placeholder schemas, and a sandbox."""

    def inspect_query(self, sql: str) -> QueryShape: ...
    def describe_query(self, sql: str, relations: dict[str, list[tuple[str, str]]]) -> list[tuple[str, str, LogicalType]]: ...
    def session(self, inputs: dict[str, str]) -> AbstractContextManager[QuerySession]: ...


@dataclass(frozen=True)
class QueryBinding:
    """One placeholder and the dataset version it stands for."""

    placeholder: str
    dataset_id: str
    dataset: str
    columns: list[tuple[str, str]]  # (name, physical type)


@dataclass
class QueryAnalysis:
    columns: list[str]  # the names the statement returns, in order
    fields: list[FieldRef]  # what those columns become in the backend's type system
    used: list[str]  # the placeholders the statement reads
    renamed: list[tuple[str, str]]  # (name returned, field name) where the name was normalized


def refusal(text: str, where: str, refused: str, **facts) -> InvalidTransformError:
    """A raw_query refusal: the refused class and its facts travel in details for core/recovery."""
    return InvalidTransformError(text, field=where, details={"raw_query": {"refused": refused, **facts}})


def check_placeholder(name: object, where: str, *, source: bool = False) -> str:
    """A placeholder name as declared under inputs; ``input`` is the source's and nobody else's."""
    text = str(name).strip() if isinstance(name, str) else ""
    if not PLACEHOLDER_RE.fullmatch(text) or PHYSICAL_TABLE_RE.fullmatch(text):
        raise refusal(
            f"{name!r} cannot be a placeholder: a placeholder is a table name in the SQL that starts with a letter "
            "and holds letters, digits and underscores, and it does not look like a storage name.",
            where, "placeholder_name", name=name if isinstance(name, str) else repr(name),
        )
    if (text.casefold() == DEFAULT_PLACEHOLDER) != source:
        raise refusal(
            f"{DEFAULT_PLACEHOLDER!r} is the placeholder of the transform's source; bind other datasets under other names.",
            where, "placeholder_name", name=text,
        )
    return text


def analyze_query(
    sql: str,
    bindings: list[QueryBinding],
    engine: QueryEngine,
    *,
    where: str,
    dataset_named: Callable[[str], str | None] | None = None,
) -> QueryAnalysis:
    """Check one raw_query statement against the rules and derive its output with DESCRIBE.

    ``bindings[0]`` is the source under ``input``. ``dataset_named`` maps a
    table name the statement reads to the dataset it exactly names, so a
    refusal can offer to bind it; the validator re-checks without it.
    """
    placeholders = {b.placeholder.casefold(): b for b in bindings}
    bound = {b.placeholder: b.dataset for b in bindings}
    if not isinstance(sql, str) or not sql.strip():
        raise refusal("raw_query needs one SELECT statement as its SQL; it was empty.", where, "empty", placeholders=bound)
    shape = engine.inspect_query(sql)
    # A placeholder written as `$input` is the one mechanical repair of a statement's text; it is offered only
    # when the statement without the `$` parses.
    dollars = [offset for offset, name in shape.dollar_names if name.casefold() in placeholders]
    suggested = _without_dollars(sql, dollars) if dollars else None
    if suggested is not None and engine.inspect_query(suggested).error is not None:
        suggested = None

    if shape.error is not None and not shape.statements:
        facts = {"message": shape.error, "position": shape.position, "placeholders": bound}
        if suggested:
            facts["suggested_sql"] = suggested
        raise refusal(f"The raw_query SQL does not parse: {shape.error}", where, "parse", **facts)
    if not shape.statements:
        raise refusal("raw_query needs one SELECT statement as its SQL; it holds none.", where, "empty", placeholders=bound)
    if len(shape.statements) > 1:
        raise refusal(
            f"raw_query takes exactly one statement; this SQL holds {len(shape.statements)} "
            f"({', '.join(shape.statements)}).",
            where, "statements", statements=shape.statements, command=shape.command, placeholders=bound,
        )
    kind = _COMMANDS.get(shape.command) or _STATEMENTS.get(shape.statements[0])
    if kind is None and shape.statements[0] != "SELECT":
        kind = "OTHER"
    if kind is None and shape.describes:
        kind = "DESCRIBE"
    if kind is not None:
        facts = {"statement": shape.statements[0], "command": shape.command, "category": kind, "placeholders": bound}
        if shape.create_as is not None:
            facts["create_as"] = {"name": shape.create_as[0], "sql": shape.create_as[1]}
        raise refusal(
            f"raw_query runs one read-only query (SELECT or WITH); this {shape.command or shape.statements[0]} "
            f"statement {_WHAT_IT_DOES.get(kind, 'is not a query')}.",
            where, "statement", **facts,
        )
    if shape.error is not None:
        # A SELECT that parses but that the parser cannot serialize for inspection is not run unread.
        raise refusal(f"The raw_query SQL could not be inspected: {shape.error}", where, "parse",
                      message=shape.error, placeholders=bound)
    if shape.parameters:
        facts = {"parameters": shape.parameters, "placeholders": bound}
        if suggested:
            facts["suggested_sql"] = suggested
        raise refusal(
            "raw_query takes no parameters ($name, $1 or ?): write each value into the SQL as a literal. "
            "A placeholder is a bare table name, such as input.",
            where, "parameter", **facts,
        )

    for function in shape.functions:
        name = function.name.casefold()
        if not function.schema and name in TABLE_FUNCTIONS:
            continue
        if _FILE_FUNCTION_RE.match(name):
            raise refusal(
                f"{function.name}() reads files or the network; a raw_query reads only the datasets bound to its "
                "placeholders.",
                where, "file_function", function=function.name, argument=function.argument, placeholders=bound,
            )
        raise refusal(
            f"{function.name}() is not a table function raw_query accepts; it accepts "
            f"{', '.join(sorted(TABLE_FUNCTIONS))}, and every other input is a placeholder.",
            where, "table_function", function=function.name, allowed=sorted(TABLE_FUNCTIONS), placeholders=bound,
        )

    shadowed = sorted(name for name in shape.ctes if name in placeholders)
    if shadowed:
        raise refusal(
            f"The WITH clause defines {', '.join(repr(n) for n in shadowed)}, the name of a placeholder, which hides "
            "the dataset bound to it; give the CTE another name.",
            where, "cte_shadows_placeholder", names=shadowed, placeholders=bound,
        )
    used: list[str] = []
    unknown: list[str] = []
    for table in shape.tables:
        key = table.name.casefold()
        if table.cte:
            continue
        if _looks_like_file(table.name):
            raise refusal(
                f"FROM {table.text()!r} reads a file directly; a raw_query reads only the datasets bound to its "
                "placeholders.",
                where, "replacement_scan", reference=table.text(), placeholders=bound,
            )
        if PHYSICAL_TABLE_RE.fullmatch(table.name):
            # The name is the agent's own text, but whether it exists is not confirmed.
            raise refusal(
                "The SQL names a storage table of the backend; a query never sees storage names, only its "
                f"placeholders ({', '.join(bound)}).",
                where, "physical_table", placeholders=bound,
            )
        if table.qualified:
            raise refusal(
                f"{table.text()!r} is qualified with a schema or database; a placeholder is a bare name "
                f"({', '.join(bound)}).",
                where, "qualified_table", reference=table.text(), placeholders=bound,
            )
        if key in placeholders:
            if placeholders[key].placeholder not in used:
                used.append(placeholders[key].placeholder)
        elif table.name not in unknown:
            unknown.append(table.name)
    if unknown:
        matches = {name: found for name in unknown if dataset_named and (found := dataset_named(name))}
        listed = ", ".join(f"{p} ({d})" for p, d in bound.items())
        raise refusal(
            f"The SQL reads {', '.join(repr(n) for n in unknown)}, which "
            f"{'is not a placeholder' if len(unknown) == 1 else 'are not placeholders'} here; the placeholders are "
            f"{listed}. Bind a dataset under inputs to read it by name.",
            where, "unknown_placeholder", names=unknown, placeholders=bound, datasets=matches,
        )
    unused = [b.placeholder for b in bindings[1:] if b.placeholder not in used]
    if unused:
        raise refusal(
            f"inputs binds {', '.join(repr(n) for n in unused)} but the SQL never reads "
            f"{'it' if len(unused) == 1 else 'them'}; every bound dataset becomes an upstream of the result, so bind "
            "only what the query reads.",
            where, "unused_input", names=unused, placeholders=bound,
        )
    source = bindings[0]
    if source.placeholder not in used and not any(placeholders[p.casefold()].dataset_id == source.dataset_id for p in used):
        raise refusal(
            f"The SQL never reads {DEFAULT_PLACEHOLDER}, the transform's source ({source.dataset}); a raw_query must "
            "read its source, which becomes the result's upstream.",
            where, "source_unused", used=used, placeholders=bound,
        )

    try:
        described = engine.describe_query(sql, {b.placeholder: b.columns for b in bindings})
    except BackendError as err:
        facts = dict(err.details.get("raw_query") or {})
        if facts.get("refused") == "deadline":
            raise  # binding outlasted the server's deadline: an execution failure, not a refused shape
        refused = str(facts.pop("refused", "binding"))
        facts["columns"] = {b.placeholder: [name for name, _ in b.columns] for b in bindings}
        facts["placeholders"] = bound
        raise refusal(err.message, where, refused, **facts) from None
    return _output(described, bindings, where, used)


def _output(described: list[tuple[str, str, LogicalType]], bindings: list[QueryBinding], where: str,
            used: list[str]) -> QueryAnalysis:
    """The statement's columns in the backend's type system, under names the rest of the language can use.

    A column keeps its name when it is an identifier or passes an input
    column through under its own name; anything else, such as ``max(amount)``,
    is normalized to snake_case and reported, as derive and rename names are.
    """
    unsupported = [{"name": name, "type": physical} for name, physical, logical in described
                   if logical == LogicalType.UNKNOWN]
    if unsupported:
        raise refusal(
            "The query returns " + ", ".join(f"{c['name']} ({c['type']})" for c in unsupported)
            + ", outside the backend's types (integer, float, boolean, string, date, timestamp); cast "
            + ("it" if len(unsupported) == 1 else "them") + " in the SQL or leave "
            + ("it" if len(unsupported) == 1 else "them") + " out.",
            where, "output_type", columns=unsupported,
        )
    passthrough = {name for b in bindings for name, _ in b.columns}
    columns: list[str] = []
    fields: list[FieldRef] = []
    renamed: list[tuple[str, str]] = []
    for name, _physical, logical in described:
        canonical = name
        if name not in passthrough and not is_identifier(name):
            try:
                canonical = slugify(name)
            except BackendError:
                raise refusal(f"The query returns a column named {name!r}; name it with AS.", where, "output_name",
                              names=[name]) from None
            renamed.append((name, canonical))
        columns.append(name)
        fields.append(FieldRef(name=canonical, logical_type=logical))
    seen: dict[str, str] = {}
    for name, f in zip(columns, fields):
        key = f.name.casefold()
        if key in seen:
            raise refusal(
                f"The query returns the column {f.name!r} more than once"
                + (f" ({seen[key]!r} and {name!r})" if seen[key] != name else "")
                + "; give each output column its own name with AS.",
                where, "output_name", names=[seen[key], name],
            )
        seen[key] = name
    return QueryAnalysis(columns=columns, fields=fields, used=used, renamed=renamed)


def _looks_like_file(name: str) -> bool:
    lowered = name.casefold()
    return ("/" in lowered or "\\" in lowered or "://" in lowered or "*" in lowered
            or lowered.endswith(_FILE_SUFFIXES))


def _without_dollars(sql: str, offsets: list[int]) -> str:
    """The statement with the ``$`` dropped in front of each placeholder written as a parameter."""
    text = sql
    for offset in sorted(offsets, reverse=True):
        text = text[:offset] + text[offset + 1:]
    return text
