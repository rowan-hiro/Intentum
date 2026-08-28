"""The agent-ready backend: semantic operations over a deterministic core.

Every public method takes loose intent, runs the pipeline
resolve → canonical IR → validate → plan → execute → commit → audit,
and returns a structured, agent-friendly response. Errors never escape as
exceptions; they are rendered as structured error responses.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import ValidationError

from ..storage.duckdb.engine import AnalyticsEngine, DuckDBEngine, physical_to_logical
from ..storage.files.workspace import Workspace
from ..storage.metadata.interface import MetadataStore
from ..storage.metadata.sqlite import SqliteMetadataStore
from .access import AccessPolicy, AllowAllPolicy
from .audit import AuditService
from .errors import (
    BackendError,
    ConflictError,
    ErrorCode,
    InvalidIntentError,
    InvalidSchemaError,
    InvalidStateError,
    NotFoundError,
)
from .execution import Executor
from .ir import AggregateStep, DeriveStep, FilterStep, ImportIR, JoinStep, LimitStep, OutputMode, RenameStep, SelectStep, SortStep, TransformIR
from .lineage import LineageService
from .logging import log_event
from .models.entities import (
    Column,
    Dataset,
    DatasetStatus,
    DatasetVersion,
    LogicalType,
    Operation,
    OperationKind,
    OperationStatus,
    Relationship,
    SemanticRole,
)
from .planner import ExecutionPlan, Planner
from .planner.planner import _expr_text
from .resolver import DatasetResolver, FieldResolver, ResolutionNote, Scope, ScopeField, TransformResolver
from .resolver.common import slugify, tokens
from .validation import IRValidator

Clock = Callable[[], dt.datetime]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def semantic_operation(action: str):
    """Wrap a backend method: access check + structured error rendering."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self: "Backend", *args: Any, **kwargs: Any) -> dict[str, Any]:
            principal = kwargs.get("principal")
            try:
                self.policy.check(principal, action, {"args": _jsonable(kwargs)})
                return fn(self, *args, **kwargs)
            except BackendError as err:
                log_event("operation.error", code=str(err.code), message=err.message, field=err.field, action=action)
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

        return wrapper

    return decorator


