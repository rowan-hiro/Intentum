"""Persistent entities owned by the backend.

Agents never construct these directly; the backend creates them while
executing semantic operations, and returns agent-friendly projections.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LogicalType(StrEnum):
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"
    DATE = "date"
    TIMESTAMP = "timestamp"
    UNKNOWN = "unknown"

    @property
    def is_numeric(self) -> bool:
        return self in (LogicalType.INTEGER, LogicalType.FLOAT)

    @property
    def is_temporal(self) -> bool:
        return self in (LogicalType.DATE, LogicalType.TIMESTAMP)


class SemanticRole(StrEnum):
    IDENTIFIER = "identifier"
    DIMENSION = "dimension"
    MEASURE = "measure"
    TIME = "time"
    UNKNOWN = "unknown"


class DatasetStatus(StrEnum):
    ACTIVE = "active"
    PUBLISHED = "published"
    DELETED = "deleted"


class OperationKind(StrEnum):
    IMPORT = "import_dataset"
    IMPORT_WORKSPACE = "import_workspace"
    TRANSFORM = "transform_dataset"
    MATERIALIZE = "materialize_result"
    PUBLISH = "publish_dataset"
    UPDATE_METADATA = "update_metadata"
    ATTACH_METADATA = "attach_metadata"
    DELETE = "delete_dataset"
    RESTORE = "restore_dataset"
    EXPORT = "export_result"
    DECLARE_OUTPUT = "declare_output"


class OperationStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class Relationship(StrEnum):
    DERIVED_FROM = "derived_from"
    JOINED_WITH = "joined_with"


class ArtifactKind(StrEnum):
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"
    SQLITE = "sqlite"
    MARKDOWN = "markdown"
    TEXT = "text"
    PDF = "pdf"
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    OTHER = "other"

    @property
    def is_tabular(self) -> bool:
        return self in (ArtifactKind.CSV, ArtifactKind.JSON, ArtifactKind.PARQUET, ArtifactKind.SQLITE)


class _Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class Column(_Entity):
    id: str
    dataset_id: str
    name: str
    logical_type: LogicalType
    physical_type: str
    description: str = ""
    semantic_role: SemanticRole = SemanticRole.UNKNOWN
    aliases: list[str] = Field(default_factory=list)
    unit: str = ""
    position: int = 0


class Dataset(_Entity):
    id: str
    name: str
    description: str = ""
    status: DatasetStatus = DatasetStatus.ACTIVE
    version: int = 1
    created_at: datetime
    updated_at: datetime
    physical_location: str
    origin: str  # "import" | "materialize" | "restore"
    metadata: dict[str, Any] = Field(default_factory=dict)
    deleted_at: datetime | None = None
    source_artifact_id: str | None = None
    source_locator: str | None = None  # e.g. a table name inside a SQLite artifact
    columns: list[Column] = Field(default_factory=list)

    @property
    def aliases(self) -> list[str]:
        raw = self.metadata.get("aliases", [])
        return [str(a) for a in raw] if isinstance(raw, list) else []

    @property
    def is_deleted(self) -> bool:
        return self.status == DatasetStatus.DELETED


class Artifact(_Entity):
    """An external or managed file the backend knows about.

    Tabular artifacts become datasets; documents and media are registered so
    lineage and metadata can point at them (for example a knowledge document
    that describes a dataset, or a PDF a table was extracted from).
    """

    id: str
    kind: ArtifactKind
    name: str
    path: str
    managed_path: str | None = None
    content_hash: str
    size_bytes: int
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetVersion(_Entity):
    id: str
    dataset_id: str
    version: int
    physical_table: str
    row_count: int
    columns_snapshot: list[dict[str, Any]]
    operation_id: str | None
    created_at: datetime


class LineageEdge(_Entity):
    id: str
    source_dataset_id: str
    source_version: int
    target_dataset_id: str
    target_version: int
    operation_id: str
    relationship: Relationship
    created_at: datetime


class Operation(_Entity):
    id: str
    kind: OperationKind
    status: OperationStatus
    original_intent: dict[str, Any]
    canonical_ir: dict[str, Any] | None = None
    execution_plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    idempotency_key: str | None = None
    principal: str | None = None
    parent_operation_id: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ContractStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"


class RowCardinality(StrEnum):
    ONE = "one"
    AT_LEAST_ONE = "at_least_one"
    ONE_PER = "one_per"


class ContractColumn(_Entity):
    name: str
    logical_type: LogicalType | None = None


class ContractOrder(_Entity):
    """A column the deliverable is ordered by, whether or not it carries it."""

    name: str
    descending: bool = False


class OutputContract(_Entity):
    """The declared shape of the deliverable an agent is working towards.

    Declared while the requirement is fresh, held by the backend, and checked
    against every export (MADR 0007). ``revision`` counts explicit amendments;
    ``satisfied_by`` names the latest export that matched it.

    The shape has two halves. ``columns`` is what the deliverable *carries*, in
    order. ``order_by`` and ``row_keys`` are what it is *organized by*: they may
    name columns the deliverable does not carry, and those columns are expected
    in the dataset at export so the backend can order and count by them, then
    left out of the file (MADR 0012).
    """

    id: str
    status: ContractStatus = ContractStatus.OPEN
    columns: list[ContractColumn]
    rows: RowCardinality | None = None
    row_keys: list[str] = Field(default_factory=list)
    order_by: list[ContractOrder] = Field(default_factory=list)
    description: str = ""
    revision: int = 1
    operation_id: str
    satisfied_by: str | None = None
    dataset_id: str | None = None
    dataset_version: int | None = None
    created_at: datetime
    updated_at: datetime


class AuditEvent(_Entity):
    id: str
    operation_id: str | None
    event_type: str
    entity_type: str
    entity_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    created_at: datetime
