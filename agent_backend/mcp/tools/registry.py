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
                             "keyed by column name with description/aliases/semantic_role/type/unit. Text columns "
                             "whose values are all ISO dates or timestamps become date/timestamp columns; a `type` "
                             "hint overrides that. Safe to retry: identical content and name replays the original "
                             "result.")
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

    @server.tool(name="declare_output", annotations=annotations("write"),
                 description="Declare the shape of the deliverable before you work towards it. `columns` is the "
                             "ordered list of column names the answer file will CARRY, exactly and only those "
                             "(strings, or {\"name\", \"type\"} objects with type integer/float/string/boolean/"
                             "date/timestamp). `rows` is required and says how many rows the answer has: \"one\", "
                             "\"at_least_one\", or {\"one_per\": [key columns]}. `order_by` names the columns the "
                             "answer is SORTED by ([\"treatmentid\"], or [{\"column\": ..., \"direction\": "
                             "\"desc\"}]; a leading \"-\" also means descending). A column that only orders or "
                             "groups the answer does not belong in `columns`: put it in `order_by` or in the "
                             "`one_per` keys and the backend will sort and count by it and leave it out of the "
                             "file, as long as the dataset you export carries it. Optional `description`. The "
                             "backend keeps the contract and export_result refuses a dataset that does not match "
                             "it, so you cannot drift away from it later. One contract is current per workspace; "
                             "declaring a different shape while one is open needs `reason`, which is recorded as "
                             "an amendment. A reason may explain a changed requirement or correct an earlier "
                             "misinterpretation; a mismatch with the current dataset alone is not a reason to amend. "
                             "Read the returned contract, summary and any names facts against the original request "
                             "before dependent calls; keep the declaration if it matches, otherwise amend with the "
                             "reason. Re-declaring the same shape changes nothing.")
    def declare_output(columns: list[Any] | dict[str, Any] | str, rows: str | int | dict[str, Any] | None = None,
                       order_by: list[Any] | dict[str, Any] | str | None = None,
                       description: str | None = None, reason: str | None = None) -> dict[str, Any]:
        return backend.declare_output(columns, rows=rows, order_by=order_by, description=description, reason=reason)

    # ``source`` and ``transform`` are typed loosely on purpose: a JSON string that does not parse, or a relation
    # written where a name belongs, must reach the backend, which refuses it with advice (MADR 0010). Typed
    # narrowly, the MCP SDK would refuse first, with pydantic text and no advice.
    @server.tool(name="transform_dataset", annotations=annotations("write"),
                 description="Run a semantic transform on a dataset and preview the result (or persist it when "
                             "`output_name` is given). `source` is a dataset reference (id, name, alias or loose "
                             "description). `transform` is either one step object, a list of steps, or a compact "
                             "form such as {\"filter\": \"amount > 100\", \"group_by\": [\"region\"], \"metric\": "
                             "\"revenue\", \"sort\": \"-revenue\", \"limit\": 10}. Step types: select, filter, "
                             "aggregate (group_by + optional measures[{function, field, alias}]; group_by alone "
                             "returns distinct groups), sort, limit, rename, derive ({name, expression}), join "
                             "({right, on, how}), semi_join ({right, on}; keeps left rows with a right match without "
                             "copying right columns; materialize a filtered right input first when needed), and "
                             "raw_query, the fallback for shapes the other steps cannot express, such as the latest "
                             "row per group (a window), every row tied at a maximum, or a union: {\"raw_query\": "
                             "{\"sql\": \"SELECT * FROM input UNION ALL SELECT * FROM archive\", \"inputs\": "
                             "{\"archive\": \"orders_2025\"}}}. Its sql is one read-only DuckDB SELECT or WITH "
                             "statement that names datasets only by placeholder: input is `source`, and each name "
                             "under `inputs` is bound to a dataset reference; raw_query must be the first step, "
                             "semantic steps may follow it, it runs in a sandbox with no file, network or setting "
                             "access, and the response says used_raw_query: true. Use the other steps whenever they "
                             "can express the request. A step may also be written "
                             "without `type` when its key names it, e.g. [{\"filter\": \"...\"}, {\"sort\": \"-amount\"}] "
                             "or {\"aggregate\": {\"group_by\": [...], \"measures\": [...]}}; select and derive accept "
                             "\"field as alias\", and group_by accepts a named expression such as "
                             "\"date_trunc('day', ts) as day\". In a compact object, select runs after aggregate "
                             "when it names an aggregate output, before it otherwise, and after sort when sort names a "
                             "field select drops. Expressions: comparison, arithmetic, and/or/not, "
                             "in (a, b) or in [a, b], cast(x as integer|float|string|boolean|date|timestamp) (SQL names such as "
                             "int, double and varchar also work; x::type is the same cast) and try_cast(x as type), which "
                             "yields null where cast would fail, and the functions abs round floor ceil upper lower trim length "
                             "substr left right concat (or ||) coalesce year month day date date_trunc strftime "
                             "is_null contains starts_with ends_with. Field names may be approximate; the response "
                             "lists how each was resolved, or returns needs_resolution with candidates. Transforms "
                             "compute values; how they are rendered as text is export_result's business. `transform` is required; "
                             "an empty list previews the source as it is. `source` names one dataset (id, name or description); "
                             "a query, a join or a step object written there is refused with the request rewritten. A refused transform "
                             "comes back with `advice`: what the backend accepts instead and, when mechanical, a "
                             "`rewrite` to send as-is. A successful response may carry advice too: an empty result "
                             "says where a filtered value does occur; a result with the declared output shape says so.")
    def transform_dataset(
        source: str | dict[str, Any] | list[Any],
        transform: dict[str, Any] | list[Any] | str | None = None,
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
                             "`transform` is required and takes the same steps as transform_dataset; when the steps cannot express a "
                             "shape, a raw_query first step ({\"raw_query\": {\"sql\": \"SELECT ... FROM input\", "
                             "\"inputs\": {\"name\": \"dataset\"}}}, one read-only SELECT or WITH statement over "
                             "placeholders) is the fallback, and every dataset it binds is recorded as an input. "
                             "Retrying the same logical request returns the original result instead of creating a "
                             "duplicate.")
    def materialize_result(
        source: str | dict[str, Any] | list[Any],
        name: str,
        transform: dict[str, Any] | list[Any] | str | None = None,
        description: str | None = None,
        explain: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return backend.materialize_result(source, transform, name, description=description, explain=explain,
                                          idempotency_key=idempotency_key)

    @server.tool(name="export_result", annotations=annotations("write"),
                 description="Write a managed dataset to a file (csv or parquet) at a path you name, e.g. a result "
                             "file another system expects. The file carries exactly the dataset's columns, in "
                             "order: shape the dataset first (select/rename) so it has exactly the columns the "
                             "answer asks for. When an output contract was declared with declare_output, the "
                             "dataset is checked against it (columns, order, types, row cardinality) before "
                             "anything is written and a mismatch is returned as CONTRACT_MISMATCH with the "
                             "transform that would repair it; a matching export closes the contract with the "
                             "evidence in the audit trail. Paths are confined to the configured export root. The "
                             "dataset stays managed; the export is audited with its content hash. Existing files "
                             "are not replaced unless `overwrite` is true. Rendering values as text belongs here, "
                             "not in a transform: optional `format_spec` (csv only) holds file-level defaults plus "
                             "per-column overrides under `columns`, with keys `decimals` (rounded half-up), "
                             "`strip_trailing_zeros`, `integer_min_decimals` (whole numbers keep at least N "
                             "decimals), `date_format` / `timestamp_format` (strftime patterns such as %Y-%m-%d) "
                             "and `null_text`. Example: {\"decimals\": 4, \"strip_trailing_zeros\": true, "
                             "\"integer_min_decimals\": 1, \"columns\": {\"end_date\": {\"date_format\": \"%Y-%m-%d\"}}}. "
                             "The specification is recorded with the operation, so the file is reproducible.")
    def export_result(dataset: str, path: str, format: str = "csv", format_spec: dict[str, Any] | None = None,
                      overwrite: bool = False) -> dict[str, Any]:
        return backend.export_result(dataset, path, format=format, format_spec=format_spec, overwrite=overwrite)

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

    @server.tool(name="get_output_contract", annotations=annotations("read"),
                 description="Show the output contract this workspace currently holds (or one by `contract_id`): "
                             "the declared columns, row cardinality, revision, whether an export has satisfied it, "
                             "and its history of declarations and amendments. Use it to re-anchor on what the "
                             "deliverable must look like.")
    def get_output_contract(contract_id: str | None = None) -> dict[str, Any]:
        return backend.get_output_contract(contract_id=contract_id)

    @server.tool(name="get_operation", annotations=annotations("read"),
                 description="Inspect a recorded operation: original intent, canonical IR, execution plan, result or error.")
    def get_operation(operation_id: str) -> dict[str, Any]:
        return backend.get_operation(operation_id)
