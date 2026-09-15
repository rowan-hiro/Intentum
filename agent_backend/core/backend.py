"""The agent-ready backend: semantic operations over a deterministic core.

Every public method takes loose intent, runs the pipeline
resolve → canonical IR → validate → plan → execute → commit → audit,
and returns a structured, agent-friendly response. Errors never escape as
exceptions; they are rendered as structured error responses.
"""

from __future__ import annotations

import datetime as dt
import functools
import inspect
import json
import math
import os
import re
import stat
import threading
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field as dc_field
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterator

from pydantic import ValidationError

from ..storage.duckdb.engine import SUPPORTED_FORMATS, AnalyticsEngine, DuckDBEngine, TableSource, physical_to_logical
from ..storage.duckdb.sandbox import QuerySandbox
from ..storage.files.workspace import Workspace
from ..storage.metadata.interface import MetadataStore
from ..storage.metadata.sqlite import SqliteMetadataStore
from .recovery import Advice, advise_contract, advise_empty_result, advise_error, call as tool_call
from .access import AccessPolicy, AllowAllPolicy
from .audit import AuditService
from .contracts import (
    ContractSpec,
    contract_summary,
    organizing_keys,
    parse_contract,
    repair_transform,
    verify_columns,
    verify_rows,
)
from .errors import (
    AmbiguousReferenceError,
    BackendError,
    ConflictError,
    ContractMismatchError,
    ErrorCode,
    ExecutionFailedError,
    InvalidIntentError,
    InvalidSchemaError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
)
from .execution import Executor
from .export import ExportFormat, ValueRenderer, write_formatted_csv
from .ir import (AggregateStep, DeriveStep, FilterStep, ImportIR, JoinStep, LimitStep, OutputMode, RawQueryStep, RenameStep,
                 SelectStep, SemiJoinStep, SortStep, TransformIR)
from .knowledge import ColumnFact, KnowledgeDocument, TableFact, parse_knowledge_markdown
from .lineage import LineageService
from .logging import log_event
from .models.entities import (
    ARTIFACT_KINDS,
    Artifact,
    ArtifactKind,
    Column,
    ContractStatus,
    Dataset,
    DatasetStatus,
    DatasetVersion,
    LogicalType,
    Operation,
    OperationKind,
    OperationStatus,
    OutputContract,
    Relationship,
    RowCardinality,
    SemanticRole,
)
from .naming import normalize, slugify, tokens
from .planner import ExecutionPlan, Planner, PlanStep
from .planner.planner import _expr_text
from .resolver import DatasetResolver, FieldResolver, ResolutionNote, Scope, ScopeField, TransformResolver
from .validation import IRValidator

Clock = Callable[[], dt.datetime]