class Backend:
    def __init__(
        self,
        workspace: Workspace | str | Path,
        *,
        clock: Clock | None = None,
        policy: AccessPolicy | None = None,
        store: MetadataStore | None = None,
        engine: AnalyticsEngine | None = None,
    ) -> None:
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.clock = clock or _utcnow
        self.policy = policy or AllowAllPolicy()
        self.store = store or SqliteMetadataStore(self.workspace.metadata_path)
        self.engine = engine or DuckDBEngine(self.workspace.analytics_path)
        self.datasets = DatasetResolver(self.store, self.clock)
        self.fields = FieldResolver()
        self.transforms = TransformResolver(self.datasets, self.fields)
        self.validator = IRValidator(self.store)
        self.planner = Planner()
        self.executor = Executor(self.engine)
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

    @semantic_operation("describe_dataset")
    def describe_dataset(self, dataset: Any, *, sample_rows: int = 5, principal: str | None = None) -> dict[str, Any]:
        notes: list[ResolutionNote] = []
        ds = self.datasets.resolve(dataset, field="dataset", notes=notes)
        version = self.store.get_version(ds.id, ds.version)
        versions = self.store.list_versions(ds.id)
        upstream = self.lineage.upstream(ds.id, depth=1)
        downstream = self.lineage.downstream(ds.id, depth=1)
        sample: dict[str, Any] | None = None
        if sample_rows > 0 and version is not None:
            cols, rows = self.engine.sample(version.physical_table, min(sample_rows, 100))
            sample = {"columns": cols, "rows": _jsonable([list(r) for r in rows])}
        body = {
            "status": "success",
            "dataset": {
                **self._dataset_summary(ds),
                "physical_location": ds.physical_location,
                "metadata": ds.metadata,
            },
            "schema": [self._column_summary(c) for c in ds.columns],
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
        if sample is not None:
            body["sample"] = sample
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
                "metadata": set(t for v in d.metadata.values() if isinstance(v, (str, int, float)) for t in tokens(str(v))),
            }
            weights = {"name": 3, "alias": 3, "description": 1.5, "column": 1, "metadata": 1}
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
            "upstream": upstream,
            "audit": self.audit.for_entity(ds.id),
        }
        if op and op.canonical_ir:
            body["canonical_intent"] = op.canonical_ir
        return self._with_notes(body, notes)

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
    # import_dataset
    # ======================================================================
    @semantic_operation("import_dataset")
    def import_dataset(
        self,
        path: str,
        *,
        name: str | None = None,
        description: str | None = None,
        format: str | None = None,
        schema_hints: dict[str, Any] | None = None,
        aliases: list[str] | None = None,
        idempotency_key: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        intent = _jsonable({"path": path, "name": name, "description": description, "format": format,
                            "schema_hints": schema_hints, "aliases": aliases})
        source = Path(str(path)).expanduser()
        if not source.is_file():
            raise NotFoundError(f"File {path!r} does not exist or is not a file.", field="path", recoverable=True)
        fmt = (format or source.suffix.lstrip(".")).lower()
        if fmt not in ("csv", "parquet"):
            raise InvalidSchemaError(f"Unsupported format {fmt!r}; supported formats are csv and parquet.", field="format")
        logical_name = slugify(name or source.stem)
        content_hash = self.workspace.content_hash(source)
        hints = self._normalize_hints(schema_hints)
        ir = ImportIR(source_path=str(source), format=fmt, name=logical_name, description=description or "",
                      content_hash=content_hash, column_hints=hints)
        key = idempotency_key or f"import:{ir.logical_fingerprint()}"

        replay = self._replay(key, ir.logical_fingerprint())
        if replay is not None:
            return replay

        existing = self.store.get_dataset_by_name(logical_name)
        if existing is not None:
            raise ConflictError(
                f"A dataset named {logical_name!r} already exists ({existing.id}) with different content.",
                field="name",
                details={"existing": self._dataset_summary(existing)},
                hint="Choose another name, or delete_dataset the existing one first.",
            )

        with self._operation(OperationKind.IMPORT, intent, principal, key) as op:
            log_event("resolution.completed", operation_id=op.id, name=logical_name, format=fmt, content_hash=content_hash)
            op.canonical_ir = ir.model_dump(mode="json")
            physical_columns = self.engine.inspect_file(source, fmt)
            if not physical_columns:
                raise InvalidSchemaError("The file has no columns.", field="path")
            self._check_column_names([c for c, _ in physical_columns])
            managed = self.workspace.import_file(source, content_hash)
            dataset_id = self.store.allocate_id("ds")
            table = f"{dataset_id}_v1"
            plan = ExecutionPlan(
                steps=[],
                physical_inputs={},
                output_table=table,
            )
            from .planner import PlanStep

            plan.steps = [
                PlanStep("InspectFile", f"InspectFile({source.name})", {"columns": [c for c, _ in physical_columns]}),
                PlanStep("CopyToWorkspace", f"CopyToWorkspace({managed.name})"),
                PlanStep("LoadTable", f"LoadTable({table})", {"format": fmt}),
                PlanStep("RegisterDataset", f"RegisterDataset({logical_name} as {dataset_id})"),
                PlanStep("Audit", "Audit(dataset.created, version.created)"),
            ]
            op.execution_plan = plan.to_dict()
            self._save_operation(op)
            log_event("plan.created", operation_id=op.id, plan=plan.to_text())

            row_count = self.engine.import_file(managed, fmt, table)
            log_event("execution.completed", operation_id=op.id, table=table, rows=row_count)
            now = self.clock()
            try:
                with self.store.transaction():
                    columns = self._build_columns(dataset_id, physical_columns, hints)
                    metadata: dict[str, Any] = {"source_file": source.name, "content_hash": content_hash, "format": fmt}
                    if aliases:
                        metadata["aliases"] = [str(a) for a in aliases]
                    dataset = Dataset(
                        id=dataset_id, name=logical_name, description=description or "", status=DatasetStatus.ACTIVE,
                        version=1, created_at=now, updated_at=now, physical_location=f"duckdb://{table}", origin="import",
                        metadata=metadata, columns=columns,
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
                                      details={"name": logical_name, "origin": "import", "rows": row_count})
                    self.audit.record(event_type="version.created", entity_type="dataset", entity_id=dataset_id,
                                      operation_id=op.id, now=now, actor=principal, details={"version": 1, "table": table})
                    response = {
                        "status": "success",
                        "operation_id": op.id,
                        "dataset": {"id": dataset_id, "name": logical_name, "rows": row_count,
                                    "columns": [c.name for c in columns], "version": 1},
                        "schema": [self._column_summary(c) for c in columns],
                        "summary": f"Imported {source.name} as {logical_name} ({row_count} rows, {len(columns)} columns).",
                    }
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
                                       preview_limit, context, explain, idempotency_key, principal)
        return self._run_transform(OperationKind.TRANSFORM, source, transform, None, None,
                                   preview_limit, context, explain, idempotency_key, principal)

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
                                   preview_limit, context, explain, idempotency_key, principal)

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
    ) -> dict[str, Any]:
        intent = _jsonable({"source": source, "transform": transform, "output_name": output_name,
                            "description": description, "context": context})
        if not isinstance(preview_limit, int) or preview_limit < 0 or preview_limit > 1000:
            raise InvalidIntentError("preview_limit must be an integer between 0 and 1000.", field="preview_limit")
        notes: list[ResolutionNote] = []
        with self._operation(kind, intent, principal, idempotency_key) as op:
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

            result = self.executor.execute_transform(ir, plan)
            log_event("execution.completed", operation_id=op.id, rows=result.row_count, table=result.physical_table)

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
                    response["plan"] = plan.to_text()
                    if explain:
                        response["explain"] = {"canonical_ir": op.canonical_ir, "execution_plan": plan.to_dict(), "sql": result.sql}
                    self._complete(op, response, key, ir.logical_fingerprint(), now)
            except Exception:
                self.executor.rollback_table(table)
                raise
            log_event("state.committed", operation_id=op.id, dataset_id=dataset_id)
            return self._with_notes(response, notes)

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
                aliases=list(origin.aliases) if origin else [], position=position,
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
        for ref in ir.referenced_datasets():
            relationship = Relationship.DERIVED_FROM if ref.dataset_id == ir.source.dataset_id else Relationship.JOINED_WITH
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
    # publish / update_metadata / delete / restore
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
        reserved = {"aliases", "source_file", "content_hash", "format", "derived", "intent_fingerprint"}
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
                unknown = sorted(set(patch) - {"description", "aliases", "semantic_role"})
                if unknown:
                    raise InvalidIntentError(f"Unknown column metadata keys {unknown}.", field="columns",
                                             details={"allowed_keys": ["description", "aliases", "semantic_role"]})
                target = self.fields.resolve(ref, scope, field="columns", notes=notes)
                column = next(c for c in ds.columns if c.id == target.column_id)
                if "description" in patch:
                    column.description = str(patch["description"])
                if "aliases" in patch:
                    column.aliases = [str(a) for a in (patch["aliases"] or [])]
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
            from .errors import AmbiguousReferenceError

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
        for d in self.store.list_datasets(include_deleted=True):
            if self.store.get_version(d.id, d.version) is None:
                issues.append({"kind": "missing_version", "dataset_id": d.id, "version": d.version})
            if not d.columns:
                issues.append({"kind": "no_columns", "dataset_id": d.id})
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
                   idempotency_key: str | None = None) -> Iterator[Operation]:
        op = Operation(id=self.store.allocate_id("op"), kind=kind, status=OperationStatus.PENDING,
                       original_intent=intent, idempotency_key=idempotency_key, principal=principal, created_at=self.clock())
        with self.store.transaction():
            self.store.insert_operation(op)
        log_event("intent.received", operation_id=op.id, kind=str(kind), intent=intent, principal=principal)
        try:
            yield op
        except BackendError as err:
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

    # -- projections -----------------------------------------------------
    def _dataset_summary(self, d: Dataset) -> dict[str, Any]:
        version = self.store.get_version(d.id, d.version)
        return {
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

    @staticmethod
    def _column_summary(c: Column) -> dict[str, Any]:
        body: dict[str, Any] = {"id": c.id, "name": c.name, "type": str(c.logical_type), "role": str(c.semantic_role)}
        if c.description:
            body["description"] = c.description
        if c.aliases:
            body["aliases"] = c.aliases
        return body

    @staticmethod
    def _operation_summary(op: Operation) -> dict[str, Any]:
        return {
            "id": op.id,
            "kind": str(op.kind),
            "status": str(op.status),
            "original_intent": op.original_intent,
            "plan": " → ".join(s["description"] for s in (op.execution_plan or {}).get("steps", [])) or None,
            "created_at": op.created_at.isoformat(),
            "completed_at": op.completed_at.isoformat() if op.completed_at else None,
            "principal": op.principal,
        }

    @staticmethod
    def _semantic_hints(d: Dataset) -> dict[str, Any]:
        measures = [c.name for c in d.columns if c.semantic_role == SemanticRole.MEASURE]
        dimensions = [c.name for c in d.columns if c.semantic_role == SemanticRole.DIMENSION]
        times = [c.name for c in d.columns if c.semantic_role == SemanticRole.TIME]
        identifiers = [c.name for c in d.columns if c.semantic_role == SemanticRole.IDENTIFIER]
        suggestions = [f"sum({m}) by {dim}" for m in measures[:2] for dim in dimensions[:2]]
        return {"measures": measures, "dimensions": dimensions, "time_columns": times, "identifiers": identifiers,
                "suggested_aggregations": suggestions}

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
            unknown = sorted(set(patch) - {"description", "aliases", "semantic_role", "type"})
            if unknown:
                raise InvalidIntentError(f"Unknown schema hint keys {unknown} for column {name!r}.", field="schema_hints",
                                         details={"allowed_keys": ["description", "aliases", "semantic_role", "type"]})
            hints[str(name)] = _jsonable(patch)
        return hints

    @staticmethod
    def _check_column_names(names: list[str]) -> None:
        lowered = [n.lower() for n in names]
        if len(set(lowered)) != len(lowered):
            raise InvalidSchemaError("The file has duplicate column names (case-insensitive).", field="path",
                                     details={"columns": names})
        for n in names:
            if not n.strip():
                raise InvalidSchemaError("The file has an empty column name.", field="path", details={"columns": names})

    def _build_columns(self, dataset_id: str, physical_columns: list[tuple[str, str]],
                       hints: dict[str, dict[str, Any]]) -> list[Column]:
        lowered_hints = {k.lower(): v for k, v in hints.items()}
        unknown = sorted(set(lowered_hints) - {n.lower() for n, _ in physical_columns})
        if unknown:
            raise InvalidSchemaError(f"schema_hints reference unknown columns {unknown}.", field="schema_hints",
                                     candidates=[n for n, _ in physical_columns])
        columns: list[Column] = []
        for position, (name, physical) in enumerate(physical_columns):
            logical = physical_to_logical(physical)
            hint = lowered_hints.get(name.lower(), {})
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
                aliases=[str(a) for a in hint.get("aliases", [])], position=position,
            ))
        return columns

    @staticmethod
    def _infer_role(name: str, logical: LogicalType) -> SemanticRole:
        lowered = name.lower()
        if lowered == "id" or lowered.endswith("_id") or lowered.endswith("uuid") or lowered.endswith("_key"):
            return SemanticRole.IDENTIFIER
        if logical.is_temporal or re.search(r"(^|_)(date|time|timestamp|day|month|year)($|_)", lowered):
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
                text = ", ".join(parts)
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
        body = "; ".join(clauses) if clauses else "copying all rows"
        if name:
            return f"Created {name} from {ir.source.name} by {body}."
        return f"Previewed {ir.source.name} by {body}."
