"""SQLite implementation of the metadata store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ...core.models.entities import (
    Artifact,
    ArtifactKind,
    AuditEvent,
    Column,
    ContractColumn,
    ContractOrder,
    ContractStatus,
    Dataset,
    DatasetStatus,
    DatasetVersion,
    LineageEdge,
    LogicalType,
    Operation,
    OperationKind,
    OperationStatus,
    OutputContract,
    Relationship,
    RowCardinality,
    SemanticRole,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS id_sequences (
    prefix TEXT PRIMARY KEY,
    next_value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    physical_location TEXT NOT NULL,
    origin TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    deleted_at TEXT,
    source_artifact_id TEXT,
    source_locator TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_datasets_active_name
    ON datasets(name) WHERE status != 'deleted';
CREATE TABLE IF NOT EXISTS columns (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    name TEXT NOT NULL,
    logical_type TEXT NOT NULL,
    physical_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    semantic_role TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    unit TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL,
    UNIQUE(dataset_id, name)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    managed_path TEXT,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_artifacts_hash ON artifacts(content_hash);
CREATE TABLE IF NOT EXISTS dataset_versions (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    version INTEGER NOT NULL,
    physical_table TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL,
    columns_json TEXT NOT NULL,
    operation_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, version)
);
CREATE TABLE IF NOT EXISTS lineage_edges (
    id TEXT PRIMARY KEY,
    source_dataset_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    target_dataset_id TEXT NOT NULL,
    target_version INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lineage_target ON lineage_edges(target_dataset_id);
CREATE INDEX IF NOT EXISTS ix_lineage_source ON lineage_edges(source_dataset_id);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    original_intent_json TEXT NOT NULL,
    canonical_ir_json TEXT,
    execution_plan_json TEXT,
    result_json TEXT,
    error_json TEXT,
    idempotency_key TEXT,
    principal TEXT,
    parent_operation_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    operation_id TEXT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_events(entity_id);
CREATE TABLE IF NOT EXISTS output_contracts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    rows TEXT,
    row_keys_json TEXT NOT NULL DEFAULT '[]',
    order_by_json TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    operation_id TEXT NOT NULL,
    satisfied_by TEXT,
    dataset_id TEXT,
    dataset_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release; applied to existing workspaces.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("datasets", "source_artifact_id", "TEXT"),
    ("datasets", "source_locator", "TEXT"),
    ("columns", "unit", "TEXT NOT NULL DEFAULT ''"),
    ("operations", "parent_operation_id", "TEXT"),
    ("output_contracts", "order_by_json", "TEXT NOT NULL DEFAULT '[]'"),
]


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class SqliteMetadataStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._depth = 0
        self._rollback_only = False

    def _migrate(self) -> None:
        for table, column, decl in MIGRATIONS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # -- transactions ----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
            self._rollback_only = False
        self._depth += 1
        try:
            yield
        except BaseException:
            self._rollback_only = True
            raise
        finally:
            self._depth -= 1
            if self._depth == 0:
                if self._rollback_only:
                    self.conn.execute("ROLLBACK")
                else:
                    self.conn.execute("COMMIT")

    def allocate_id(self, prefix: str) -> str:
        with self.transaction():
            row = self.conn.execute("SELECT next_value FROM id_sequences WHERE prefix = ?", (prefix,)).fetchone()
            value = int(row["next_value"]) if row else 1
            self.conn.execute(
                "INSERT INTO id_sequences(prefix, next_value) VALUES (?, ?) "
                "ON CONFLICT(prefix) DO UPDATE SET next_value = excluded.next_value",
                (prefix, value + 1),
            )
        return f"{prefix}_{value}"

    # -- datasets --------------------------------------------------------
    def _row_to_dataset(self, row: sqlite3.Row, with_columns: bool = True) -> Dataset:
        dataset = Dataset(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            status=DatasetStatus(row["status"]),
            version=int(row["version"]),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
            physical_location=row["physical_location"],
            origin=row["origin"],
            metadata=json.loads(row["metadata_json"]),
            deleted_at=_dt(row["deleted_at"]),
            source_artifact_id=row["source_artifact_id"],
            source_locator=row["source_locator"],
        )
        if with_columns:
            dataset.columns = self.get_columns(dataset.id)
        return dataset

    def insert_dataset(self, dataset: Dataset) -> None:
        self.conn.execute(
            "INSERT INTO datasets(id, name, description, status, version, created_at, updated_at, "
            "physical_location, origin, metadata_json, deleted_at, source_artifact_id, source_locator) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                dataset.id,
                dataset.name,
                dataset.description,
                str(dataset.status),
                dataset.version,
                _iso(dataset.created_at),
                _iso(dataset.updated_at),
                dataset.physical_location,
                dataset.origin,
                json.dumps(dataset.metadata, sort_keys=True, ensure_ascii=False),
                _iso(dataset.deleted_at),
                dataset.source_artifact_id,
                dataset.source_locator,
            ),
        )

    def update_dataset(self, dataset: Dataset) -> None:
        self.conn.execute(
            "UPDATE datasets SET name=?, description=?, status=?, version=?, updated_at=?, physical_location=?, "
            "origin=?, metadata_json=?, deleted_at=?, source_artifact_id=?, source_locator=? WHERE id=?",
            (
                dataset.name,
                dataset.description,
                str(dataset.status),
                dataset.version,
                _iso(dataset.updated_at),
                dataset.physical_location,
                dataset.origin,
                json.dumps(dataset.metadata, sort_keys=True, ensure_ascii=False),
                _iso(dataset.deleted_at),
                dataset.source_artifact_id,
                dataset.source_locator,
                dataset.id,
            ),
        )

    def get_dataset(self, dataset_id: str, include_deleted: bool = False) -> Dataset | None:
        row = self.conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if row is None:
            return None
        if row["status"] == "deleted" and not include_deleted:
            return None
        return self._row_to_dataset(row)

    def get_dataset_by_name(self, name: str) -> Dataset | None:
        row = self.conn.execute(
            "SELECT * FROM datasets WHERE name = ? AND status != 'deleted'", (name,)
        ).fetchone()
        return self._row_to_dataset(row) if row else None

    def list_datasets(self, include_deleted: bool = False) -> list[Dataset]:
        sql = "SELECT * FROM datasets" + ("" if include_deleted else " WHERE status != 'deleted'") + " ORDER BY created_at, id"
        return [self._row_to_dataset(row) for row in self.conn.execute(sql).fetchall()]

    # -- columns ---------------------------------------------------------
    def insert_columns(self, columns: list[Column]) -> None:
        self.conn.executemany(
            "INSERT INTO columns(id, dataset_id, name, logical_type, physical_type, description, semantic_role, "
            "aliases_json, unit, position) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    c.id,
                    c.dataset_id,
                    c.name,
                    str(c.logical_type),
                    c.physical_type,
                    c.description,
                    str(c.semantic_role),
                    json.dumps(c.aliases, ensure_ascii=False),
                    c.unit,
                    c.position,
                )
                for c in columns
            ],
        )

    def update_column(self, column: Column) -> None:
        self.conn.execute(
            "UPDATE columns SET description=?, semantic_role=?, aliases_json=?, unit=? WHERE id=?",
            (column.description, str(column.semantic_role), json.dumps(column.aliases, ensure_ascii=False),
             column.unit, column.id),
        )

    def get_columns(self, dataset_id: str) -> list[Column]:
        rows = self.conn.execute(
            "SELECT * FROM columns WHERE dataset_id = ? ORDER BY position", (dataset_id,)
        ).fetchall()
        return [
            Column(
                id=r["id"],
                dataset_id=r["dataset_id"],
                name=r["name"],
                logical_type=LogicalType(r["logical_type"]),
                physical_type=r["physical_type"],
                description=r["description"],
                semantic_role=SemanticRole(r["semantic_role"]),
                aliases=json.loads(r["aliases_json"]),
                unit=r["unit"] or "",
                position=int(r["position"]),
            )
            for r in rows
        ]

    # -- artifacts -------------------------------------------------------
    @staticmethod
    def _row_to_artifact(r: sqlite3.Row) -> Artifact:
        return Artifact(
            id=r["id"],
            kind=ArtifactKind(r["kind"]),
            name=r["name"],
            path=r["path"],
            managed_path=r["managed_path"],
            content_hash=r["content_hash"],
            size_bytes=int(r["size_bytes"]),
            created_at=_dt(r["created_at"]),
            metadata=json.loads(r["metadata_json"]),
        )

    def insert_artifact(self, artifact: Artifact) -> None:
        self.conn.execute(
            "INSERT INTO artifacts(id, kind, name, path, managed_path, content_hash, size_bytes, created_at, metadata_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                artifact.id,
                str(artifact.kind),
                artifact.name,
                artifact.path,
                artifact.managed_path,
                artifact.content_hash,
                artifact.size_bytes,
                _iso(artifact.created_at),
                json.dumps(artifact.metadata, sort_keys=True, ensure_ascii=False),
            ),
        )

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        row = self.conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return self._row_to_artifact(row) if row else None

    def find_artifact(self, path: str, content_hash: str) -> Artifact | None:
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE path = ? AND content_hash = ? ORDER BY created_at LIMIT 1",
            (path, content_hash),
        ).fetchone()
        return self._row_to_artifact(row) if row else None

    def list_artifacts(self) -> list[Artifact]:
        rows = self.conn.execute("SELECT * FROM artifacts ORDER BY created_at, id").fetchall()
        return [self._row_to_artifact(r) for r in rows]

    # -- versions --------------------------------------------------------
    @staticmethod
    def _row_to_version(r: sqlite3.Row) -> DatasetVersion:
        return DatasetVersion(
            id=r["id"],
            dataset_id=r["dataset_id"],
            version=int(r["version"]),
            physical_table=r["physical_table"],
            row_count=int(r["row_count"]),
            columns_snapshot=json.loads(r["columns_json"]),
            operation_id=r["operation_id"],
            created_at=_dt(r["created_at"]),
        )

    def insert_version(self, version: DatasetVersion) -> None:
        self.conn.execute(
            "INSERT INTO dataset_versions(id, dataset_id, version, physical_table, row_count, columns_json, "
            "operation_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                version.id,
                version.dataset_id,
                version.version,
                version.physical_table,
                version.row_count,
                json.dumps(version.columns_snapshot, ensure_ascii=False),
                version.operation_id,
                _iso(version.created_at),
            ),
        )

    def list_versions(self, dataset_id: str) -> list[DatasetVersion]:
        rows = self.conn.execute(
            "SELECT * FROM dataset_versions WHERE dataset_id = ? ORDER BY version DESC", (dataset_id,)
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def get_version(self, dataset_id: str, version: int) -> DatasetVersion | None:
        row = self.conn.execute(
            "SELECT * FROM dataset_versions WHERE dataset_id = ? AND version = ?", (dataset_id, version)
        ).fetchone()
        return self._row_to_version(row) if row else None

    def list_all_versions(self) -> list[DatasetVersion]:
        rows = self.conn.execute("SELECT * FROM dataset_versions ORDER BY dataset_id, version").fetchall()
        return [self._row_to_version(r) for r in rows]

    # -- lineage ---------------------------------------------------------
    @staticmethod
    def _row_to_edge(r: sqlite3.Row) -> LineageEdge:
        return LineageEdge(
            id=r["id"],
            source_dataset_id=r["source_dataset_id"],
            source_version=int(r["source_version"]),
            target_dataset_id=r["target_dataset_id"],
            target_version=int(r["target_version"]),
            operation_id=r["operation_id"],
            relationship=Relationship(r["relationship"]),
            created_at=_dt(r["created_at"]),
        )

    def insert_lineage_edge(self, edge: LineageEdge) -> None:
        self.conn.execute(
            "INSERT INTO lineage_edges(id, source_dataset_id, source_version, target_dataset_id, target_version, "
            "operation_id, relationship, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                edge.id,
                edge.source_dataset_id,
                edge.source_version,
                edge.target_dataset_id,
                edge.target_version,
                edge.operation_id,
                str(edge.relationship),
                _iso(edge.created_at),
            ),
        )

    def lineage_into(self, dataset_id: str) -> list[LineageEdge]:
        rows = self.conn.execute(
            "SELECT * FROM lineage_edges WHERE target_dataset_id = ? ORDER BY created_at, id", (dataset_id,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def lineage_out_of(self, dataset_id: str) -> list[LineageEdge]:
        rows = self.conn.execute(
            "SELECT * FROM lineage_edges WHERE source_dataset_id = ? ORDER BY created_at, id", (dataset_id,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    # -- operations ------------------------------------------------------
    @staticmethod
    def _row_to_operation(r: sqlite3.Row) -> Operation:
        loads = lambda v: json.loads(v) if v else None  # noqa: E731
        return Operation(
            id=r["id"],
            kind=OperationKind(r["kind"]),
            status=OperationStatus(r["status"]),
            original_intent=json.loads(r["original_intent_json"]),
            canonical_ir=loads(r["canonical_ir_json"]),
            execution_plan=loads(r["execution_plan_json"]),
            result=loads(r["result_json"]),
            error=loads(r["error_json"]),
            idempotency_key=r["idempotency_key"],
            principal=r["principal"],
            parent_operation_id=r["parent_operation_id"],
            created_at=_dt(r["created_at"]),
            completed_at=_dt(r["completed_at"]),
        )

    def insert_operation(self, operation: Operation) -> None:
        self.conn.execute(
            "INSERT INTO operations(id, kind, status, original_intent_json, canonical_ir_json, execution_plan_json, "
            "result_json, error_json, idempotency_key, principal, parent_operation_id, created_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self._operation_values(operation),
        )

    def update_operation(self, operation: Operation) -> None:
        values = self._operation_values(operation)
        self.conn.execute(
            "UPDATE operations SET kind=?, status=?, original_intent_json=?, canonical_ir_json=?, execution_plan_json=?, "
            "result_json=?, error_json=?, idempotency_key=?, principal=?, parent_operation_id=?, created_at=?, "
            "completed_at=? WHERE id=?",
            values[1:] + (operation.id,),
        )

    @staticmethod
    def _operation_values(op: Operation) -> tuple[Any, ...]:
        dumps = lambda v: json.dumps(v, default=str, ensure_ascii=False) if v is not None else None  # noqa: E731
        return (
            op.id,
            str(op.kind),
            str(op.status),
            json.dumps(op.original_intent, default=str, ensure_ascii=False),
            dumps(op.canonical_ir),
            dumps(op.execution_plan),
            dumps(op.result),
            dumps(op.error),
            op.idempotency_key,
            op.principal,
            op.parent_operation_id,
            _iso(op.created_at),
            _iso(op.completed_at),
        )

    def get_operation(self, operation_id: str) -> Operation | None:
        row = self.conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        return self._row_to_operation(row) if row else None

    def list_operations(self, limit: int = 50) -> list[Operation]:
        rows = self.conn.execute(
            "SELECT * FROM operations ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_operation(r) for r in rows]

    # -- audit -----------------------------------------------------------
    def insert_audit_event(self, event: AuditEvent) -> None:
        self.conn.execute(
            "INSERT INTO audit_events(id, operation_id, event_type, entity_type, entity_id, details_json, actor, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                event.id,
                event.operation_id,
                event.event_type,
                event.entity_type,
                event.entity_id,
                json.dumps(event.details, default=str, ensure_ascii=False),
                event.actor,
                _iso(event.created_at),
            ),
        )

    def list_audit_events(self, entity_id: str | None = None, operation_id: str | None = None) -> list[AuditEvent]:
        clauses, params = [], []
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        if operation_id:
            clauses.append("operation_id = ?")
            params.append(operation_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM audit_events{where} ORDER BY created_at, CAST(substr(id, 4) AS INTEGER)", params
        ).fetchall()
        return [
            AuditEvent(
                id=r["id"],
                operation_id=r["operation_id"],
                event_type=r["event_type"],
                entity_type=r["entity_type"],
                entity_id=r["entity_id"],
                details=json.loads(r["details_json"]),
                actor=r["actor"],
                created_at=_dt(r["created_at"]),
            )
            for r in rows
        ]

    # -- output contracts ------------------------------------------------
    @staticmethod
    def _row_to_contract(r: sqlite3.Row) -> OutputContract:
        return OutputContract(
            id=r["id"],
            status=ContractStatus(r["status"]),
            columns=[ContractColumn(name=c["name"], logical_type=LogicalType(c["type"]) if c.get("type") else None)
                     for c in json.loads(r["columns_json"])],
            rows=RowCardinality(r["rows"]) if r["rows"] else None,
            row_keys=json.loads(r["row_keys_json"]),
            order_by=[ContractOrder(name=o["column"], descending=bool(o.get("descending")))
                      for o in json.loads(r["order_by_json"] or "[]")],
            description=r["description"],
            revision=int(r["revision"]),
            operation_id=r["operation_id"],
            satisfied_by=r["satisfied_by"],
            dataset_id=r["dataset_id"],
            dataset_version=r["dataset_version"],
            created_at=_dt(r["created_at"]),
            updated_at=_dt(r["updated_at"]),
        )

    @staticmethod
    def _contract_values(c: OutputContract) -> tuple[Any, ...]:
        columns = [{"name": col.name, **({"type": str(col.logical_type)} if col.logical_type else {})} for col in c.columns]
        return (
            c.id,
            str(c.status),
            json.dumps(columns, ensure_ascii=False),
            str(c.rows) if c.rows else None,
            json.dumps(c.row_keys, ensure_ascii=False),
            json.dumps([{"column": o.name, "descending": o.descending} for o in c.order_by], ensure_ascii=False),
            c.description,
            c.revision,
            c.operation_id,
            c.satisfied_by,
            c.dataset_id,
            c.dataset_version,
            _iso(c.created_at),
            _iso(c.updated_at),
        )

    def insert_contract(self, contract: OutputContract) -> None:
        self.conn.execute(
            "INSERT INTO output_contracts(id, status, columns_json, rows, row_keys_json, order_by_json, description, "
            "revision, operation_id, satisfied_by, dataset_id, dataset_version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            self._contract_values(contract),
        )

    def update_contract(self, contract: OutputContract) -> None:
        values = self._contract_values(contract)
        self.conn.execute(
            "UPDATE output_contracts SET status=?, columns_json=?, rows=?, row_keys_json=?, order_by_json=?, "
            "description=?, revision=?, operation_id=?, satisfied_by=?, dataset_id=?, dataset_version=?, "
            "created_at=?, updated_at=? WHERE id=?",
            values[1:] + (contract.id,),
        )

    def get_contract(self, contract_id: str) -> OutputContract | None:
        row = self.conn.execute("SELECT * FROM output_contracts WHERE id = ?", (contract_id,)).fetchone()
        return self._row_to_contract(row) if row else None

    def latest_contract(self) -> OutputContract | None:
        row = self.conn.execute("SELECT * FROM output_contracts ORDER BY created_at DESC, CAST(substr(id, 4) AS INTEGER) DESC LIMIT 1").fetchone()
        return self._row_to_contract(row) if row else None

    def list_contracts(self, limit: int = 50) -> list[OutputContract]:
        rows = self.conn.execute(
            "SELECT * FROM output_contracts ORDER BY created_at DESC, CAST(substr(id, 4) AS INTEGER) DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_contract(r) for r in rows]

    # -- idempotency -----------------------------------------------------
    def get_idempotent_response(self, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT response_json FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
        return json.loads(row["response_json"]) if row else None

    def save_idempotent_response(self, key: str, operation_id: str, response: dict[str, Any], created_at: str) -> None:
        self.conn.execute(
            "INSERT INTO idempotency_keys(key, operation_id, response_json, created_at) VALUES (?,?,?,?)",
            (key, operation_id, json.dumps(response, default=str, ensure_ascii=False), created_at),
        )

    def close(self) -> None:
        self.conn.close()