FORMAT_BY_KIND = {ArtifactKind.CSV: "csv", ArtifactKind.JSON: "json", ArtifactKind.PARQUET: "parquet", ArtifactKind.SQLITE: "sqlite"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _finite(value: Any) -> bool:
    """A range bound worth reporting: present, and not an infinity or nan."""
    return value is not None and not (isinstance(value, float) and not math.isfinite(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # JSON has no inf or nan; the MCP layer would otherwise send null
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


# The semantic operation in progress: its tool name and its arguments as the caller wrote them.
# Read by _advise so that a refusal raised anywhere inside the operation can be taught (MADR 0010).
_CURRENT_CALL: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar("intentum_current_call", default=None)


def _written_arguments(signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """The call's arguments as written: what the caller passed, without defaults, self or principal."""
    try:
        bound = signature.bind_partial(None, *args, **kwargs)
    except TypeError:
        return {}
    return _jsonable({name: value for name, value in bound.arguments.items() if name not in ("self", "principal")})


def _advise(err: BackendError) -> None:
    """Teach on refusal: attach advice for the current operation when the raise site attached none."""
    if err.advice:
        return
    current = _CURRENT_CALL.get()
    if current is None:
        return
    tool, arguments = current
    try:
        err.advice = advise_error(err, tool=tool, arguments=arguments)
    except Exception:  # advice must never break a response
        err.advice = []


def semantic_operation(action: str):
    """Wrap a backend method: one operation at a time, access check, advice on refusal, structured error rendering.

    Operations are serialized on the backend's lock. An agent may send several
    tool calls in one step and the MCP server runs them on worker threads, while
    the backend holds one SQLite connection and one DuckDB connection, neither
    of which tolerates concurrent use. The lock is re-entrant so an operation
    may call another.
    """

    def decorator(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(self: "Backend", *args: Any, **kwargs: Any) -> dict[str, Any]:
            with self._lock:
                return _run_operation(self, fn, signature, action, args, kwargs)

        return wrapper

    return decorator


def _run_operation(self: "Backend", fn, signature: inspect.Signature, action: str, args: tuple[Any, ...],
                   kwargs: dict[str, Any]) -> dict[str, Any]:
    principal = kwargs.get("principal")
    token = _CURRENT_CALL.set((action, _written_arguments(signature, args, kwargs)))
    try:
        self.policy.check(principal, action, {"args": _jsonable(kwargs)})
        return fn(self, *args, **kwargs)
    except BackendError as err:
        _advise(err)
        log_event("operation.error", code=str(err.code), message=err.message, field=err.field, action=action,
                  advice=[getattr(a, "kind", None) for a in err.advice])
        return err.to_response()
    except ValidationError as err:
        log_event("operation.error", code="INVALID_INTENT", message=str(err), action=action)
        return InvalidIntentError(
            "The request did not match the expected shape.",
            details={"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in err.errors()]},
        ).to_response()
    except Exception as err:  # pragma: no cover - defensive
        log_event("operation.error", code="INTERNAL", message=repr(err), action=action)
        return BackendError(f"Internal error: {err!r}", code=ErrorCode.INTERNAL, recoverable=False).to_response()
    finally:
        _CURRENT_CALL.reset(token)


def _path_facts(path: Path) -> dict[str, Any]:
    """Where a path that does not exist was looked for: the directory it resolved against, and what the nearest
    existing directory above it holds, so the caller can see the spelling or the root it meant."""
    resolved = path.resolve()
    nearest = resolved
    while not nearest.is_dir() and nearest != nearest.parent:
        nearest = nearest.parent
    try:
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in nearest.iterdir() if not p.name.startswith("."))[:30]
    except OSError:
        entries = []
    return {"resolved": str(resolved), "cwd": str(Path.cwd()), "nearest_directory": str(nearest), "entries": entries}


@dataclass
class _ImportSpec:
    """Everything needed to turn one tabular source into a dataset."""

    artifact: Artifact
    source: TableSource
    name: str
    description: str
    hints: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    aliases: list[str] = dc_field(default_factory=list)
    parent_operation_id: str | None = None

    @property
    def locator(self) -> str | None:
        return self.source.table

    def describe(self) -> str:
        return f"{self.artifact.name}::{self.locator}" if self.locator else self.artifact.name


class Backend:
    def __init__(
        self,
        workspace: Workspace | str | Path,
        *,
        clock: Clock | None = None,
        policy: AccessPolicy | None = None,
        store: MetadataStore | None = None,
        engine: AnalyticsEngine | None = None,
        export_root: str | Path | None = None,
        query_timeout: float | None = None,
    ) -> None:
        self._lock = threading.RLock()  # one semantic operation at a time; see semantic_operation
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        # Files may only be exported under this directory; None disables the guard (library use).
        self.export_root = Path(export_root).expanduser().resolve() if export_root is not None else None
        # Seconds one raw_query statement may run before it is interrupted; None runs without a deadline.
        if query_timeout is not None and (isinstance(query_timeout, bool) or not isinstance(query_timeout, (int, float))
                                          or query_timeout <= 0):
            raise ValueError(f"query_timeout must be a positive number of seconds or None, got {query_timeout!r}")
        self.query_timeout = float(query_timeout) if query_timeout is not None else None
        self.clock = clock or _utcnow
        self.policy = policy or AllowAllPolicy()
        self.store = store or SqliteMetadataStore(self.workspace.metadata_path)
        self.engine = engine or DuckDBEngine(self.workspace.analytics_path)
        # raw_query statements are parsed, described and run in a sandbox beside the engine (MADR 0002).
        self.queries = QuerySandbox(self.engine, timeout=self.query_timeout) if isinstance(self.engine, DuckDBEngine) else None
        self.datasets = DatasetResolver(self.store, self.clock)
        self.fields = FieldResolver()
        self.transforms = TransformResolver(self.datasets, self.fields, queries=self.queries)
        self.validator = IRValidator(self.store, queries=self.queries)
        self.planner = Planner()
        self.executor = Executor(self.engine, queries=self.queries)
        self.lineage = LineageService(self.store)
        self.audit = AuditService(self.store)

    def close(self) -> None:
        self.store.close()
        self.engine.close()

    # ======================================================================
    # Read operations
    # ======================================================================
    @semantic_operation("list_datasets")
    def list_datasets(self, *, include_deleted: bool = False, principal: str | None = None) -> dict[str, Any]:
        datasets = self.store.list_datasets(include_deleted=include_deleted)
        return {"status": "success", "datasets": [self._dataset_summary(d) for d in datasets], "count": len(datasets)}

    @semantic_operation("list_artifacts")
    def list_artifacts(self, *, kind: str | None = None, principal: str | None = None) -> dict[str, Any]:
        artifacts = self.store.list_artifacts()
        if kind is not None:
            try:
                wanted = ArtifactKind(str(kind).lower())
            except ValueError as exc:
                raise InvalidIntentError(f"Unknown artifact kind {kind!r}.", field="kind",
                                         details={"allowed": [str(k) for k in ArtifactKind]}) from exc
            artifacts = [a for a in artifacts if a.kind == wanted]
        datasets_by_artifact: dict[str, list[str]] = {}
        for d in self.store.list_datasets():
            if d.source_artifact_id:
                datasets_by_artifact.setdefault(d.source_artifact_id, []).append(d.name)
        return {
            "status": "success",
            "artifacts": [{**self._artifact_summary(a), "datasets": datasets_by_artifact.get(a.id, [])} for a in artifacts],
            "count": len(artifacts),
        }

    # Columns beyond which describe_dataset skips the per-column profile: one scan with four aggregates per column.
    PROFILE_COLUMN_BOUND = 300

    @semantic_operation("describe_dataset")
    def describe_dataset(self, dataset: Any, *, sample_rows: int = 5, profile: bool = True,
                         principal: str | None = None) -> dict[str, Any]:
        notes: list[ResolutionNote] = []
        try:
            ds = self.datasets.resolve(dataset, field="dataset", notes=notes)
        except BackendError as err:
            err.advice = advise_error(err, tool="describe_dataset", arguments={"dataset": dataset})
            raise
        version = self.store.get_version(ds.id, ds.version)
        versions = self.store.list_versions(ds.id)
        upstream = self.lineage.upstream(ds.id, depth=1)
        downstream = self.lineage.downstream(ds.id, depth=1)
        sample: dict[str, Any] | None = None
        if sample_rows > 0 and version is not None:
            cols, rows = self.engine.sample(version.physical_table, min(sample_rows, 100))
            sample = {"columns": cols, "rows": _jsonable([list(r) for r in rows])}
        schema = [self._column_summary(c) for c in ds.columns]
        profiled: str | None = None
        if profile and version is not None and len(ds.columns) <= self.PROFILE_COLUMN_BOUND:
            # What the data holds, column by column: repetition (distinct against rows), gaps (non-null against
            # rows) and the range, so a count of entities or a filter on a range needs no exploratory query.
            ranged = [c.name for c in ds.columns if c.logical_type.is_numeric or c.logical_type in (LogicalType.DATE, LogicalType.TIMESTAMP)]
            facts = self.engine.profile_columns(version.physical_table, [c.name for c in ds.columns], ranged=ranged)
            for entry in schema:
                column_facts = facts.get(entry["name"], {})
                if not all(_finite(column_facts.get(k)) for k in ("min", "max")):
                    column_facts = {k: v for k, v in column_facts.items() if k not in ("min", "max")}  # no finite range
                entry.update(_jsonable(column_facts))
            profiled = "non_null and distinct count every column's values; min and max are the range of numeric and temporal columns"
        elif profile and version is not None:
            profiled = f"skipped: {len(ds.columns)} columns, more than {self.PROFILE_COLUMN_BOUND}; aggregate counts the ones needed"
        elif profile:
            profiled = "skipped: the dataset has no physical version to scan"
        body = {
            "status": "success",
            "dataset": {
                **self._dataset_summary(ds),
                "physical_location": ds.physical_location,
                "metadata": ds.metadata,
            },
            "schema": schema,
            "row_count": version.row_count if version else None,
            "versions": [
                {"version": v.version, "rows": v.row_count, "created_at": v.created_at.isoformat(), "operation_id": v.operation_id}
                for v in versions[:5]
            ],
            "lineage": {
                "upstream": [{"id": e["source"], "name": e["source_name"], "operation_id": e["operation_id"]} for e in upstream],
                "downstream": [{"id": e["target"], "name": e["target_name"], "operation_id": e["operation_id"]} for e in downstream],
            },
            "hints": self._semantic_hints(ds),
        }
        source = self._source_summary(ds)
        if source is not None:
            body["source"] = source
        if sample is not None:
            body["sample"] = sample
        if profiled is not None:
            body["profile"] = profiled
        return self._with_notes(body, notes)

    @semantic_operation("search_datasets")
    def search_datasets(self, query: str, *, limit: int = 10, principal: str | None = None) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise InvalidIntentError("query must be a non-empty string.", field="query")
        words = [w for w in tokens(query)]
        results: list[tuple[float, Dataset, list[str]]] = []
        for d in self.store.list_datasets():
            score, reasons = 0.0, []
            haystacks = {
                "name": set(tokens(d.name)),
                "alias": set(t for a in d.aliases for t in tokens(a)),
                "description": set(tokens(d.description)),
                "column": set(t for c in d.columns for t in tokens(c.name) + [t2 for a in c.aliases for t2 in tokens(a)]),
                "column_description": set(t for c in d.columns for t in tokens(c.description)),
                "metadata": set(t for v in d.metadata.values() if isinstance(v, (str, int, float)) for t in tokens(str(v))),
            }
            weights = {"name": 3, "alias": 3, "description": 1.5, "column": 1, "column_description": 0.5, "metadata": 1}
            for w in words:
                for kind, bag in haystacks.items():
                    if w in bag or any(w in t for t in bag if len(w) >= 3):
                        score += weights[kind]
                        reasons.append(f"{w} in {kind}")
                        break
            if score > 0:
                results.append((score, d, reasons))
        results.sort(key=lambda r: (-r[0], r[1].name))
        return {
            "status": "success",
            "query": query,
            "results": [
                {**self._dataset_summary(d), "score": score, "matched": sorted(set(reasons))}
                for score, d, reasons in results[:limit]
            ],
            "count": len(results),
        }

    @semantic_operation("get_provenance")
    def get_provenance(self, dataset: Any, *, principal: str | None = None) -> dict[str, Any]:
        notes: list[ResolutionNote] = []
        ds = self.datasets.resolve(dataset, field="dataset", notes=notes)
        version = self.store.get_version(ds.id, ds.version)
        op = self.store.get_operation(version.operation_id) if version and version.operation_id else None
        upstream = self.lineage.upstream(ds.id)
        inputs = [
            {"id": e["source"], "name": e["source_name"], "version": e["source_version"], "relationship": e["relationship"]}
            for e in upstream
            if e["target"] == ds.id
        ]
        body: dict[str, Any] = {
            "status": "success",
            "dataset": self._dataset_summary(ds),
            "produced_by": self._operation_summary(op) if op else None,
            "inputs": inputs,
            "source": self._source_summary(ds),
            "upstream": upstream,
            "audit": self.audit.for_entity(ds.id),
        }
        if op and op.canonical_ir:
            body["canonical_intent"] = op.canonical_ir
        return self._with_notes(body, notes)

    @semantic_operation("get_output_contract")
    def get_output_contract(self, *, contract_id: str | None = None, principal: str | None = None) -> dict[str, Any]:
        """The output contract the workspace currently holds (or one by id), with its history."""
        if contract_id is not None:
            contract = self.store.get_contract(str(contract_id))
            if contract is None:
                raise NotFoundError(f"Output contract {contract_id!r} does not exist.", field="contract_id",
                                    candidates=[c.id for c in self.store.list_contracts(limit=10)])
        else:
            contract = self.store.latest_contract()
        if contract is None:
            return {"status": "success", "contract": None,
                    "summary": "No output contract has been declared in this workspace."}
        return {
            "status": "success",
            "contract": contract_summary(contract),
            "history": self.audit.for_entity(contract.id),
            "summary": self._contract_sentence(contract),
        }

    @semantic_operation("get_operation")
    def get_operation(self, operation_id: str, *, principal: str | None = None) -> dict[str, Any]:
        op = self.store.get_operation(operation_id)
        if op is None:
            raise NotFoundError(f"Operation {operation_id!r} does not exist.", field="operation_id")
        body = self._operation_summary(op)
        body.update({"status": "success", "operation": body.pop("status"), "canonical_ir": op.canonical_ir,
                     "execution_plan": op.execution_plan, "result": op.result, "error": op.error,
                     "audit": self.audit.for_operation(op.id)})
        return body

    # ======================================================================
    # import_dataset / import_workspace
    # ======================================================================
    @semantic_operation("import_dataset")
    def import_dataset(
        self,
        path: str,
        *,
        name: str | None = None,
        description: str | None = None,
        format: str | None = None,
        table: str | None = None,
        schema_hints: dict[str, Any] | None = None,
        aliases: list[str] | None = None,
        idempotency_key: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        source = Path(str(path)).expanduser()
        if not source.is_file():
            raise NotFoundError(f"File {path!r} does not exist or is not a file.", field="path", recoverable=True,
                                details=_path_facts(source))
        kind = self._classify(source, format)
        if not kind.is_tabular:
            # What was received (the kind), what is accepted (the formats), and who reads the rest (MADR 0009).
            raise InvalidSchemaError(
                f"Unsupported format {(format or source.suffix.lstrip('.')).lower()!r}; supported formats are "
                f"{', '.join(SUPPORTED_FORMATS)}.",
                field="format",
                details={"kind": str(kind), "allowed_formats": list(SUPPORTED_FORMATS),
                         "reader": "attach_metadata" if kind.is_document else None},
            )
        fmt = FORMAT_BY_KIND[kind]
        hints = self._normalize_hints(schema_hints)
        artifact = self._register_artifact(source, kind)
        locator: str | None = None
        if fmt == "sqlite":
            tables = self.engine.list_container_tables(Path(artifact.managed_path or artifact.path), fmt)
            if table is not None:
                match = next((t for t in tables if t == table), None) or next((t for t in tables if t.lower() == str(table).lower()), None)
                if match is None:
                    raise NotFoundError(f"Table {table!r} not found in {source.name}.", field="table", candidates=tables)
                locator = match
            elif len(tables) == 1:
                locator = tables[0]
            elif not tables:
                raise InvalidSchemaError(f"{source.name} contains no tables.", field="path")
            else:
                raise AmbiguousReferenceError(
                    f"{source.name} contains {len(tables)} tables; pass table=<name> or use import_workspace to import them all.",
                    field="table",
                    candidates=[{"name": t} for t in tables],
                )
        elif table is not None:
            raise InvalidIntentError("table applies to SQLite sources only.", field="table")
        logical_name = slugify(name or (locator if locator else source.stem))
        spec = _ImportSpec(
            artifact=artifact,
            source=TableSource(path=Path(artifact.managed_path or artifact.path), format=fmt, table=locator),
            name=logical_name,
            description=description or "",
            hints=hints,
            aliases=[str(a) for a in (aliases or [])],
        )
        intent = _jsonable({"path": path, "name": name, "description": description, "format": format, "table": table,
                            "schema_hints": schema_hints, "aliases": aliases})
        return self._import_source(spec, intent, idempotency_key, principal)

    @semantic_operation("import_workspace")
    def import_workspace(
        self,
        path: str,
        *,
        description: str | None = None,
        include_documents: bool = True,
        principal: str | None = None,
    ) -> dict[str, Any]:
        root = Path(str(path)).expanduser()
        if not root.is_dir():
            raise NotFoundError(f"Directory {path!r} does not exist.", field="path", details=_path_facts(root))
        intent = _jsonable({"path": path, "description": description, "include_documents": include_documents})
        files = sorted(p for p in root.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts))

        with self._operation(OperationKind.IMPORT_WORKSPACE, intent, principal) as op:
            artifacts: list[Artifact] = []
            skipped: list[str] = []
            for file in files:
                kind = self._classify(file, None)
                if not kind.is_tabular and not include_documents:
                    skipped.append(str(file.relative_to(root)))
                    continue
                artifacts.append(self._register_artifact(file, kind, relative_to=root))
            log_event("resolution.completed", operation_id=op.id, artifacts=len(artifacts), skipped=len(skipped))

            # Plan: one source per tabular file, or per table for containers.
            candidates: list[tuple[Artifact, TableSource, str]] = []  # artifact, source, base name
            failed: list[dict[str, Any]] = []
            for artifact in artifacts:
                if not artifact.kind.is_tabular:
                    continue
                fmt = FORMAT_BY_KIND[artifact.kind]
                managed = Path(artifact.managed_path or artifact.path)
                if fmt == "sqlite":
                    try:
                        tables = self.engine.list_container_tables(managed, fmt)
                    except BackendError as err:
                        failed.append({"source": artifact.name, "code": str(err.code), "message": err.message})
                        continue
                    for table in tables:
                        candidates.append((artifact, TableSource(path=managed, format=fmt, table=table), table))
                else:
                    candidates.append((artifact, TableSource(path=managed, format=fmt), Path(artifact.name).stem))
            names = self._assign_names(candidates, root)
            plan = ExecutionPlan(
                steps=[
                    PlanStep("ScanWorkspace", f"ScanWorkspace({root.name})", {"files": len(files)}),
                    PlanStep("RegisterArtifacts", f"RegisterArtifacts({len(artifacts)})"),
                    PlanStep("ImportSources", f"ImportSources({len(candidates)})", {"sources": [n for n in names]}),
                    PlanStep("Audit", "Audit(workspace.imported)"),
                ],
                physical_inputs={},
                output_table=None,
            )
            op.canonical_ir = {
                "operation": "import_workspace",
                "root": str(root),
                "sources": [
                    {"artifact_id": a.id, "path": str(s.path), "format": s.format, "locator": s.table, "name": n}
                    for (a, s, _), n in zip(candidates, names)
                ],
            }
            op.execution_plan = plan.to_dict()
            self._save_operation(op)
            log_event("plan.created", operation_id=op.id, plan=plan.to_text())

            imported: list[dict[str, Any]] = []
            replayed: list[str] = []
            for (artifact, source, _base), name in zip(candidates, names):
                spec = _ImportSpec(
                    artifact=artifact, source=source, name=name, description="", parent_operation_id=op.id,
                )
                child_intent = {"workspace_operation_id": op.id, "artifact_id": artifact.id, "locator": source.table, "name": name}
                try:
                    result = self._import_source(spec, child_intent, None, principal)
                except BackendError as err:
                    failed.append({"source": spec.describe(), "name": name, "code": str(err.code), "message": err.message})
                    continue
                entry = {**result["dataset"], "source": spec.describe(), "operation_id": result["operation_id"]}
                if result.get("idempotent_replay"):
                    replayed.append(name)
                imported.append(entry)

            now = self.clock()
            with self.store.transaction():
                self.audit.record(event_type="workspace.imported", entity_type="workspace", entity_id=str(root),
                                  operation_id=op.id, now=now, actor=principal,
                                  details={"artifacts": len(artifacts), "datasets": len(imported), "failed": len(failed)})
                response = {
                    "status": "success" if not failed else "partial",
                    "operation_id": op.id,
                    "workspace": str(root),
                    "datasets": imported,
                    "artifacts": [self._artifact_summary(a) for a in artifacts],
                    "failed": failed,
                    "replayed": replayed,
                    "skipped": skipped,
                    "summary": (
                        f"Imported {len(imported)} dataset(s) from {len(artifacts)} artifact(s) in {root.name}"
                        + (f"; {len(replayed)} already existed" if replayed else "")
                        + (f"; {len(failed)} source(s) failed" if failed else "")
                        + "."
                    ),
                    "plan": plan.to_text(),
                }
                if description:
                    response["description"] = description
                self._complete(op, response, None, None, now)
            log_event("state.committed", operation_id=op.id, datasets=len(imported), failed=len(failed))
            return response

    def _assign_names(self, candidates: list[tuple[Artifact, TableSource, str]], root: Path) -> list[str]:
        """Deterministic, collision-free logical names for a batch of sources.

        Every member of a colliding group is qualified (never just the second
        one), so names do not depend on discovery order.
        """
        names = [slugify(base) for _, _, base in candidates]
        for level in (1, 2):
            counts = Counter(names)
            for index, (artifact, source, base) in enumerate(candidates):
                if counts[names[index]] <= 1:
                    continue
                stem = Path(artifact.name).stem
                if level == 1:
                    qualifier = stem if source.table else Path(artifact.name).suffix.lstrip(".")
                    names[index] = slugify(f"{stem}__{source.table}") if source.table else slugify(f"{base}__{qualifier}")
                else:
                    parent = Path(artifact.path).parent.name or "root"
                    names[index] = slugify(f"{parent}__{names[index]}")
        return names

    def _import_source(
        self,
        spec: _ImportSpec,
        intent: dict[str, Any],
        idempotency_key: str | None,
        principal: str | None,
    ) -> dict[str, Any]:
        ir = ImportIR(
            source_path=spec.artifact.path, format=spec.source.format, locator=spec.locator, name=spec.name,
            description=spec.description, content_hash=spec.artifact.content_hash, column_hints=spec.hints,
        )
        key = idempotency_key or f"import:{ir.logical_fingerprint()}"
        replay = self._replay(key, ir.logical_fingerprint())
        if replay is not None:
            return replay

        existing = self.store.get_dataset_by_name(spec.name)
        if existing is not None:
            raise ConflictError(
                f"A dataset named {spec.name!r} already exists ({existing.id}) with different content.",
                field="name",
                details={"existing": self._dataset_summary(existing)},
                hint="Choose another name, or delete_dataset the existing one first.",
            )

        with self._operation(OperationKind.IMPORT, intent, principal, key, parent=spec.parent_operation_id) as op:
            log_event("resolution.completed", operation_id=op.id, name=spec.name, source=spec.describe(),
                      content_hash=spec.artifact.content_hash)
            op.canonical_ir = ir.model_dump(mode="json")
            physical_columns = self.engine.inspect_source(spec.source)
            if not physical_columns:
                raise InvalidSchemaError(f"{spec.describe()} has no columns.", field="path")
            self._check_column_names([c for c, _ in physical_columns])
            dataset_id = self.store.allocate_id("ds")
            table = f"{dataset_id}_v1"
            plan = ExecutionPlan(
                steps=[
                    PlanStep("InspectSource", f"InspectSource({spec.describe()})", {"columns": [c for c, _ in physical_columns]}),
                    PlanStep("LoadTable", f"LoadTable({table})", {"format": spec.source.format, "locator": spec.locator}),
                    PlanStep("RegisterDataset", f"RegisterDataset({spec.name} as {dataset_id})"),
                    PlanStep("Audit", "Audit(dataset.created, version.created)"),
                ],
                physical_inputs={},
                output_table=table,
            )
            op.execution_plan = plan.to_dict()
            self._save_operation(op)
            log_event("plan.created", operation_id=op.id, plan=plan.to_text())

            try:
                row_count = self.engine.import_source(spec.source, table)
                notes: list[ResolutionNote] = []
                refined = self._refine_temporal_columns(table, physical_columns, spec.hints, notes)
                if refined:
                    physical_columns = self.engine.describe_table(table)
                    ir = ir.model_copy(update={"refined_types": {name: t.lower() for name, t in refined.items()}})
                    op.canonical_ir = ir.model_dump(mode="json")
                    plan.steps.insert(2, PlanStep("RefineTypes", f"RefineTypes({', '.join(f'{n}:{t.lower()}' for n, t in refined.items())})",
                                                  {"columns": {n: t.lower() for n, t in refined.items()}}))
                    op.execution_plan = plan.to_dict()
                    log_event("ir.canonical", operation_id=op.id, refined_types=refined)
                log_event("execution.completed", operation_id=op.id, table=table, rows=row_count)
                now = self.clock()
                with self.store.transaction():
                    columns = self._build_columns(dataset_id, physical_columns, spec.hints)
                    metadata: dict[str, Any] = {
                        "source_file": spec.artifact.name,
                        "content_hash": spec.artifact.content_hash,
                        "format": spec.source.format,
                    }
                    if spec.aliases:
                        metadata["aliases"] = list(spec.aliases)
                    original = spec.locator or Path(spec.artifact.name).stem
                    if original != spec.name and original not in metadata.get("aliases", []):
                        metadata.setdefault("aliases", []).append(original)
                    dataset = Dataset(
                        id=dataset_id, name=spec.name, description=spec.description, status=DatasetStatus.ACTIVE,
                        version=1, created_at=now, updated_at=now, physical_location=f"duckdb://{table}", origin="import",
                        metadata=metadata, columns=columns, source_artifact_id=spec.artifact.id, source_locator=spec.locator,
                    )
                    self.store.insert_dataset(dataset)
                    self.store.insert_columns(columns)
                    self.store.insert_version(DatasetVersion(
                        id=self.store.allocate_id("v"), dataset_id=dataset_id, version=1, physical_table=table,
                        row_count=row_count, columns_snapshot=[self._column_summary(c) for c in columns],
                        operation_id=op.id, created_at=now,
                    ))
                    self.audit.record(event_type="dataset.created", entity_type="dataset", entity_id=dataset_id,
                                      operation_id=op.id, now=now, actor=principal,
                                      details={"name": spec.name, "origin": "import", "rows": row_count,
                                               "artifact_id": spec.artifact.id, "locator": spec.locator})
                    self.audit.record(event_type="version.created", entity_type="dataset", entity_id=dataset_id,
                                      operation_id=op.id, now=now, actor=principal, details={"version": 1, "table": table})
                    response = {
                        "status": "success",
                        "operation_id": op.id,
                        "dataset": {"id": dataset_id, "name": spec.name, "rows": row_count,
                                    "columns": [c.name for c in columns], "version": 1},
                        "schema": [self._column_summary(c) for c in columns],
                        "source": {"artifact_id": spec.artifact.id, "name": spec.artifact.name,
                                   "kind": str(spec.artifact.kind), "locator": spec.locator},
                        "summary": f"Imported {spec.describe()} as {spec.name} ({row_count} rows, {len(columns)} columns).",
                    }
                    response = self._with_notes(response, notes)
                    self._complete(op, response, key, ir.logical_fingerprint(), now)
            except Exception:
                self.executor.rollback_table(table)
                raise
            log_event("state.committed", operation_id=op.id, dataset_id=dataset_id)
            return response

    # ======================================================================
    # transform_dataset / materialize_result
    # ======================================================================
    @semantic_operation("transform_dataset")
    def transform_dataset(
        self,
        source: Any,
        transform: Any,
        *,
        output_name: str | None = None,
        description: str | None = None,
        preview_limit: int = 20,
        context: dict[str, Any] | None = None,
        explain: bool = False,
        idempotency_key: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        if output_name:
            return self._run_transform(OperationKind.MATERIALIZE, source, transform, output_name, description,
                                       preview_limit, context, explain, idempotency_key, principal, tool="transform_dataset")
        return self._run_transform(OperationKind.TRANSFORM, source, transform, None, None,
                                   preview_limit, context, explain, idempotency_key, principal, tool="transform_dataset")

    @semantic_operation("materialize_result")
    def materialize_result(
        self,
        source: Any,
        transform: Any,
        name: str,
        *,
        description: str | None = None,
        preview_limit: int = 20,
        context: dict[str, Any] | None = None,
        explain: bool = False,
        idempotency_key: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise InvalidIntentError("materialize_result needs a non-empty name.", field="name")
        return self._run_transform(OperationKind.MATERIALIZE, source, transform, name, description,
                                   preview_limit, context, explain, idempotency_key, principal, tool="materialize_result")

    def _run_transform(
        self,
        kind: OperationKind,
        source: Any,
        transform: Any,
        output_name: str | None,
        description: str | None,
        preview_limit: int,
        context: dict[str, Any] | None,
        explain: bool,
        idempotency_key: str | None,
        principal: str | None,
        tool: str = "transform_dataset",
    ) -> dict[str, Any]:
        intent = _jsonable({"source": source, "transform": transform, "output_name": output_name,
                            "description": description, "context": context})
        if not isinstance(preview_limit, int) or preview_limit < 0 or preview_limit > 1000:
            raise InvalidIntentError("preview_limit must be an integer between 0 and 1000.", field="preview_limit")
        notes: list[ResolutionNote] = []
        # The request as the agent wrote it, for advice that rewrites it (MADR 0010).
        call_arguments: dict[str, Any] = {"source": source, "transform": transform}
        if tool == "materialize_result":
            call_arguments["name"] = output_name
        elif output_name:
            call_arguments["output_name"] = output_name
        if description:
            call_arguments["description"] = description
        with self._operation(kind, intent, principal, idempotency_key) as op:
            try:
                ir, used = self.transforms.resolve(
                    source=source, transform=transform, output_name=output_name, description=description,
                    preview_limit=preview_limit, context=context, notes=notes,
                )
                log_event("resolution.completed", operation_id=op.id, source=ir.source.dataset_id,
                          notes=[n.to_dict() for n in notes])
                op.canonical_ir = ir.model_dump(mode="json")
                log_event("ir.canonical", operation_id=op.id, ir=op.canonical_ir)
                self.validator.validate_transform(ir)
                log_event("validation.completed", operation_id=op.id)
            except BackendError as err:
                # Teach on refusal: what the backend accepts instead, and the request rewritten when mechanical.
                err.advice = advise_error(err, tool=tool, arguments=call_arguments)
                raise

            materialize = ir.output.mode == OutputMode.MATERIALIZED
            key = idempotency_key or (f"materialize:{ir.logical_fingerprint()}" if materialize else None)
            if key is not None:
                replay = self._replay(key, ir.logical_fingerprint(), op=op)
                if replay is not None:
                    return self._with_notes(replay, notes)
            if materialize:
                existing = self.store.get_dataset_by_name(ir.output.name or "")
                if existing is not None:
                    raise ConflictError(
                        f"A dataset named {ir.output.name!r} already exists ({existing.id}) and was produced by a different request.",
                        field="name",
                        details={"existing": self._dataset_summary(existing)},
                        hint="Choose another name, or delete_dataset the existing one first.",
                    )

            versions = {d.id: self.store.get_version(d.id, d.version) for d in used}
            missing = [d.id for d, v in zip(used, versions.values()) if v is None]
            if missing:
                raise InvalidStateError(f"Datasets {missing} have no physical version.", field="source")
            dataset_id = self.store.allocate_id("ds") if materialize else None
            table = f"{dataset_id}_v1" if materialize else None
            plan = self.planner.plan_transform(ir, versions, table, dataset_id)
            op.execution_plan = plan.to_dict()
            self._save_operation(op)
            log_event("plan.created", operation_id=op.id, plan=plan.to_text())

            result = self.executor.execute_transform(ir, plan, count_distinct=self._contract_keys(ir))
            log_event("execution.completed", operation_id=op.id, rows=result.row_count, table=result.physical_table)
            advice = self._transform_advice(ir, used, result.row_count, tool, call_arguments,
                                            ir.output.name if materialize else None, distinct_keys=result.distinct_keys)

            now = self.clock()
            response: dict[str, Any]
            try:
                with self.store.transaction():
                    if materialize:
                        response = self._commit_materialization(op, ir, used, dataset_id, table, result, now, principal)
                    else:
                        response = {
                            "status": "success",
                            "operation_id": op.id,
                            "source": {"id": ir.source.dataset_id, "name": ir.source.name, "version": ir.source.version},
                            "result": {
                                "columns": [{"name": f.name, "type": str(f.logical_type)} for f in ir.output_schema],
                                "rows": _jsonable(result.rows),
                                "row_count": result.row_count,
                                "truncated": result.truncated,
                            },
                            "summary": self._summarize(ir, None),
                            "hint": "Call materialize_result with the same source/transform and a name to persist this result.",
                        }
                    if advice:
                        response["advice"] = [a.to_dict() for a in advice]
                    response["plan"] = plan.to_text()
                    if ir.uses_raw_query:
                        # Countable: how often the semantic steps were not enough, and for which shapes (MADR 0002).
                        response["used_raw_query"] = True
                    if explain:
                        response["explain"] = {"canonical_ir": op.canonical_ir, "execution_plan": plan.to_dict(), "sql": result.sql}
                        if ir.uses_raw_query:
                            response["explain"]["used_raw_query"] = True
                    self._complete(op, response, key, ir.logical_fingerprint(), now)
            except Exception:
                self.executor.rollback_table(table)
                raise
            log_event("state.committed", operation_id=op.id, dataset_id=dataset_id)
            return self._with_notes(response, notes)

    def _contract_keys(self, ir: TransformIR) -> list[str] | None:
        """The open contract's one_per keys when the result has them all: export counts them, so the advice must too."""
        try:
            contract = self.store.latest_contract()
        except Exception as err:  # advice never breaks a response
            log_event("advice.skipped", error=repr(err))
            return None
        if contract is None or contract.status != ContractStatus.OPEN or contract.rows != RowCardinality.ONE_PER:
            return None
        names = {f.name for f in ir.output_schema}
        return list(contract.row_keys) if all(k in names for k in contract.row_keys) else None

    def _transform_advice(self, ir: TransformIR, used: list[Dataset], row_count: int, tool: str,
                          arguments: dict[str, Any], materialized_name: str | None,
                          distinct_keys: int | None = None) -> list[Advice]:
        """Advice on a successful transform: an empty result explained, or a result that fits the contract."""
        advice: list[Advice] = []
        try:
            if row_count == 0:
                found = advise_empty_result(ir, used=used, workspace=self.store.list_datasets(include_deleted=False),
                                            occurs=self._column_contains)
                if found is not None:
                    advice.append(found)
            contract = self.store.latest_contract()
            if contract is not None:
                found = advise_contract(ir, contract=contract, row_count=row_count, tool=tool, arguments=arguments,
                                        materialized_name=materialized_name, distinct_keys=distinct_keys)
                if found is not None:
                    advice.append(found)
        except Exception as err:  # advice never breaks a response
            log_event("advice.skipped", error=repr(err))
        return advice

    def _column_contains(self, dataset: Dataset, column: Column, value: Any) -> bool:
        version = self.store.get_version(dataset.id, dataset.version)
        return version is not None and self.engine.column_contains(version.physical_table, column.name, value)

    def _commit_materialization(
        self,
        op: Operation,
        ir: TransformIR,
        used: list[Dataset],
        dataset_id: str,
        table: str,
        result,
        now: dt.datetime,
        principal: str | None,
    ) -> dict[str, Any]:
        physical_types = dict(self.engine.describe_table(table))
        columns: list[Column] = []
        source_columns = {c.id: c for d in used for c in d.columns}
        for position, f in enumerate(ir.output_schema):
            origin = source_columns.get(f.column_id) if f.column_id else None
            columns.append(Column(
                id=self.store.allocate_id("col"), dataset_id=dataset_id, name=f.name,
                logical_type=f.logical_type, physical_type=physical_types.get(f.name, "UNKNOWN"),
                description=origin.description if origin else "",
                semantic_role=origin.semantic_role if origin else (SemanticRole.MEASURE if f.logical_type.is_numeric else SemanticRole.UNKNOWN),
                aliases=list(origin.aliases) if origin else [], unit=origin.unit if origin else "", position=position,
            ))
        metadata = {"derived": True, "intent_fingerprint": ir.logical_fingerprint()}
        dataset = Dataset(
            id=dataset_id, name=ir.output.name or "", description=ir.output.description, status=DatasetStatus.ACTIVE,
            version=1, created_at=now, updated_at=now, physical_location=f"duckdb://{table}", origin="materialize",
            metadata=metadata, columns=columns,
        )
        self.store.insert_dataset(dataset)
        self.store.insert_columns(columns)
        self.store.insert_version(DatasetVersion(
            id=self.store.allocate_id("v"), dataset_id=dataset_id, version=1, physical_table=table,
            row_count=result.row_count, columns_snapshot=[self._column_summary(c) for c in columns],
            operation_id=op.id, created_at=now,
        ))
        lineage_ids: list[str] = []
        # A raw_query's result derives from every dataset it reads, not only from the source.
        queried = {b.dataset.dataset_id for step in ir.steps if isinstance(step, RawQueryStep) for b in step.inputs}
        for ref in ir.referenced_datasets():
            relationship = (Relationship.DERIVED_FROM if ref.dataset_id == ir.source.dataset_id or ref.dataset_id in queried
                            else Relationship.JOINED_WITH)
            self.lineage.record(source_dataset_id=ref.dataset_id, source_version=ref.version, target_dataset_id=dataset_id,
                                target_version=1, operation_id=op.id, relationship=relationship, now=now)
            lineage_ids.append(ref.dataset_id)
        self.audit.record(event_type="dataset.created", entity_type="dataset", entity_id=dataset_id, operation_id=op.id,
                          now=now, actor=principal, details={"name": dataset.name, "origin": "materialize", "rows": result.row_count})
        self.audit.record(event_type="version.created", entity_type="dataset", entity_id=dataset_id, operation_id=op.id,
                          now=now, actor=principal, details={"version": 1, "table": table})
        self.audit.record(event_type="lineage.recorded", entity_type="dataset", entity_id=dataset_id, operation_id=op.id,
                          now=now, actor=principal, details={"inputs": lineage_ids})
        return {
            "status": "success",
            "operation_id": op.id,
            "dataset": {"id": dataset_id, "name": dataset.name, "rows": result.row_count,
                        "columns": [c.name for c in columns], "version": 1},
            "summary": self._summarize(ir, dataset.name),
            "lineage": lineage_ids,
            "preview": {"columns": result.columns, "rows": _jsonable(result.rows), "truncated": result.truncated},
        }

    # ======================================================================
    # publish / update_metadata / attach_metadata / delete / restore
    # ======================================================================
    @semantic_operation("publish_dataset")
    def publish_dataset(self, dataset: Any, *, principal: str | None = None) -> dict[str, Any]:
        notes: list[ResolutionNote] = []
        ds = self.datasets.resolve(dataset, field="dataset", notes=notes)
        intent = _jsonable({"dataset": dataset})
        with self._operation(OperationKind.PUBLISH, intent, principal) as op:
            if ds.status == DatasetStatus.PUBLISHED:
                response = {"status": "success", "operation_id": op.id, "dataset": self._dataset_summary(ds),
                            "already_published": True, "summary": f"{ds.name} was already published."}
                with self.store.transaction():
                    self._complete(op, response, None, None, self.clock())
                return self._with_notes(response, notes)
            problems = self._publish_problems(ds)
            if problems:
                raise InvalidStateError(
                    f"Dataset {ds.name} ({ds.id}) is not in a publishable state.",
                    field="dataset",
                    details={"problems": problems},
                    hint="Fix the listed problems (for example with update_metadata) and publish again.",
                )
            now = self.clock()
            with self.store.transaction():
                ds.status = DatasetStatus.PUBLISHED
                ds.updated_at = now
                self.store.update_dataset(ds)
                self.audit.record(event_type="dataset.published", entity_type="dataset", entity_id=ds.id,
                                  operation_id=op.id, now=now, actor=principal, details={"version": ds.version})
                response = {"status": "success", "operation_id": op.id, "dataset": self._dataset_summary(ds),
                            "summary": f"Published {ds.name} (version {ds.version}) as stable."}
                self._complete(op, response, None, None, now)
            log_event("state.committed", operation_id=op.id, dataset_id=ds.id, status="published")
            return self._with_notes(response, notes)

    @semantic_operation("update_metadata")
    def update_metadata(
        self,
        dataset: Any,
        *,
        description: str | None = None,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        columns: dict[str, dict[str, Any]] | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        notes: list[ResolutionNote] = []
        ds = self.datasets.resolve(dataset, field="dataset", notes=notes)
        intent = _jsonable({"dataset": dataset, "description": description, "aliases": aliases, "metadata": metadata, "columns": columns})
        if description is None and aliases is None and metadata is None and columns is None:
            raise InvalidIntentError("update_metadata needs at least one of description, aliases, metadata or columns.")
        if metadata is not None and not isinstance(metadata, dict):
            raise InvalidIntentError("metadata must be an object.", field="metadata")
        reserved = {"aliases", "source_file", "content_hash", "format", "derived", "intent_fingerprint", "knowledge_artifacts"}
        if metadata and (bad := sorted(set(metadata) & reserved)):
            raise InvalidIntentError(f"metadata keys {bad} are managed by the backend.", field="metadata",
                                     hint="Use the aliases parameter for aliases.")
        with self._operation(OperationKind.UPDATE_METADATA, intent, principal) as op:
            changes: dict[str, Any] = {}
            scope = Scope(fields=[ScopeField.from_column(c) for c in ds.columns])
            column_updates: list[Column] = []
            for ref, patch in (columns or {}).items():
                if not isinstance(patch, dict):
                    raise InvalidIntentError(f"column patch for {ref!r} must be an object.", field="columns")
                unknown = sorted(set(patch) - {"description", "aliases", "semantic_role", "unit"})
                if unknown:
                    raise InvalidIntentError(f"Unknown column metadata keys {unknown}.", field="columns",
                                             details={"allowed_keys": ["description", "aliases", "semantic_role", "unit"]})
                target = self.fields.resolve(ref, scope, field="columns", notes=notes)
                column = next(c for c in ds.columns if c.id == target.column_id)
                if "description" in patch:
                    column.description = str(patch["description"])
                if "aliases" in patch:
                    column.aliases = [str(a) for a in (patch["aliases"] or [])]
                if "unit" in patch:
                    column.unit = str(patch["unit"] or "")
                if "semantic_role" in patch:
                    try:
                        column.semantic_role = SemanticRole(str(patch["semantic_role"]).lower())
                    except ValueError as exc:
                        raise InvalidIntentError(f"Unknown semantic_role {patch['semantic_role']!r}.", field="columns",
                                                 details={"allowed": [str(r) for r in SemanticRole]}) from exc
                column_updates.append(column)
                changes.setdefault("columns", []).append(column.name)
            now = self.clock()
            with self.store.transaction():
                if description is not None:
                    ds.description = str(description)
                    changes["description"] = ds.description
                if aliases is not None:
                    ds.metadata["aliases"] = [str(a) for a in aliases]
                    changes["aliases"] = ds.metadata["aliases"]
                if metadata:
                    ds.metadata.update(_jsonable(metadata))
                    changes["metadata"] = sorted(metadata)
                ds.updated_at = now
                self.store.update_dataset(ds)
                for column in column_updates:
                    self.store.update_column(column)
                self.audit.record(event_type="metadata.updated", entity_type="dataset", entity_id=ds.id,
                                  operation_id=op.id, now=now, actor=principal, details=changes)
                response = {"status": "success", "operation_id": op.id, "dataset": self._dataset_summary(ds),
                            "changes": changes, "summary": f"Updated metadata of {ds.name}: {', '.join(changes)}."}
                self._complete(op, response, None, None, now)
            return self._with_notes(response, notes)

    @semantic_operation("attach_metadata")
    def attach_metadata(
        self,
        source: str,
        *,
        dataset: Any | None = None,
        overwrite: bool = False,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """Ingest a knowledge / semantic-layer document into dataset and column metadata."""
        notes: list[ResolutionNote] = []
        artifact = self._resolve_document(source)
        text = Path(artifact.managed_path or artifact.path).read_text(encoding="utf-8", errors="replace")
        document = parse_knowledge_markdown(text)
        targets = [self.datasets.resolve(dataset, field="dataset", notes=notes)] if dataset is not None else self.store.list_datasets()
        intent = _jsonable({"source": source, "dataset": dataset, "overwrite": overwrite})
        with self._operation(OperationKind.ATTACH_METADATA, intent, principal) as op:
            op.canonical_ir = {"operation": "attach_metadata", "artifact_id": artifact.id, "facts": document.summary(),
                               "targets": [d.id for d in targets], "overwrite": overwrite}
            plan = ExecutionPlan(
                steps=[
                    PlanStep("ParseDocument", f"ParseDocument({artifact.name})", document.summary()),
                    PlanStep("MatchDatasets", f"MatchDatasets({len(targets)} candidates)"),
                    PlanStep("ApplyMetadata", "ApplyMetadata(descriptions, units)"),
                    PlanStep("Audit", "Audit(metadata.attached)"),
                ],
                physical_inputs={}, output_table=None,
            )
            op.execution_plan = plan.to_dict()
            self._save_operation(op)
            log_event("plan.created", operation_id=op.id, plan=plan.to_text(), facts=document.summary())

            applied, unmatched_tables, unmatched_columns = self._match_knowledge(document, targets, overwrite)
            now = self.clock()
            with self.store.transaction():
                touched: list[dict[str, Any]] = []
                for changes in applied:
                    ds = changes["dataset"]
                    if not changes["description_set"] and not changes["columns"]:
                        continue
                    knowledge = ds.metadata.setdefault("knowledge_artifacts", [])
                    if artifact.id not in knowledge:
                        knowledge.append(artifact.id)
                    ds.updated_at = now
                    self.store.update_dataset(ds)
                    for column in changes["column_objects"]:
                        self.store.update_column(column)
                    self.audit.record(event_type="metadata.attached", entity_type="dataset", entity_id=ds.id,
                                      operation_id=op.id, now=now, actor=principal,
                                      details={"artifact_id": artifact.id, "description_set": changes["description_set"],
                                               "columns": changes["columns"]})
                    touched.append({"dataset": ds.id, "name": ds.name, "description_set": changes["description_set"],
                                    "columns": changes["columns"]})
                response = {
                    "status": "success",
                    "operation_id": op.id,
                    "source": self._artifact_summary(artifact),
                    "facts": document.summary(),
                    "applied": touched,
                    "unmatched": {"tables": unmatched_tables, "columns": unmatched_columns},
                    "summary": (
                        f"Attached {artifact.name}: updated {len(touched)} dataset(s), "
                        f"{sum(len(t['columns']) for t in touched)} column(s); "
                        f"{len(unmatched_tables)} table fact(s) and {len(unmatched_columns)} column fact(s) did not match."
                    ),
                    "plan": plan.to_text(),
                }
                if unmatched_tables or unmatched_columns:
                    response["hint"] = "Unmatched facts refer to names that no dataset has; use update_metadata to apply them by hand if they matter."
                self._complete(op, response, None, None, now)
            log_event("state.committed", operation_id=op.id, datasets=len(touched))
            return self._with_notes(response, notes)

    def _resolve_document(self, source: str) -> Artifact:
        if not isinstance(source, str) or not source.strip():
            raise InvalidIntentError("source must be an artifact id, an artifact name, or a path to a markdown document.", field="source")
        text = source.strip()
        artifact = self.store.get_artifact(text)
        if artifact is None:
            # A leading slash or ./ and a bare file name are the same document to an agent.
            loose = text.lstrip("./").lower()
            candidates = [a for a in self.store.list_artifacts()
                          if a.name.lower() in (text.lower(), loose) or a.path == text]
            if not candidates and loose:
                candidates = [a for a in self.store.list_artifacts() if Path(a.name).name.lower() == Path(loose).name]
            if len(candidates) == 1:
                artifact = candidates[0]
            elif len(candidates) > 1:
                raise AmbiguousReferenceError(f"Several artifacts are named {text!r}.", field="source",
                                              candidates=[self._artifact_summary(a) for a in candidates])
        if artifact is None:
            path = Path(text).expanduser()
            if not path.is_file():
                documents = [a for a in self.store.list_artifacts() if a.kind.is_document]
                shown = ", ".join(a.name for a in documents[:12]) if documents else "none"
                raise NotFoundError(
                    f"No artifact or file matches {text!r}; the documents registered here are {shown}.",
                    field="source",
                    candidates=[self._artifact_summary(a) for a in documents],
                    details={"reference": text, "documents": [a.name for a in documents]},
                )
            artifact = self._register_artifact(path, self._classify(path, None))
        if artifact.kind not in (ArtifactKind.MARKDOWN, ArtifactKind.TEXT):
            raise InvalidSchemaError(f"{artifact.name} is a {artifact.kind} artifact; attach_metadata reads markdown or text.", field="source")
        return artifact

    def _match_knowledge(
        self, document: KnowledgeDocument, targets: list[Dataset], overwrite: bool
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        by_key: dict[str, Dataset] = {}
        for ds in targets:
            for key in (ds.id, ds.name, normalize(ds.name), *(normalize(a) for a in ds.aliases)):
                by_key.setdefault(key.casefold(), ds)

        def find_dataset(name: str) -> Dataset | None:
            return by_key.get(name.casefold()) or by_key.get(normalize(name))

        applied: dict[str, dict[str, Any]] = {}

        def changes_for(ds: Dataset) -> dict[str, Any]:
            return applied.setdefault(ds.id, {"dataset": ds, "description_set": False, "columns": [], "column_objects": []})

        unmatched_tables: list[dict[str, Any]] = []
        for fact in document.tables:
            ds = find_dataset(fact.name)
            if ds is None:
                unmatched_tables.append({"name": fact.name, "line": fact.line})
                continue
            changes = changes_for(ds)
            if fact.description and (overwrite or not ds.description.strip()) and not changes["description_set"]:
                ds.description = fact.description
                changes["description_set"] = True

        unmatched_columns: list[dict[str, Any]] = []
        for fact in document.columns:
            if fact.scope is None:
                candidates = targets
            else:
                scoped = find_dataset(fact.scope)
                candidates = [scoped] if scoped is not None else []
            hit = False
            for ds in candidates:
                column = self._find_column(ds, fact.name)
                if column is None:
                    continue
                hit = True
                changes = changes_for(ds)
                updated = False
                if fact.description and (overwrite or not column.description.strip()):
                    column.description = fact.description
                    updated = True
                if fact.unit and (overwrite or not column.unit.strip()):
                    column.unit = fact.unit
                    updated = True
                if updated and column.name not in changes["columns"]:
                    changes["columns"].append(column.name)
                    changes["column_objects"].append(column)
            if not hit:
                unmatched_columns.append({"name": fact.name, "scope": fact.scope, "line": fact.line})
        return list(applied.values()), unmatched_tables, unmatched_columns

    @staticmethod
    def _find_column(ds: Dataset, name: str) -> Column | None:
        lowered = name.casefold()
        for c in ds.columns:
            if c.name == name:
                return c
        for c in ds.columns:
            if c.name.casefold() == lowered or lowered in (a.casefold() for a in c.aliases):
                return c
        norm = normalize(name)
        hits = [c for c in ds.columns if normalize(c.name) == norm]
        return hits[0] if len(hits) == 1 else None

    @semantic_operation("delete_dataset")
    def delete_dataset(self, dataset: Any, *, reason: str | None = None, principal: str | None = None) -> dict[str, Any]:
        notes: list[ResolutionNote] = []
        try:
            ds = self.datasets.resolve(dataset, field="dataset", notes=notes)
        except NotFoundError as err:
            if err.details.get("status") == "deleted":
                gone = self.store.get_dataset(err.details["dataset_id"], include_deleted=True)
                return {"status": "success", "dataset": self._dataset_summary(gone), "already_deleted": True,
                        "restore": {"operation": "restore_dataset", "dataset": gone.id},
                        "summary": f"{gone.name} was already deleted."}
            raise
        intent = _jsonable({"dataset": dataset, "reason": reason})
        with self._operation(OperationKind.DELETE, intent, principal) as op:
            downstream = self.lineage.downstream(ds.id, depth=1)
            now = self.clock()
            with self.store.transaction():
                ds.metadata["status_before_delete"] = str(ds.status)
                ds.status = DatasetStatus.DELETED
                ds.deleted_at = now
                ds.updated_at = now
                self.store.update_dataset(ds)
                self.audit.record(event_type="dataset.deleted", entity_type="dataset", entity_id=ds.id,
                                  operation_id=op.id, now=now, actor=principal, details={"reason": reason, "logical": True})
                response = {
                    "status": "success",
                    "operation_id": op.id,
                    "dataset": self._dataset_summary(ds),
                    "summary": f"Logically deleted {ds.name} ({ds.id}); its data is retained and can be restored.",
                    "restore": {"operation": "restore_dataset", "dataset": ds.id},
                    "affected_downstream": [{"id": e["target"], "name": e["target_name"]} for e in downstream],
                }
                self._complete(op, response, None, None, now)
            log_event("state.committed", operation_id=op.id, dataset_id=ds.id, status="deleted")
            return self._with_notes(response, notes)

    @semantic_operation("restore_dataset")
    def restore_dataset(self, dataset: Any, *, new_name: str | None = None, principal: str | None = None) -> dict[str, Any]:
        text = dataset if isinstance(dataset, str) else (dataset or {}).get("dataset_id") or (dataset or {}).get("id") or (dataset or {}).get("name")
        if not isinstance(text, str) or not text.strip():
            raise InvalidIntentError("restore_dataset needs a dataset id or name.", field="dataset")
        deleted = [d for d in self.store.list_datasets(include_deleted=True) if d.status == DatasetStatus.DELETED]
        hits = [d for d in deleted if d.id.lower() == text.lower() or d.name.lower() == text.lower()]
        if not hits:
            active = self.store.get_dataset_by_name(text) or self.store.get_dataset(text)
            if active is not None:
                return {"status": "success", "dataset": self._dataset_summary(active), "already_active": True,
                        "summary": f"{active.name} is not deleted."}
            raise NotFoundError(f"No deleted dataset matches {text!r}.", field="dataset",
                                candidates=[{"id": d.id, "name": d.name} for d in deleted])
        if len(hits) > 1:
            raise AmbiguousReferenceError(f"Several deleted datasets match {text!r}.", field="dataset",
                                          candidates=[{"id": d.id, "name": d.name, "deleted_at": d.deleted_at.isoformat()} for d in hits])
        ds = hits[0]
        target_name = slugify(new_name) if new_name else ds.name
        if self.store.get_dataset_by_name(target_name) is not None:
            raise ConflictError(f"An active dataset named {target_name!r} already exists.", field="new_name",
                                hint="Pass new_name to restore under a different name.")
        intent = _jsonable({"dataset": dataset, "new_name": new_name})
        with self._operation(OperationKind.RESTORE, intent, principal) as op:
            now = self.clock()
            with self.store.transaction():
                previous = ds.metadata.pop("status_before_delete", "active")
                ds.status = DatasetStatus(previous) if previous in ("active", "published") else DatasetStatus.ACTIVE
                ds.name = target_name
                ds.deleted_at = None
                ds.updated_at = now
                self.store.update_dataset(ds)
                self.audit.record(event_type="dataset.restored", entity_type="dataset", entity_id=ds.id,
                                  operation_id=op.id, now=now, actor=principal, details={"name": target_name})
                response = {"status": "success", "operation_id": op.id, "dataset": self._dataset_summary(ds),
                            "summary": f"Restored {ds.name} ({ds.id})."}
                self._complete(op, response, None, None, now)
            return response

    # ======================================================================
    # export_result
    # ======================================================================
    @semantic_operation("export_result")
    def export_result(
        self,
        dataset: Any,
        path: str,
        *,
        format: str = "csv",
        format_spec: dict[str, Any] | None = None,
        overwrite: bool = False,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """Write a managed dataset to a file at the boundary of the system.

        The file is an *export*, not authoritative state: the dataset keeps
        living in the backend, and the audit trail records where a copy went
        and what it contained (content hash, rows). ``format_spec`` decides how
        values are rendered as text (MADR 0005); the dataset itself stays typed.
        """
        notes: list[ResolutionNote] = []
        ds = self.datasets.resolve(dataset, field="dataset", notes=notes)
        fmt = str(format or "csv").lower().lstrip(".")
        if fmt not in ("csv", "parquet"):
            raise InvalidSchemaError(f"Unsupported export format {fmt!r}; supported formats are csv and parquet.", field="format")
        if not isinstance(path, str) or not path.strip():
            raise InvalidIntentError("path must be a file path.", field="path")
        spec = self._export_format(format_spec, fmt)
        target = Path(path).expanduser()
        version = self.store.get_version(ds.id, ds.version)
        if version is None:
            raise InvalidStateError(f"Dataset {ds.name} has no physical version.", field="dataset")
        spec_payload = spec.model_dump(mode="json", exclude_none=True) if spec is not None else None
        if spec_payload is not None and not spec_payload.get("columns"):
            spec_payload.pop("columns", None)
        intent = _jsonable({"dataset": dataset, "path": path, "format": fmt, "format_spec": format_spec,
                            "overwrite": overwrite})
        with self._operation(OperationKind.EXPORT, intent, principal) as op:
            # Refusals are recorded as failed operations so attempts to write outside the sandbox are auditable.
            if self.export_root is not None:
                resolved = (self.export_root / target).resolve() if not target.is_absolute() else target.resolve()
                if resolved != self.export_root and self.export_root not in resolved.parents:
                    raise PermissionDeniedError(
                        f"Exports are only allowed under {self.export_root}; refusing to write {target}.",
                        field="path",
                        recoverable=True,
                        details={"export_root": str(self.export_root)},
                        hint=f"Use a path inside {self.export_root} (relative paths are resolved against it).",
                    )
                target = resolved
            if target.is_dir():
                raise InvalidIntentError(f"{target} is a directory; path must name a file.", field="path",
                                         hint="Give the file to write, for example a path ending in .csv.")
            if target.exists() and not overwrite:
                raise ConflictError(f"{target} already exists.", field="path", hint="Pass overwrite=true to replace it.",
                                    details={"path": str(target), "existing": self._file_facts(target)})
            # The contract the agent declared while the requirement was fresh is the
            # reference; the export attempted now is the thing checked (MADR 0007, 0008).
            contract = self.store.latest_contract()
            steps = [PlanStep("Scan", f"Scan({ds.name} v{ds.version})", {"table": version.physical_table})]
            if contract is not None:
                steps.append(PlanStep("VerifyContract", f"VerifyContract({self._contract_shape(contract)})",
                                      {"contract_id": contract.id, "revision": contract.revision}))
            if spec is not None:
                detail = f"{len(spec.columns)} column rule(s)" if spec.columns else "file defaults"
                steps.append(PlanStep("FormatValues", f"FormatValues({detail})", {"format_spec": spec_payload}))
            steps.append(PlanStep("WriteFile", f"WriteFile({target.name}, {fmt})", {"path": str(target)}))
            steps.append(PlanStep("Audit", "Audit(dataset.exported)"))
            plan = ExecutionPlan(steps=steps, physical_inputs={ds.id: version.physical_table}, output_table=None)
            op.canonical_ir = {"operation": "export", "dataset_id": ds.id, "version": ds.version, "path": str(target),
                               "format": fmt, "format_spec": spec_payload,
                               "contract": {"id": contract.id, "revision": contract.revision} if contract else None}
            op.execution_plan = plan.to_dict()
            self._save_operation(op)
            log_event("plan.created", operation_id=op.id, plan=plan.to_text())
            # Verified before anything is read or written: a mismatch costs no work
            # and leaves an existing target untouched.
            evidence = (self._verify_contract(contract, ds, version, export={"path": str(target), "format": fmt,
                                                                         "format_spec": format_spec, "overwrite": overwrite})
                        if contract is not None else None)
            # What the contract carries, in its order, sorted the way it declares:
            # the organizing columns are needed in the dataset and left out of the
            # file (MADR 0012). Without a contract the dataset is written as it is.
            projection = [c.name for c in contract.columns] if contract is not None and organizing_keys(contract) else None
            ordering = [(o.name, o.descending) for o in contract.order_by] if contract is not None else []
            renderer: ValueRenderer | None = None
            columns: list[str] = []
            data: list[Any] = []
            if spec is not None:
                # Prepared before anything is staged, so a bad column key costs no work.
                columns, data = self.engine.read_table(version.physical_table, columns=projection, order_by=ordering)
                renderer = ValueRenderer(spec, columns)
                for key, column, how in renderer.lenient:
                    notes.append(ResolutionNote("format_spec.columns", key, column, how))
            target.parent.mkdir(parents=True, exist_ok=True)
            # Publish only a complete file, staged on the target's filesystem.
            # A failed render leaves the original target untouched.
            with TemporaryDirectory(prefix=".intentum-export-", dir=target.parent) as staging_dir:
                staged = Path(staging_dir) / target.name
                if renderer is None:
                    rows = self.engine.export_table(version.physical_table, staged, fmt,
                                                    columns=projection, order_by=ordering)
                else:
                    rows = write_formatted_csv(staged, columns, data, renderer)
                content_hash = self.workspace.content_hash(staged)
                self._publish_export(staged, target, overwrite)
            log_event("execution.completed", operation_id=op.id, path=str(target), rows=rows)
            now = self.clock()
            with self.store.transaction():
                exported_details = {"path": str(target), "format": fmt, "rows": rows, "content_hash": content_hash,
                                    "version": ds.version, "format_spec": spec_payload}
                if contract is not None and evidence is not None:
                    contract.status = ContractStatus.SATISFIED
                    contract.satisfied_by = op.id
                    contract.dataset_id = ds.id
                    contract.dataset_version = ds.version
                    contract.updated_at = now
                    self.store.update_contract(contract)
                    self.audit.record(event_type="output_contract.satisfied", entity_type="output_contract",
                                      entity_id=contract.id, operation_id=op.id, now=now, actor=principal,
                                      details={**evidence, "dataset": ds.name, "path": str(target), "format": fmt,
                                               "content_hash": content_hash})
                    exported_details["contract"] = evidence
                self.audit.record(event_type="dataset.exported", entity_type="dataset", entity_id=ds.id,
                                  operation_id=op.id, now=now, actor=principal, details=exported_details)
                response = {
                    "status": "success",
                    "operation_id": op.id,
                    "dataset": self._dataset_summary(ds),
                    "path": str(target),
                    "format": fmt,
                    "rows": rows,
                    "columns": projection or [c.name for c in ds.columns],
                    "content_hash": content_hash,
                    "summary": f"Exported {ds.name} (version {ds.version}, {rows} rows) to {target}."
                               + (" Values were rendered with the given format specification." if spec is not None else ""),
                    "plan": plan.to_text(),
                }
                if spec_payload is not None:
                    response["format_spec"] = spec_payload
                if contract is not None and evidence is not None:
                    response["contract"] = {**contract_summary(contract), "verified": evidence}
                    response["summary"] += f" Output contract {contract.id} (revision {contract.revision}) is satisfied."
                self._complete(op, response, None, None, now)
            log_event("state.committed", operation_id=op.id, dataset_id=ds.id, exported=str(target))
            return self._with_notes(response, notes)

    @semantic_operation("declare_output")
    def declare_output(
        self,
        columns: Any,
        *,
        rows: Any = None,
        order_by: Any = None,
        description: str | None = None,
        reason: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """Declare the shape of the deliverable before (or while) working towards it.

        The declaration is trusted as given (MADR 0008) and held by the backend;
        every ``export_result`` is then checked against it (MADR 0007). One
        contract is current per workspace: declaring while one is open and
        unsatisfied is an amendment and needs a ``reason``, which is recorded.
        Re-declaring the same shape changes nothing and is not an error.
        """
        try:
            spec = parse_contract(columns, rows, order_by, description)
        except BackendError as err:
            # Teach on refusal: a declaration is refused with the grammar it accepts.
            err.advice = advise_error(err, tool="declare_output", arguments={
                "columns": columns, "rows": rows, "order_by": order_by, "description": description})
            raise
        intent = _jsonable({"columns": columns, "rows": rows, "order_by": order_by, "description": description,
                            "reason": reason})
        with self._operation(OperationKind.DECLARE_OUTPUT, intent, principal) as op:
            op.canonical_ir = {
                "operation": "declare_output",
                "columns": [{"name": c.name, "type": str(c.logical_type) if c.logical_type else None} for c in spec.columns],
                "rows": str(spec.rows) if spec.rows else None,
                "row_keys": spec.row_keys,
                "order_by": [{"column": o.name, "descending": o.descending} for o in spec.order_by],
            }
            current = self.store.latest_contract()
            now = self.clock()
            if current is not None and current.status == ContractStatus.OPEN:
                if spec.same_shape_as(current):
                    with self.store.transaction():
                        response = {
                            "status": "success", "operation_id": op.id, "contract": contract_summary(current),
                            "unchanged": True,
                            "summary": f"Output contract {current.id} already declares this shape (revision "
                                       f"{current.revision}); nothing changed. {self._contract_sentence(current)}",
                        }
                        self._complete(op, response, None, None, now)
                    return response
                if not isinstance(reason, str) or not reason.strip():
                    raise ConflictError(
                        f"Output contract {current.id} is open and declares {self._contract_shape(current)}; "
                        "changing it is an amendment and needs a reason.",
                        field="reason",
                        details={"contract": contract_summary(current)},
                        hint="To amend the open contract, pass reason explaining the changed requirement or the "
                             "earlier misinterpretation. Otherwise produce what the current contract declares.",
                    )
                before = contract_summary(current)
                current.columns = spec.columns
                current.rows = spec.rows
                current.row_keys = spec.row_keys
                current.order_by = spec.order_by
                if spec.description:
                    current.description = spec.description
                current.revision += 1
                current.operation_id = op.id
                current.updated_at = now
                with self.store.transaction():
                    self.store.update_contract(current)
                    self.audit.record(event_type="output_contract.amended", entity_type="output_contract",
                                      entity_id=current.id, operation_id=op.id, now=now, actor=principal,
                                      details={"reason": reason.strip(), "before": before,
                                               "after": contract_summary(current), "revision": current.revision})
                    response = {
                        "status": "success", "operation_id": op.id, "contract": contract_summary(current),
                        "amended": True, "reason": reason.strip(),
                        "summary": f"Output contract {current.id} amended to revision {current.revision}: "
                                   f"{self._contract_sentence(current)}",
                    }
                    self._add_name_facts(response, spec)
                    self._complete(op, response, None, None, now)
                log_event("state.committed", operation_id=op.id, contract_id=current.id, amended=True)
                return response
            contract = OutputContract(id=self.store.allocate_id("oc"), columns=spec.columns, rows=spec.rows,
                                      row_keys=spec.row_keys, order_by=spec.order_by, description=spec.description,
                                      operation_id=op.id, created_at=now, updated_at=now)
            with self.store.transaction():
                self.store.insert_contract(contract)
                details: dict[str, Any] = {"contract": contract_summary(contract)}
                if current is not None:
                    details["supersedes"] = current.id
                self.audit.record(event_type="output_contract.declared", entity_type="output_contract",
                                  entity_id=contract.id, operation_id=op.id, now=now, actor=principal, details=details)
                response = {
                    "status": "success", "operation_id": op.id, "contract": contract_summary(contract),
                    "summary": f"Output contract {contract.id} declared: {self._contract_sentence(contract)}",
                }
                self._add_name_facts(response, spec)
                self._complete(op, response, None, None, now)
            log_event("state.committed", operation_id=op.id, contract_id=contract.id)
            return response

    def _add_name_facts(self, response: dict[str, Any], spec: ContractSpec) -> None:
        facts = self._declared_name_facts(spec)
        if facts:
            response["names"] = facts

    def _declared_name_facts(self, spec: ContractSpec) -> list[dict[str, Any]]:
        """What the workspace holds under each name a declaration uses.

        The backend never sees the question, so it cannot know whether a name
        belongs in the answer. What it can do is say what the name is *here* —
        which dataset carries it, its type and role, and whether it is unique
        per row, which is what tells a locator apart from a value — and leave
        the reading to the agent (MADR 0010: the backend owns the data facts).
        """
        datasets = self.store.list_datasets()
        facts: list[dict[str, Any]] = []
        carried = {normalize(c.name) for c in spec.columns}
        ordered = {normalize(o.name) for o in spec.order_by}
        grain = {normalize(k) for k in spec.row_keys}
        for name in [c.name for c in spec.columns] + organizing_keys(spec):
            matches: list[dict[str, Any]] = []
            for dataset in datasets:
                if len(matches) == 3:
                    break
                column = next((c for c in self.store.get_columns(dataset.id)
                               if normalize(c.name) == normalize(name)), None)
                if column is None:
                    continue
                match: dict[str, Any] = {"dataset": dataset.name, "column": column.name,
                                         "type": str(column.logical_type)}
                if column.semantic_role != SemanticRole.UNKNOWN:
                    match["role"] = str(column.semantic_role)
                unique = self._column_is_unique(dataset, column.name)
                if unique is not None:
                    match["unique_per_row"] = unique
                matches.append(match)
            fact: dict[str, Any] = {"name": name, "carried": normalize(name) in carried}
            organizing = [field for field, named in (("order_by", ordered), ("one_per", grain))
                          if normalize(name) in named]
            if organizing:
                fact["organizing"] = organizing
            notes: list[str] = []
            if matches:
                fact["matches"] = matches
                locators = [m["dataset"] for m in matches if m.get("unique_per_row")]
                if locators and fact["carried"]:
                    notes.append(f"{name!r} is unique per row in {', '.join(locators)}, which is what a locator "
                                 "looks like rather than a value; the answer will carry it.")
            else:
                notes.append(f"no dataset in this workspace has a column named {name!r} yet.")
            if organizing and fact["carried"]:
                notes.append(self._both_lists_sentence(name, organizing))
            if notes:
                fact["note"] = " ".join(notes)
            facts.append(fact)
        return facts

    @staticmethod
    def _both_lists_sentence(name: str, organizing: list[str]) -> str:
        """What it means to name one column both as carried and as organizing.

        Both are legal and the contract holds the declaration as written: a
        column that is ordered by and carried is simply carried (MADR 0012).
        Some answers do want the sort key in the file. The backend never sees
        the question, so it says what the declaration means here and leaves the
        reading to the agent (MADR 0010).
        """
        where = " and ".join("order_by" if field == "order_by" else "the one_per keys" for field in organizing)
        does = " and ".join("order" if field == "order_by" else "set the grain of" for field in organizing)
        return (f"{name!r} is named both as a carried column and in {where}; the file will carry it. "
                f"Named only in {where} it would {does} the answer without being written to the file.")

    def _column_is_unique(self, dataset: Dataset, column: str) -> bool | None:
        """Whether the column has a different value in every row, when that is cheap to know."""
        version = self.store.get_version(dataset.id, dataset.version)
        if version is None or version.row_count <= 1:
            return None
        try:
            return self.engine.count_distinct_rows(version.physical_table, [column]) == version.row_count
        except BackendError:
            return None

    def _verify_contract(self, contract: OutputContract, ds: Dataset, version: DatasetVersion,
                         export: dict[str, Any] | None = None) -> dict[str, Any]:
        """Hold a dataset to the contract; the evidence returned goes into the audit event."""
        actual = [(c.name, c.logical_type) for c in ds.columns]
        problems = verify_columns(contract, actual)
        distinct: int | None = None
        names = {n for n, _ in actual}
        if contract.rows == RowCardinality.ONE_PER and all(k in names for k in contract.row_keys) and version.row_count > 0:
            distinct = self.engine.count_distinct_rows(version.physical_table, contract.row_keys)
        if contract.rows != RowCardinality.ONE_PER or distinct is not None or version.row_count == 0:
            problems += verify_rows(contract, version.row_count, distinct)
        if problems:
            repair = repair_transform(contract, problems)
            if repair is not None:
                first = (f"Reshape it with materialize_result(source={ds.name!r}, transform={json.dumps(repair, ensure_ascii=False)}) "
                         "and export that dataset, or")
            else:
                first = "Produce a dataset with the declared shape and export that, or"
            details: dict[str, Any] = {
                "contract": contract_summary(contract),
                "actual": {"dataset": ds.name, "version": ds.version, "rows": version.row_count,
                           "columns": [{"name": n, "type": str(t)} for n, t in actual]},
                "problems": [p.to_dict() for p in problems],
            }
            if repair is not None:
                details["repair"] = repair
            err = ContractMismatchError(
                f"{ds.name} does not have the shape declared in output contract {contract.id}: "
                + " ".join(p.message for p in problems),
                field="dataset",
                candidates=[n for n, _ in actual],
                details=details,
                hint=first + " call declare_output again with a reason if the requirement changed or the earlier "
                            "declaration misinterpreted it.",
            )
            problem_text = " ".join(p.message for p in problems)
            amend = (" Amend the contract with declare_output and a reason if the requirement changed or the earlier "
                     "declaration misinterpreted it; a mismatch with this dataset alone is not a reason to amend.")
            if repair is not None:
                answer = f"answer_{contract.id}"
                rewrite = [tool_call("materialize_result", source=ds.name, transform=repair, name=answer,
                                     description=contract.description or "the declared deliverable")]
                if export is not None:
                    rewrite.append(tool_call("export_result", dataset=answer, **export))
                err.advice = [Advice("reshape_to_contract",
                                     f"{ds.name} differs from the declared shape only in shape: {problem_text} "
                                     "Reshape it as below and export the new dataset." + amend, rewrite=rewrite)]
            else:
                err.advice = [Advice("contract_mismatch",
                                     f"{ds.name} lacks something the contract declares: {problem_text} "
                                     "Go back to the data for it and export a dataset with the declared shape." + amend)]
            raise err
        evidence: dict[str, Any] = {"contract_id": contract.id, "revision": contract.revision,
                                    "columns": [n for n, _ in actual], "rows": version.row_count}
        if contract.rows is not None:
            evidence["cardinality"] = str(contract.rows)
        if distinct is not None:
            evidence["distinct_keys"] = distinct
        return evidence

    @staticmethod
    def _contract_shape(contract: OutputContract) -> str:
        text = f"{contract.id} rev {contract.revision}: columns [{', '.join(c.name for c in contract.columns)}]"
        if contract.rows == RowCardinality.ONE_PER:
            text += f", one row per [{', '.join(contract.row_keys)}]"
        elif contract.rows is not None:
            text += f", rows {contract.rows.replace('_', ' ')}"
        if contract.order_by:
            text += (", ordered by ["
                     + ", ".join(o.name + (" desc" if o.descending else "") for o in contract.order_by) + "]")
        return text

    @staticmethod
    def _contract_sentence(contract: OutputContract) -> str:
        columns = ", ".join(c.name + (f" ({c.logical_type})" if c.logical_type else "") for c in contract.columns)
        text = f"the exported file must carry exactly the columns [{columns}] in this order"
        if contract.rows == RowCardinality.ONE:
            text += " and exactly one row"
        elif contract.rows == RowCardinality.AT_LEAST_ONE:
            text += " and at least one row"
        elif contract.rows == RowCardinality.ONE_PER:
            text += f" and one row per [{', '.join(contract.row_keys)}]"
        if contract.order_by:
            text += (", sorted by ["
                     + ", ".join(o.name + (" descending" if o.descending else "") for o in contract.order_by) + "]")
        keys = organizing_keys(contract)
        if keys:
            text += (f"; [{', '.join(keys)}] organize the answer without being part of it, so the dataset must carry "
                     "them and the file will not")
        if contract.status == ContractStatus.SATISFIED:
            return text + f"; satisfied by {contract.satisfied_by}."
        return text + "; export_result will refuse anything else."

    @staticmethod
    def _file_facts(path: Path) -> dict[str, Any]:
        """Size and modification time of a file that is in the way, for a conflict the agent can weigh."""
        try:
            st = path.stat()
        except OSError:
            return {}
        return {"size_bytes": st.st_size,
                "modified_at": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat()}

    @staticmethod
    def _publish_export(staged: Path, target: Path, overwrite: bool) -> None:
        """Move a fully written file into place, keeping failures structured.

        Both branches are atomic: replace swaps the inode, link refuses a target
        that appeared while we were rendering. Anything the filesystem refuses is
        reported as a failed export, never as an internal error.
        """
        try:
            if overwrite:
                try:
                    target_mode = stat.S_IMODE(target.stat().st_mode)
                except FileNotFoundError:
                    pass
                else:
                    staged.chmod(target_mode)
                staged.replace(target)
            else:
                # Linking is atomic and refuses a target created after our initial check.
                os.link(staged, target)
        except FileExistsError as exc:
            raise ConflictError(f"{target} already exists.", field="path",
                                details={"path": str(target), "existing": Backend._file_facts(target)},
                                hint="Pass overwrite=true to replace it.") from exc
        except IsADirectoryError as exc:
            raise InvalidIntentError(f"{target} is a directory; path must name a file.", field="path",
                                     hint="Give the file to write, for example a path ending in .csv.") from exc
        except OSError as exc:
            raise ExecutionFailedError(f"Could not write the export to {target}: {exc}",
                                       details={"path": str(target)}) from exc

    @staticmethod
    def _export_format(format_spec: Any, fmt: str) -> ExportFormat | None:
        """Validate a format specification the way the IR is validated."""
        if format_spec is None:
            return None
        if not isinstance(format_spec, dict):
            raise InvalidIntentError("format_spec must be an object.", field="format_spec")
        if fmt != "csv":
            raise InvalidIntentError(
                f"format_spec renders values as text; {fmt} keeps typed values, so it takes no specification.",
                field="format_spec",
                hint="Export as csv to apply a format specification.",
            )
        try:
            return ExportFormat.model_validate(format_spec)
        except ValidationError as err:
            raise InvalidSchemaError(
                "format_spec did not match the expected shape.",
                field="format_spec",
                details={"errors": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in err.errors()]},
                hint='Example: {"decimals": 4, "strip_trailing_zeros": true, "integer_min_decimals": 1, '
                     '"columns": {"end_date": {"date_format": "%Y-%m-%d"}}}',
            ) from err

    # ======================================================================
    # Integrity
    # ======================================================================
    def integrity_report(self) -> dict[str, Any]:
        """Cross-check metadata against the analytical store."""
        issues: list[dict[str, Any]] = []
        referenced: set[str] = set()
        for v in self.store.list_all_versions():
            referenced.add(v.physical_table)
            if not self.engine.table_exists(v.physical_table):
                issues.append({"kind": "missing_table", "dataset_id": v.dataset_id, "version": v.version, "table": v.physical_table})
            elif self.engine.row_count(v.physical_table) != v.row_count:
                issues.append({"kind": "row_count_mismatch", "dataset_id": v.dataset_id, "version": v.version})
        artifacts = {a.id for a in self.store.list_artifacts()}
        for d in self.store.list_datasets(include_deleted=True):
            if self.store.get_version(d.id, d.version) is None:
                issues.append({"kind": "missing_version", "dataset_id": d.id, "version": d.version})
            if not d.columns:
                issues.append({"kind": "no_columns", "dataset_id": d.id})
            if d.source_artifact_id and d.source_artifact_id not in artifacts:
                issues.append({"kind": "missing_artifact", "dataset_id": d.id, "artifact_id": d.source_artifact_id})
        for table in self.engine.list_tables():
            if table not in referenced:
                issues.append({"kind": "orphan_table", "table": table})
        for op in self.store.list_operations(limit=10_000):
            if op.status == OperationStatus.PENDING:
                issues.append({"kind": "pending_operation", "operation_id": op.id})
        return {"ok": not issues, "issues": issues}

    # ======================================================================
    # Internals
    # ======================================================================
    @contextmanager
    def _operation(self, kind: OperationKind, intent: dict[str, Any], principal: str | None,
                   idempotency_key: str | None = None, parent: str | None = None) -> Iterator[Operation]:
        op = Operation(id=self.store.allocate_id("op"), kind=kind, status=OperationStatus.PENDING,
                       original_intent=intent, idempotency_key=idempotency_key, principal=principal,
                       parent_operation_id=parent, created_at=self.clock())
        with self.store.transaction():
            self.store.insert_operation(op)
        log_event("intent.received", operation_id=op.id, kind=str(kind), intent=intent, principal=principal, parent=parent)
        try:
            yield op
        except BackendError as err:
            _advise(err)  # so the recorded error carries the same advice as the response
            self._fail(op, err.to_response())
            raise
        except ValidationError as err:
            self._fail(op, {"code": "INVALID_INTENT", "message": str(err)})
            raise
        except Exception as err:
            self._fail(op, {"code": "INTERNAL", "message": repr(err)})
            raise

    def _fail(self, op: Operation, error: dict[str, Any]) -> None:
        now = self.clock()
        op.status = OperationStatus.FAILED
        op.error = error
        op.completed_at = now
        with self.store.transaction():
            self.store.update_operation(op)
            self.audit.record(event_type="operation.failed", entity_type="operation", entity_id=op.id, operation_id=op.id,
                              now=now, actor=op.principal, details={"code": error.get("code"), "message": error.get("message")})
        log_event("operation.failed", operation_id=op.id, error=error)

    def _save_operation(self, op: Operation) -> None:
        with self.store.transaction():
            self.store.update_operation(op)

    def _complete(self, op: Operation, response: dict[str, Any], key: str | None, fingerprint: str | None,
                  now: dt.datetime) -> None:
        """Must be called inside the committing transaction."""
        op.status = OperationStatus.COMPLETED
        op.result = _jsonable(response)
        op.completed_at = now
        op.idempotency_key = key
        self.store.update_operation(op)
        if key is not None:
            stored = {**_jsonable(response), "_fingerprint": fingerprint}
            self.store.save_idempotent_response(key, op.id, stored, now.isoformat())

    def _replay(self, key: str, fingerprint: str, op: Operation | None = None) -> dict[str, Any] | None:
        stored = self.store.get_idempotent_response(key)
        if stored is None:
            return None
        stored_fp = stored.pop("_fingerprint", None)
        if stored_fp is not None and stored_fp != fingerprint:
            raise ConflictError(
                f"Idempotency key {key!r} was already used for a different request.",
                field="idempotency_key",
                details={"original_operation_id": stored.get("operation_id")},
                hint="Use a new idempotency key for a new logical request.",
            )
        original_op = stored.get("operation_id")
        response = {**stored, "idempotent_replay": True, "original_operation_id": original_op}
        response.setdefault("summary", "")
        response["summary"] = f"Replayed {original_op}: " + str(stored.get("summary", ""))
        if op is not None:
            now = self.clock()
            op.status = OperationStatus.COMPLETED
            op.result = {"replayed": original_op}
            op.completed_at = now
            op.idempotency_key = key
            with self.store.transaction():
                self.store.update_operation(op)
            response["operation_id"] = original_op
        log_event("idempotency.replayed", key=key, original_operation_id=original_op)
        return response

    # -- artifacts ---------------------------------------------------------
    @staticmethod
    def _classify(path: Path, explicit_format: str | None) -> ArtifactKind:
        if explicit_format:
            fmt = str(explicit_format).lower().lstrip(".")
            kind = ARTIFACT_KINDS.get(f".{fmt}")
            if kind is None:
                raise InvalidSchemaError(f"Unsupported format {explicit_format!r}; supported formats are {', '.join(SUPPORTED_FORMATS)}.",
                                         field="format")
            return kind
        return ARTIFACT_KINDS.get(path.suffix.lower(), ArtifactKind.OTHER)

    def _register_artifact(self, path: Path, kind: ArtifactKind, relative_to: Path | None = None) -> Artifact:
        """Register a file once (by path + content hash); tabular files get a managed copy."""
        resolved = path.resolve()
        content_hash = self.workspace.content_hash(resolved)
        existing = self.store.find_artifact(str(resolved), content_hash)
        if existing is not None:
            return existing
        managed = self.workspace.import_file(resolved, content_hash) if kind.is_tabular else None
        artifact = Artifact(
            id=self.store.allocate_id("art"),
            kind=kind,
            name=str(resolved.relative_to(relative_to.resolve())) if relative_to else resolved.name,
            path=str(resolved),
            managed_path=str(managed) if managed else None,
            content_hash=content_hash,
            size_bytes=resolved.stat().st_size,
            created_at=self.clock(),
            metadata={},
        )
        with self.store.transaction():
            self.store.insert_artifact(artifact)
        return artifact

    @staticmethod
    def _artifact_summary(a: Artifact) -> dict[str, Any]:
        return {"id": a.id, "kind": str(a.kind), "name": a.name, "size_bytes": a.size_bytes,
                "managed": a.managed_path is not None, "created_at": a.created_at.isoformat()}

    def _source_summary(self, d: Dataset) -> dict[str, Any] | None:
        if not d.source_artifact_id:
            return None
        artifact = self.store.get_artifact(d.source_artifact_id)
        if artifact is None:
            return {"artifact_id": d.source_artifact_id, "locator": d.source_locator}
        return {"artifact_id": artifact.id, "name": artifact.name, "kind": str(artifact.kind), "locator": d.source_locator}

    # -- projections -----------------------------------------------------
    def _dataset_summary(self, d: Dataset) -> dict[str, Any]:
        version = self.store.get_version(d.id, d.version)
        body = {
            "id": d.id,
            "name": d.name,
            "description": d.description,
            "status": str(d.status),
            "version": d.version,
            "rows": version.row_count if version else None,
            "columns": [c.name for c in d.columns],
            "origin": d.origin,
            "aliases": d.aliases,
            "created_at": d.created_at.isoformat(),
            "updated_at": d.updated_at.isoformat(),
        }
        if d.source_artifact_id:
            artifact = self.store.get_artifact(d.source_artifact_id)
            body["source"] = (f"{artifact.name}::{d.source_locator}" if d.source_locator else artifact.name) if artifact else d.source_artifact_id
        return body

    @staticmethod
    def _column_summary(c: Column) -> dict[str, Any]:
        body: dict[str, Any] = {"id": c.id, "name": c.name, "type": str(c.logical_type), "role": str(c.semantic_role)}
        if c.description:
            body["description"] = c.description
        if c.unit:
            body["unit"] = c.unit
        if c.aliases:
            body["aliases"] = c.aliases
        return body

    @staticmethod
    def _operation_summary(op: Operation) -> dict[str, Any]:
        body = {
            "id": op.id,
            "kind": str(op.kind),
            "status": str(op.status),
            "original_intent": op.original_intent,
            "plan": " → ".join(s["description"] for s in (op.execution_plan or {}).get("steps", [])) or None,
            "parent_operation_id": op.parent_operation_id,
            "created_at": op.created_at.isoformat(),
            "completed_at": op.completed_at.isoformat() if op.completed_at else None,
            "principal": op.principal,
        }
        steps = (op.canonical_ir or {}).get("steps")
        if isinstance(steps, list) and any(isinstance(s, dict) and s.get("type") == "raw_query" for s in steps):
            body["used_raw_query"] = True
        return body

    @staticmethod
    def _semantic_hints(d: Dataset) -> dict[str, Any]:
        measures = [c.name for c in d.columns if c.semantic_role == SemanticRole.MEASURE]
        dimensions = [c.name for c in d.columns if c.semantic_role == SemanticRole.DIMENSION]
        times = [c.name for c in d.columns if c.semantic_role == SemanticRole.TIME]
        identifiers = [c.name for c in d.columns if c.semantic_role == SemanticRole.IDENTIFIER]
        suggestions = [f"sum({m}) by {dim}" for m in measures[:2] for dim in dimensions[:2]]
        units = {c.name: c.unit for c in d.columns if c.unit}
        hints: dict[str, Any] = {"measures": measures, "dimensions": dimensions, "time_columns": times,
                                 "identifiers": identifiers, "suggested_aggregations": suggestions}
        if units:
            hints["units"] = units
        return hints

    @staticmethod
    def _with_notes(body: dict[str, Any], notes: list[ResolutionNote]) -> dict[str, Any]:
        if notes:
            body["resolution"] = [n.to_dict() for n in notes]
        return body

    # -- import helpers ----------------------------------------------------
    @staticmethod
    def _normalize_hints(schema_hints: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not schema_hints:
            return {}
        if not isinstance(schema_hints, dict):
            raise InvalidIntentError("schema_hints must be an object keyed by column name.", field="schema_hints")
        raw = schema_hints.get("columns", schema_hints) if isinstance(schema_hints.get("columns"), dict) else schema_hints
        hints: dict[str, dict[str, Any]] = {}
        for name, patch in raw.items():
            if not isinstance(patch, dict):
                raise InvalidIntentError(f"schema hint for {name!r} must be an object.", field="schema_hints")
            unknown = sorted(set(patch) - {"description", "aliases", "semantic_role", "type", "unit"})
            if unknown:
                raise InvalidIntentError(f"Unknown schema hint keys {unknown} for column {name!r}.", field="schema_hints",
                                         details={"allowed_keys": ["description", "aliases", "semantic_role", "type", "unit"]})
            hints[str(name)] = _jsonable(patch)
        return hints

    def _refine_temporal_columns(self, table: str, physical_columns: list[tuple[str, str]],
                                 hints: dict[str, dict[str, Any]], notes: list[ResolutionNote]) -> dict[str, str]:
        """Promote text columns whose values are all ISO dates/timestamps (MADR 0006).

        Source drivers report what they can store, not what the data means:
        DuckDB's SQLite scanner maps date text to VARCHAR. An explicit ``type``
        hint always wins, and every refinement is reported as a resolution note.
        """
        hinted = {name.casefold() for name, patch in hints.items() if "type" in patch}
        refined: dict[str, str] = {}
        for name, physical in physical_columns:
            if physical_to_logical(physical) != LogicalType.STRING or name.casefold() in hinted:
                continue
            target = self.engine.probe_temporal_type(table, name)
            if target is None:
                continue
            self.engine.cast_column(table, name, target)
            refined[name] = target
            notes.append(ResolutionNote(f"schema.{name}", physical.lower(), target.lower(),
                                        "every non-null value is an ISO date/timestamp"))
        return refined

    @staticmethod
    def _check_column_names(names: list[str]) -> None:
        lowered = [n.casefold() for n in names]
        if len(set(lowered)) != len(lowered):
            raise InvalidSchemaError("The source has duplicate column names (case-insensitive).", field="path",
                                     details={"columns": names})
        for n in names:
            if not n.strip():
                raise InvalidSchemaError("The source has an empty column name.", field="path", details={"columns": names})

    def _build_columns(self, dataset_id: str, physical_columns: list[tuple[str, str]],
                       hints: dict[str, dict[str, Any]]) -> list[Column]:
        lowered_hints = {k.casefold(): v for k, v in hints.items()}
        unknown = sorted(set(lowered_hints) - {n.casefold() for n, _ in physical_columns})
        if unknown:
            raise InvalidSchemaError(f"schema_hints reference unknown columns {unknown}.", field="schema_hints",
                                     candidates=[n for n, _ in physical_columns])
        columns: list[Column] = []
        for position, (name, physical) in enumerate(physical_columns):
            logical = physical_to_logical(physical)
            hint = lowered_hints.get(name.casefold(), {})
            if "type" in hint:
                try:
                    logical = LogicalType(str(hint["type"]).lower())
                except ValueError as exc:
                    raise InvalidSchemaError(f"Unknown type hint {hint['type']!r} for column {name!r}.", field="schema_hints",
                                             details={"allowed": [str(t) for t in LogicalType]}) from exc
            role = self._infer_role(name, logical)
            if "semantic_role" in hint:
                try:
                    role = SemanticRole(str(hint["semantic_role"]).lower())
                except ValueError as exc:
                    raise InvalidSchemaError(f"Unknown semantic_role {hint['semantic_role']!r} for column {name!r}.",
                                             field="schema_hints", details={"allowed": [str(r) for r in SemanticRole]}) from exc
            columns.append(Column(
                id=self.store.allocate_id("col"), dataset_id=dataset_id, name=name, logical_type=logical,
                physical_type=physical, description=str(hint.get("description", "")), semantic_role=role,
                aliases=[str(a) for a in hint.get("aliases", [])], unit=str(hint.get("unit", "")), position=position,
            ))
        return columns

    @staticmethod
    def _infer_role(name: str, logical: LogicalType) -> SemanticRole:
        lowered = name.casefold()
        if lowered == "id" or lowered.endswith("_id") or lowered.endswith("uuid") or lowered.endswith("_key") \
                or lowered.endswith(("code", "代码", "编码", "编号")) or lowered.startswith("id_"):
            return SemanticRole.IDENTIFIER
        if logical.is_temporal or re.search(r"(^|_)(date|time|timestamp|day|month|year)($|_)", lowered) \
                or any(w in lowered for w in ("日期", "时间", "年度", "报告期", "截止日")):
            return SemanticRole.TIME
        if logical.is_numeric:
            return SemanticRole.MEASURE
        if logical in (LogicalType.STRING, LogicalType.BOOLEAN):
            return SemanticRole.DIMENSION
        return SemanticRole.UNKNOWN

    def _publish_problems(self, ds: Dataset) -> list[str]:
        problems: list[str] = []
        if not ds.description.strip():
            problems.append("description is empty")
        if not ds.columns:
            problems.append("dataset has no columns")
        version = self.store.get_version(ds.id, ds.version)
        if version is None:
            problems.append(f"version {ds.version} is not recorded")
        elif not self.engine.table_exists(version.physical_table):
            problems.append(f"physical table {version.physical_table} is missing")
        elif self.engine.row_count(version.physical_table) != version.row_count:
            problems.append("row count differs from the recorded version")
        for edge in self.store.lineage_into(ds.id):
            parent = self.store.get_dataset(edge.source_dataset_id, include_deleted=True)
            if parent is not None and parent.status == DatasetStatus.DELETED:
                problems.append(f"input dataset {parent.name} ({parent.id}) is deleted")
        return problems

    # -- summaries ---------------------------------------------------------
    @staticmethod
    def _summarize(ir: TransformIR, name: str | None) -> str:
        clauses: list[str] = []
        for step in ir.steps:
            if isinstance(step, FilterStep):
                clauses.append(f"filtering where {_expr_text(step.predicate)}")
            elif isinstance(step, SelectStep):
                clauses.append("selecting " + ", ".join(f.name for f in step.fields))
            elif isinstance(step, AggregateStep):
                verbs = {"sum": "summing", "avg": "averaging", "min": "taking min of", "max": "taking max of",
                         "count": "counting", "count_distinct": "counting distinct"}
                parts = [f"{verbs[str(m.function)]} {m.field.name if m.field else 'rows'} as {m.alias}" for m in step.measures]
                text = ", ".join(parts) if parts else "taking distinct groups"
                if step.group_by:
                    text += " grouped by " + ", ".join(f.name for f in step.group_by)
                clauses.append(text)
            elif isinstance(step, SortStep):
                clauses.append("sorting by " + ", ".join(f"{k.field.name} {k.direction}" for k in step.keys))
            elif isinstance(step, LimitStep):
                clauses.append(f"keeping the first {step.limit} rows")
            elif isinstance(step, RenameStep):
                clauses.append("renaming " + ", ".join(f"{m.field.name} to {m.to}" for m in step.mappings))
            elif isinstance(step, DeriveStep):
                clauses.append(f"deriving {step.name} = {_expr_text(step.expression)}")
            elif isinstance(step, JoinStep):
                clauses.append(f"{step.how}-joining {step.right.name} on " + ", ".join(f"{c.left.name} = {c.right.name}" for c in step.on))
            elif isinstance(step, SemiJoinStep):
                clauses.append(f"keeping rows matched by {step.right.name} on " + ", ".join(f"{c.left.name} = {c.right.name}" for c in step.on))
            elif isinstance(step, RawQueryStep):
                clauses.append("running a read-only query over " + ", ".join(
                    f"{b.placeholder} ({b.dataset.name} v{b.dataset.version})" for b in step.inputs))
        body = "; ".join(clauses) if clauses else "copying all rows"
        if name:
            return f"Created {name} from {ir.source.name} by {body}."
        return f"Previewed {ir.source.name} by {body}."
