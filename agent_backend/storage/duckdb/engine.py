"""Analytical execution engine abstraction with a DuckDB implementation.

The rest of the backend talks to ``AnalyticsEngine`` only; the executor emits
SQL against physical table names that the backend allocates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import duckdb

from ...core.errors import ExecutionFailedError, InvalidSchemaError
from ...core.models.entities import LogicalType


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


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
    def inspect_file(self, path: Path, fmt: str) -> list[tuple[str, str]]: ...
    def import_file(self, path: Path, fmt: str, table: str) -> int: ...
    def create_table_from_query(self, table: str, sql: str) -> int: ...
    def query(self, sql: str, limit: int | None = None) -> tuple[list[str], list[tuple[Any, ...]]]: ...
    def count_rows_of_query(self, sql: str) -> int: ...
    def describe_table(self, table: str) -> list[tuple[str, str]]: ...
    def table_exists(self, table: str) -> bool: ...
    def row_count(self, table: str) -> int: ...
    def sample(self, table: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]]]: ...
    def drop_table(self, table: str) -> None: ...
    def list_tables(self) -> list[str]: ...
    def close(self) -> None: ...


class DuckDBEngine:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn = duckdb.connect(str(self.path))

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _reader(path: Path, fmt: str) -> str:
        literal = str(path).replace("'", "''")
        if fmt == "csv":
            return f"read_csv('{literal}', header=true, sample_size=-1)"
        if fmt == "parquet":
            return f"read_parquet('{literal}')"
        raise InvalidSchemaError(f"Unsupported file format {fmt!r}; supported formats are csv and parquet.")

    def _describe_sql(self, sql: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(f"DESCRIBE SELECT * FROM ({sql}) AS q").fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    # -- protocol --------------------------------------------------------
    def inspect_file(self, path: Path, fmt: str) -> list[tuple[str, str]]:
        try:
            return self._describe_sql(f"SELECT * FROM {self._reader(path, fmt)}")
        except duckdb.Error as exc:
            raise InvalidSchemaError(f"Could not read {fmt} file: {exc}") from exc

    def import_file(self, path: Path, fmt: str, table: str) -> int:
        try:
            self.conn.execute(f"CREATE TABLE {quote_ident(table)} AS SELECT * FROM {self._reader(path, fmt)}")
        except duckdb.Error as exc:
            raise ExecutionFailedError(f"Import into analytical store failed: {exc}") from exc
        return self.row_count(table)

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

    def table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
        ).fetchone()
        return bool(row and row[0])

    def row_count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()
        return int(row[0]) if row else 0

    def sample(self, table: str, limit: int) -> tuple[list[str], list[tuple[Any, ...]]]:
        return self.query(f"SELECT * FROM {quote_ident(table)}", limit=limit)

    def drop_table(self, table: str) -> None:
        self.conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")

    def list_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY 1"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def close(self) -> None:
        self.conn.close()
