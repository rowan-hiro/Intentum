"""MCP tool definitions.

Each tool is intentionally small: required semantic intent plus optional
hints. Handlers do no business logic; they forward to the backend and return
its structured response (success, needs_resolution, or error) as-is so the
agent can repair its intent.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ...core.backend import Backend

TOOL_ANNOTATIONS = {
    "read": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    "write": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    "delete": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
}


def register_tools(server: MCPServer, backend: Backend) -> None:
    """Register the semantic tool surface on a server for the given backend."""

    def annotations(kind: str):
        from mcp.types import ToolAnnotations

        return ToolAnnotations(**TOOL_ANNOTATIONS[kind])

    @server.tool(name="list_datasets", annotations=annotations("read"),
                 description="List available datasets with concise metadata (id, name, description, rows, columns, status, source).")
    def list_datasets(include_deleted: bool = False) -> dict[str, Any]:
        return backend.list_datasets(include_deleted=include_deleted)

    @server.tool(name="list_artifacts", annotations=annotations("read"),
                 description="List registered artifacts (source files, documents, media) and the datasets derived from "
                             "each. Optional `kind`: csv, json, parquet, sqlite, markdown, text, pdf, video, audio, image.")
    def list_artifacts(kind: str | None = None) -> dict[str, Any]:
        return backend.list_artifacts(kind=kind)

    @server.tool(name="describe_dataset", annotations=annotations("read"),
                 description="Describe one dataset: schema with types, semantic roles, units and descriptions, row count, "
                             "metadata, source artifact, recent versions, lineage summary, semantic hints and a small "
                             "sample. `dataset` may be an id, a name, an alias (any script), or a loose description "
                             "such as 'the orders I imported today'.")
    def describe_dataset(dataset: str, sample_rows: int = 5) -> dict[str, Any]:
        return backend.describe_dataset(dataset, sample_rows=sample_rows)

    @server.tool(name="search_datasets", annotations=annotations("read"),
                 description="Search datasets by name, description, aliases, column names, column descriptions and metadata.")
    def search_datasets(query: str, limit: int = 10) -> dict[str, Any]:
        return backend.search_datasets(query, limit=limit)

    @server.tool(name="import_dataset", annotations=annotations("write"),
                 description="Import one local file as a managed dataset: csv, parquet, json (array or "
                             "{table, records} wrapper) or a SQLite table (pass `table` when the file has several). "
                             "The backend inspects the source, infers the schema, keeps a managed copy, loads it, "
                             "assigns an id and records provenance atomically. Optional `schema_hints` is an object "
                             "keyed by column name with description/aliases/semantic_role/type/unit. Safe to retry: "
                             "identical content and name replays the original result.")
    def import_dataset(
        path: str,
        name: str | None = None,
        description: str | None = None,
        table: str | None = None,
        schema_hints: dict[str, Any] | None = None,
        aliases: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return backend.import_dataset(path, name=name, description=description, table=table,
                                      schema_hints=schema_hints, aliases=aliases, idempotency_key=idempotency_key)

    @server.tool(name="import_workspace", annotations=annotations("write"),
                 description="Import a whole directory (a task workspace) in one operation: every csv/json/parquet file "
                             "and every table of every SQLite file becomes a dataset; markdown, pdf, video and other "
                             "files are registered as artifacts. Names are derived deterministically and collisions "
                             "are qualified. Returns the datasets created, artifacts registered, sources that failed "
                             "and sources that already existed (re-running is idempotent).")
    def import_workspace(path: str, description: str | None = None, include_documents: bool = True) -> dict[str, Any]:
        return backend.import_workspace(path, description=description, include_documents=include_documents)

    @server.tool(name="attach_metadata", annotations=annotations("write"),
                 description="Read a knowledge / semantic-layer markdown document (for example knowledge.md) and attach "
                             "the table and column descriptions and units it contains to the matching datasets. "
                             "`source` is an artifact id, artifact name or file path. Reports every fact that did not "
                             "match a dataset or column so you can apply it with update_metadata. Existing descriptions "
                             "are kept unless `overwrite` is true.")
    def attach_metadata(source: str, dataset: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        return backend.attach_metadata(source, dataset=dataset, overwrite=overwrite)

    @server.tool(name="transform_dataset", annotations=annotations("write"),
                 description="Run a semantic transform on a dataset and preview the result (or persist it when "
                             "`output_name` is given). `source` is a dataset reference (id, name, alias or loose "
                             "description). `transform` is either one step object, a list of steps, or a compact "
                             "form such as {\"filter\": \"amount > 100\", \"group_by\": [\"region\"], \"metric\": "
                             "\"revenue\", \"sort\": \"-revenue\", \"limit\": 10}. Step types: select, filter, "
                             "aggregate (group_by + measures[{function, field, alias}]), sort, limit, rename, "
                             "derive ({name, expression}), join ({right, on, how}). Field names may be approximate; "
                             "the response lists how each was resolved, or returns needs_resolution with candidates.")
    def transform_dataset(
        source: str,
        transform: dict[str, Any] | list[dict[str, Any]],
        output_name: str | None = None,
        description: str | None = None,
        preview_limit: int = 20,
        explain: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return backend.transform_dataset(source, transform, output_name=output_name, description=description,
                                         preview_limit=preview_limit, explain=explain, idempotency_key=idempotency_key)

    @server.tool(name="materialize_result", annotations=annotations("write"),
                 description="Persist the result of a semantic transform as a new managed dataset named `name`. "
                             "Creates the dataset, its version, lineage, provenance and audit records atomically. "
                             "Retrying the same logical request returns the original result instead of creating a "
                             "duplicate.")
    def materialize_result(
        source: str,
        transform: dict[str, Any] | list[dict[str, Any]],
        name: str,
        description: str | None = None,
        explain: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return backend.materialize_result(source, transform, name, description=description, explain=explain,
                                          idempotency_key=idempotency_key)

    @server.tool(name="export_result", annotations=annotations("write"),
                 description="Write a managed dataset to a file (csv or parquet) at a path you name, e.g. a result "
                             "file another system expects. Paths are confined to the configured export root. The "
                             "dataset stays managed; the export is audited with its content hash. Existing files "
                             "are not replaced unless `overwrite` is true.")
    def export_result(dataset: str, path: str, format: str = "csv", overwrite: bool = False) -> dict[str, Any]:
        return backend.export_result(dataset, path, format=format, overwrite=overwrite)

    @server.tool(name="publish_dataset", annotations=annotations("write"),
                 description="Mark a dataset as stable and reusable after validating it (description present, "
                             "physical data consistent, inputs not deleted).")
    def publish_dataset(dataset: str) -> dict[str, Any]:
        return backend.publish_dataset(dataset)

    @server.tool(name="update_metadata", annotations=annotations("write"),
                 description="Update semantic metadata: dataset description, aliases, free-form metadata, and "
                             "per-column description/aliases/semantic_role/unit (`columns` is an object keyed by column name).")
    def update_metadata(
        dataset: str,
        description: str | None = None,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        columns: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return backend.update_metadata(dataset, description=description, aliases=aliases, metadata=metadata, columns=columns)

    @server.tool(name="delete_dataset", annotations=annotations("delete"),
                 description="Logically delete a dataset. Data is retained; the response tells how to restore it.")
    def delete_dataset(dataset: str, reason: str | None = None) -> dict[str, Any]:
        return backend.delete_dataset(dataset, reason=reason)

    @server.tool(name="restore_dataset", annotations=annotations("write"),
                 description="Restore a logically deleted dataset, optionally under a new name.")
    def restore_dataset(dataset: str, new_name: str | None = None) -> dict[str, Any]:
        return backend.restore_dataset(dataset, new_name=new_name)

    @server.tool(name="get_provenance", annotations=annotations("read"),
                 description="Explain where a dataset came from: the operation that produced it, the canonical "
                             "intent that was executed, input datasets, source artifact, upstream lineage and audit trail.")
    def get_provenance(dataset: str) -> dict[str, Any]:
        return backend.get_provenance(dataset)

    @server.tool(name="get_operation", annotations=annotations("read"),
                 description="Inspect a recorded operation: original intent, canonical IR, execution plan, result or error.")
    def get_operation(operation_id: str) -> dict[str, Any]:
        return backend.get_operation(operation_id)
