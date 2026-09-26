"""Analytical execution engine abstraction with a DuckDB implementation.

The rest of the backend talks to ``AnalyticsEngine`` only; the executor emits
SQL against physical table names that the backend allocates. Source files are
described by ``TableSource`` (a file plus, for multi-table containers such as
SQLite, a table locator); the engine knows how to read csv, parquet, json
(top-level arrays or ``{"table": ..., "records": [...]}`` wrappers) and
SQLite tables.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb

from ...core.errors import ExecutionFailedError, InvalidSchemaError
from ...core.models.entities import LogicalType

SUPPORTED_FORMATS = ("csv", "parquet", "json", "sqlite")

# The documented patterns for import-time temporal refinement (MADR 0006). A text
# column becomes temporal only when every non-null value matches one of these and
# casts cleanly; locale-dependent orders such as 03/04/2026 stay text on purpose.
ISO_DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
ISO_TIMESTAMP_PATTERN = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?Z?"
TEMPORAL_TYPES = ("DATE", "TIMESTAMP")


@dataclass(frozen=True)
class TableSource:
    path: Path
    format: str
    table: str | None = None  # locator inside a multi-table container (sqlite)

    def describe(self) -> str:
        return f"{self.path.name}::{self.table}" if self.table else self.path.name


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def physical_to_logical(physical_type: str) -> LogicalType:
    upper = physical_type.upper()
    if upper in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT"):
        return LogicalType.INTEGER
    if upper in ("FLOAT", "DOUBLE", "REAL") or upper.startswith("DECIMAL"):
        return LogicalType.FLOAT
    if upper == "BOOLEAN":
        return LogicalType.BOOLEAN
    if upper in ("VARCHAR", "TEXT", "STRING", "UUID"):
        return LogicalType.STRING
    if upper == "DATE":
        return LogicalType.DATE
    if upper.startswith("TIMESTAMP"):
        return LogicalType.TIMESTAMP
    return LogicalType.UNKNOWN


def logical_to_physical(logical_type: LogicalType) -> str:
    return {
        LogicalType.INTEGER: "BIGINT",
        LogicalType.FLOAT: "DOUBLE",
        LogicalType.BOOLEAN: "BOOLEAN",
        LogicalType.STRING: "VARCHAR",
        LogicalType.DATE: "DATE",
        LogicalType.TIMESTAMP: "TIMESTAMP",
        LogicalType.UNKNOWN: "VARCHAR",
    }[logical_type]


class AnalyticsEngine(Protocol):
    def inspect_source(self, source: TableSource) -> list[tuple[str, str]]: ...
    def import_source(self, source: TableSource, table: str) -> int: ...
    def list_container_tables(self, path: Path, fmt: str) -> list[str]: ...
    def inspect_file(self, path: Path, fmt: str) -> list[tuple[str, str]]: ...
    def import_file(self, path: Path, fmt: str, table: str) -> int: ...
    def create_table_from_query(self, table: str, sql: str) -> int: ...
    def query(self, sql: str, limit: int | None = None) -> tuple[list[str], list[tuple[Any, ...]]]: ...
    def count_rows_of_query(self, sql: str) -> int: ...
    def describe_table(self, table: str) -> list[tuple[str, str]]: ...
    def probe_temporal_type(self, table: str, column: str) -> str | None: ...
    def cast_column(self, table: str, column: str, physical_type: str) -> None: ...
    def table_exists(self, table: str) -> bool: ...
    def row_count(self, table: str) -> int: ...
    def count_distinct_rows(self, table: str, columns: list[str]) -> int: ...
    def count_distinct_of_query(self, sql: str, columns: list[str]) -> int: ...
    def column_contains(self, table: str, column: str, value: Any) -> bool: ...
    def compare_text_as_number(self, table: str, column: str, op: str, text: str) -> tuple[int, int, str | None]: ...
    def sample(self, table: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]]]: ...
    def profile_columns(self, table: str, columns: list[str], *, ranged: list[str]) -> dict[str, dict[str, Any]]: ...
    def read_table(self, table: str, *, columns: list[str] | None = None,
                   order_by: list[tuple[str, bool]] | None = None) -> tuple[list[str], list[tuple[Any, ...]]]: ...
    def drop_table(self, table: str) -> None: ...
    def list_tables(self) -> list[str]: ...
    def export_table(self, table: str, path: Path, fmt: str, *, columns: list[str] | None = None,
                     order_by: list[tuple[str, bool]] | None = None) -> int: ...
    def close(self) -> None: ...


class DuckDBEngine:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn = duckdb.connect(str(self.path))
        self._sqlite_loaded = False

    # -- readers ---------------------------------------------------------
    def _describe_sql(self, sql: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(f"DESCRIBE SELECT * FROM ({sql}) AS q").fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    def _reader_sql(self, source: TableSource) -> str:
        """SELECT statement that reads the source. SQLite sources must be attached first."""
        literal = _literal(str(source.path))
        if source.format == "csv":
            return f"SELECT * FROM read_csv({literal}, header=true, sample_size=-1)"
        if source.format == "parquet":
            return f"SELECT * FROM read_parquet({literal})"
        if source.format == "json":
            base = f"read_json_auto({literal}, maximum_object_size=1073741824)"
            columns = self._describe_sql(f"SELECT * FROM {base}")
            lists = [(name, typ) for name, typ in columns if typ.startswith("STRUCT(") and typ.endswith("[]")]
            if len(lists) == 1 and all(not t.endswith("[]") for n, t in columns if n != lists[0][0]):
                # {"table": "...", "records": [ {...}, ... ]} wrapper: expand the record list.
                return f"SELECT unnest({quote_ident(lists[0][0])}, recursive := true) FROM {base}"
            return f"SELECT * FROM {base}"
        if source.format == "sqlite":
            if not source.table:
                raise InvalidSchemaError("A SQLite source needs a table locator.", field="path")
            return f"SELECT * FROM {quote_ident(self._sqlite_alias(source.path))}.{quote_ident(source.table)}"
        raise InvalidSchemaError(
            f"Unsupported file format {source.format!r}; supported formats are {', '.join(SUPPORTED_FORMATS)}."
        )

    # -- sqlite attachment -----------------------------------------------
    @staticmethod
    def _sqlite_alias(path: Path) -> str:
        return "_src_" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]

    def _ensure_sqlite(self) -> None:
        if self._sqlite_loaded:
            return
        try:
            self.conn.execute("LOAD sqlite")
        except duckdb.Error:
            try:
                self.conn.execute("INSTALL sqlite; LOAD sqlite;")
            except duckdb.Error as exc:
                raise InvalidSchemaError(
                    f"SQLite sources are unavailable: the DuckDB sqlite extension could not be loaded ({exc})."
                ) from exc
        self._sqlite_loaded = True

    def _attach_sqlite(self, path: Path) -> str:
        self._ensure_sqlite()
        alias = self._sqlite_alias(path)
        try:
            self.conn.execute(f"ATTACH {_literal(str(path))} AS {quote_ident(alias)} (TYPE SQLITE, READ_ONLY)")
        except duckdb.Error as exc:
            raise InvalidSchemaError(f"Could not open SQLite file {path.name}: {exc}") from exc
        return alias

    def _detach_sqlite(self, path: Path) -> None:
        try:
            self.conn.execute(f"DETACH {quote_ident(self._sqlite_alias(path))}")
        except duckdb.Error:
            pass

    # -- protocol: sources -----------------------------------------------
    def list_container_tables(self, path: Path, fmt: str) -> list[str]:
        if fmt != "sqlite":
            return []
        alias = self._attach_sqlite(path)
        try:
            rows = self.conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_catalog = ? ORDER BY table_name",
                [alias],
            ).fetchall()
            return [str(r[0]) for r in rows]
        except duckdb.Error as exc:
            raise InvalidSchemaError(f"Could not read SQLite file {path.name}: {exc}") from exc
        finally:
            self._detach_sqlite(path)

    def inspect_source(self, source: TableSource) -> list[tuple[str, str]]:
        attached = False
        try:
            if source.format == "sqlite":
                self._attach_sqlite(source.path)
                attached = True
            return self._describe_sql(self._reader_sql(source))
        except duckdb.Error as exc:
            raise InvalidSchemaError(f"Could not read {source.format} source {source.describe()}: {exc}") from exc
        finally:
            if attached:
                self._detach_sqlite(source.path)

    def import_source(self, source: TableSource, table: str) -> int:
        attached = False
        try:
            if source.format == "sqlite":
                self._attach_sqlite(source.path)
                attached = True
            self.conn.execute(f"CREATE TABLE {quote_ident(table)} AS {self._reader_sql(source)}")
        except duckdb.Error as exc:
            raise ExecutionFailedError(f"Import of {source.describe()} into analytical store failed: {exc}") from exc
        finally:
            if attached:
                self._detach_sqlite(source.path)
        return self.row_count(table)

    def inspect_file(self, path: Path, fmt: str) -> list[tuple[str, str]]:
        return self.inspect_source(TableSource(path=path, format=fmt))

    def import_file(self, path: Path, fmt: str, table: str) -> int:
        return self.import_source(TableSource(path=path, format=fmt), table)

    # -- protocol: tables ------------------------------------------------
    def create_table_from_query(self, table: str, sql: str) -> int:
        try:
            self.conn.execute(f"CREATE TABLE {quote_ident(table)} AS {sql}")
        except duckdb.Error as exc:
            raise ExecutionFailedError(f"Execution failed: {exc}", details={"sql": sql}) from exc
        return self.row_count(table)

    def query(self, sql: str, limit: int | None = None) -> tuple[list[str], list[tuple[Any, ...]]]:
        wrapped = sql if limit is None else f"SELECT * FROM ({sql}) AS q LIMIT {int(limit)}"
        try:
            cursor = self.conn.execute(wrapped)
            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description or []]
        except duckdb.Error as exc:
            raise ExecutionFailedError(f"Execution failed: {exc}", details={"sql": sql}) from exc
        return columns, rows

    def count_rows_of_query(self, sql: str) -> int:
        try:
            row = self.conn.execute(f"SELECT COUNT(*) FROM ({sql}) AS q").fetchone()
        except duckdb.Error as exc:
            raise ExecutionFailedError(f"Execution failed: {exc}", details={"sql": sql}) from exc
        return int(row[0]) if row else 0

    def describe_table(self, table: str) -> list[tuple[str, str]]:
        return self._describe_sql(f"SELECT * FROM {quote_ident(table)}")

    def probe_temporal_type(self, table: str, column: str) -> str | None:
        """DATE or TIMESTAMP when every non-null value of a text column is one.

        DATE only when every value is a bare date; a column that mixes dates and
        timestamps becomes TIMESTAMP. Deterministic by construction: a value must
        match the documented ISO pattern *and* cast cleanly, so a column holding
        2002-02-31 stays textual. An all-null column is left alone.
        """
        col = quote_ident(column)
        sql = (
            f"SELECT count({col}), "
            f"count(*) FILTER (WHERE regexp_full_match({col}, {_literal(ISO_DATE_PATTERN)})), "
            f"count(*) FILTER (WHERE regexp_full_match({col}, {_literal(ISO_TIMESTAMP_PATTERN)})), "
            f"count(try_cast({col} AS TIMESTAMP)) "
            f"FROM {quote_ident(table)}"
        )
        try:
            row = self.conn.execute(sql).fetchone()
        except duckdb.Error:
            return None
        if not row:
            return None
        non_null, dates, timestamps, castable = (int(v or 0) for v in row)
        if non_null == 0 or castable != non_null:
            return None
        if dates == non_null:
            return "DATE"
        if dates + timestamps == non_null:
            return "TIMESTAMP"
        return None

    def cast_column(self, table: str, column: str, physical_type: str) -> None:
        if physical_type not in TEMPORAL_TYPES:
            raise InvalidSchemaError(f"Refusing to cast {column!r} to {physical_type!r}.", field="schema_hints")
        try:
            self.conn.execute(f"ALTER TABLE {quote_ident(table)} ALTER COLUMN {quote_ident(column)} TYPE {physical_type}")
        except duckdb.Error as exc:
            raise ExecutionFailedError(f"Could not cast column {column!r} to {physical_type}: {exc}") from exc

    def table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ? AND table_schema = 'main'", [table]
        ).fetchone()
        return bool(row and row[0])

    def row_count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()
        return int(row[0]) if row else 0

    def count_distinct_rows(self, table: str, columns: list[str]) -> int:
        """How many distinct combinations of ``columns`` the table holds (NULLs count as one value)."""
        return self.count_distinct_of_query(f"SELECT * FROM {quote_ident(table)}", columns)

    def count_distinct_of_query(self, sql: str, columns: list[str]) -> int:
        """How many distinct combinations of ``columns`` the query returns (NULLs count as one value)."""
        keys = ", ".join(quote_ident(c) for c in columns)
        return self.count_rows_of_query(f"SELECT DISTINCT {keys} FROM ({sql}) AS keyed")

    def column_contains(self, table: str, column: str, value: Any) -> bool:
        """Whether ``value`` occurs in ``column`` (by equality; a value the column's type cannot hold never does)."""
        try:
            row = self.conn.execute(
                f"SELECT 1 FROM {quote_ident(table)} WHERE {quote_ident(column)} = ? LIMIT 1", [value]
            ).fetchone()
        except duckdb.Error:
            return False
        return row is not None

    def compare_text_as_number(self, table: str, column: str, op: str, text: str) -> tuple[int, int, str | None]:
        """For ``column op 'text'`` over a text column: how many non-blank values are not numbers, how many
        numbers compare differently as numbers than as text, and one of those. One scan of the table."""
        if op not in ("<", "<=", ">", ">="):
            raise ValueError(f"not an ordering comparison: {op!r}")
        ident = quote_ident(column)
        number = f"TRY_CAST(NULLIF(trim({ident}), '') AS DOUBLE)"
        differs = f"({number} IS NOT NULL AND ({ident} {op} ?) <> ({number} {op} ?))"
        row = self.conn.execute(
            f"SELECT count(*) FILTER (WHERE NULLIF(trim({ident}), '') IS NOT NULL AND {number} IS NULL), "
            f"count(*) FILTER (WHERE {differs}), min({ident}) FILTER (WHERE {differs}) "
            f"FROM {quote_ident(table)}",
            [text, float(text), text, float(text)],
        ).fetchone()
        return int(row[0]), int(row[1]), row[2]

    def sample(self, table: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        return self.query(f"SELECT * FROM {quote_ident(table)}", limit=limit)

    def profile_columns(self, table: str, columns: list[str], *, ranged: list[str]) -> dict[str, dict[str, Any]]:
        """Per column, how many values are not null and how many are distinct (nulls not counted), plus the
        minimum and maximum of the columns named in ``ranged``; one scan of the table."""
        if not columns:
            return {}
        parts: list[str] = []
        for column in columns:
            ident = quote_ident(column)
            parts.append(f"COUNT({ident}), COUNT(DISTINCT {ident})")
            if column in ranged:
                parts.append(f"MIN({ident}), MAX({ident})")
        row = self.conn.execute(f"SELECT {', '.join(parts)} FROM {quote_ident(table)}").fetchone() or ()
        values = iter(row)
        profile: dict[str, dict[str, Any]] = {}
        for column in columns:
            facts: dict[str, Any] = {"non_null": int(next(values)), "distinct": int(next(values))}
            if column in ranged:
                facts["min"], facts["max"] = next(values), next(values)
            profile[column] = facts
        return profile

    def read_table(self, table: str, *, columns: list[str] | None = None,
                   order_by: list[tuple[str, bool]] | None = None) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Every row of a table as typed Python values (used by formatted exports).

        ``columns`` and ``order_by`` project and sort as ``export_table`` does,
        so both export paths write the same rows in the same order.
        """
        projection = ", ".join(quote_ident(c) for c in columns) if columns else "*"
        ordering = ""
        if order_by:
            ordering = " ORDER BY " + ", ".join(
                quote_ident(c) + (" DESC" if descending else " ASC") for c, descending in order_by)
        return self.query(f"SELECT {projection} FROM {quote_ident(table)}{ordering}")

    def drop_table(self, table: str) -> None:
        self.conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")

    def list_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_catalog = current_database() ORDER BY 1"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def export_table(self, table: str, path: Path, fmt: str, *, columns: list[str] | None = None,
                     order_by: list[tuple[str, bool]] | None = None) -> int:
        """Write a table to a file; returns the row count written.

        ``columns`` writes those columns, in that order, instead of every
        column; ``order_by`` names ``(column, descending)`` pairs to sort by,
        which need not be among the columns written.
        """
        if fmt == "csv":
            options = "FORMAT CSV, HEADER TRUE, DELIMITER ',', NULL ''"
        elif fmt == "parquet":
            options = "FORMAT PARQUET"
        else:
            raise InvalidSchemaError(f"Unsupported export format {fmt!r}; supported formats are csv and parquet.", field="format")
        projection = ", ".join(quote_ident(c) for c in columns) if columns else "*"
        ordering = ""
        if order_by:
            ordering = " ORDER BY " + ", ".join(
                quote_ident(c) + (" DESC" if descending else " ASC") for c, descending in order_by)
        try:
            self.conn.execute(
                f"COPY (SELECT {projection} FROM {quote_ident(table)}{ordering}) TO {_literal(str(path))} ({options})")
        except duckdb.Error as exc:
            raise ExecutionFailedError(f"Export failed: {exc}", details={"path": str(path)}) from exc
        return self.row_count(table)

    def close(self) -> None:
        self.conn.close()
