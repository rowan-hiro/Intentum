# Agent-ready backend

A prototype execution substrate that sits between a probabilistic AI agent and
deterministic data systems. The agent expresses **intent**; the backend
resolves it into a **canonical operation**, validates it, plans it, executes it
deterministically, commits it atomically, and returns a structured result.

> Agent expresses intent. Backend owns correctness.

What the backend owns is consistency, not the truth of the task: it never sees
the question the agent is answering, so it takes the agent's *fresh* output —
how it read the requirement, what it just computed — as given, and holds the
agent to everything it later reproduces from memory. An export is checked
against the output contract declared at the start; a replayed request against
its recorded fingerprint. The failure this exists to catch is *knew but did
not do*; *did not know* belongs to the model and the agent framework
(`.seal/madr/0004`, `0007`, `0008`).

Research hypothesis under test: a general-purpose agent interacts with a
structured data system more reliably through a small semantic intent interface
plus a deterministic backend than by manipulating SQL, JSON, Markdown, metadata
files and storage primitives directly.

## 1. Architecture

```
loose intent (MCP tool call)
   │  {"source": "yesterday's sales", "transform": {"group_by": ["region"], "metric": "revenue"}}
   ▼
Resolve      core/resolver     fuzzy dataset/field/expression references → concrete ids and types
   │                           (records every lenient decision; refuses to guess on material ambiguity)
   ▼
Canonical IR core/ir           strict, typed, extra="forbid" pydantic models; every field carries its type
   │
Validate     core/validation   re-derives every type and schema with the shared type rules;
   │                           rejects stale versions, deleted inputs, tampered IR
   ▼
Plan         core/planner      explicit, inspectable step list: Scan → HashAggregate → Materialize → RegisterLineage → Audit
   │
Execute      core/execution    IR → SQL (internal IR only) → DuckDB; CREATE TABLE AS or preview query
   │
Commit       core/backend      one SQLite transaction: dataset + columns + version + lineage + audit + operation + idempotency key
   │                           failure after execution ⇒ physical table dropped (compensation)
   ▼
structured response            success | needs_resolution | error{code, message, hint, candidates, recoverable}
```

Key properties, all enforced in software rather than in prompts:

| Concern | Where it lives |
|---|---|
| Identifiers, physical table names, storage paths, timestamps | allocated by the backend (`ds_N`, `col_N`, `art_N`, `op_N`, `ds_N_v1`), never accepted from the agent |
| Name normalization / uniqueness | Unicode-aware `slugify` (`公募基金经理(新)` → `公募基金经理_新`, `Regional Sales` → `regional_sales`) + partial unique index on active dataset names; original names stay reachable as aliases |
| Source provenance | every imported dataset points at an `Artifact` (file + content hash + optional managed copy) and a locator (SQLite table); documents and media are artifacts too |
| Semantic layer | `attach_metadata` ingests a `knowledge.md`-style document into dataset/column descriptions and units, reporting every fact that did not match |
| Schema and type correctness | shared type rules in `core/ir/typing.py`, applied twice (resolver inference, validator re-check) |
| Semantic types at import | text columns whose non-null values are all ISO dates/timestamps become `date`/`timestamp` (a source driver reports what it can store, not what the data means); an explicit `type` hint wins, and the refinement is a resolution note and part of the recorded IR |
| Value rendering | a property of the exported file, not of the data: `export_result` takes a validated `format_spec`; transforms compute, they do not format |
| Ambiguity | `AmbiguousReferenceError` → `status: needs_resolution` with candidates |
| Write-ahead operation record | operation row is inserted `pending` before any work, updated with IR and plan before execution, and finalized in the commit transaction |
| Atomicity | DuckDB `CREATE TABLE AS` is atomic; metadata commit is one SQLite transaction; failure between the two drops the table |
| Idempotency | key = explicit `idempotency_key` or `sha256(canonical IR)`; replay returns the original response, key reuse with a different request is a `CONFLICT` |
| Lineage / provenance | `lineage_edges` per input dataset + operation record holding original intent, canonical IR and plan |
| Audit | `audit_events` for every state change, including failed operations |
| Access control | `AccessPolicy.check(principal, action, resource)` hook, consulted before every operation |
| Observability | structured log events per stage: `intent.received`, `resolution.completed`, `ir.canonical`, `validation.completed`, `plan.created`, `execution.completed`, `state.committed`, `operation.failed` |
| Storage abstraction | `MetadataStore` and `AnalyticsEngine` protocols; SQLite and DuckDB are implementations |

### Loose intent vs canonical IR

Input the agent sends:

```json
{"source": "the orders I imported today",
 "transform": {"group_by": ["region"], "metric": "revenue"},
 "name": "regional_sales"}
```

What is executed (abridged, obtainable with `explain: true`):

```json
{"operation": "transform",
 "source": {"dataset_id": "ds_1", "version": 1, "name": "orders"},
 "steps": [{"type": "aggregate",
            "group_by": [{"name": "region", "logical_type": "string", "column_id": "col_3"}],
            "measures": [{"function": "sum",
                          "field": {"name": "amount", "logical_type": "float", "column_id": "col_8"},
                          "alias": "revenue", "logical_type": "float"}],
            "output_schema": [...]}],
 "output": {"mode": "materialized", "name": "regional_sales"}}
```

The response tells the agent how each fuzzy reference was resolved:

```json
"resolution": [
  {"field": "source", "reference": "the orders I imported today",
   "resolved_to": "orders (ds_1)", "reason": "name/alias contains 'orders'; created today; was imported"},
  {"field": "transform.steps[0] (aggregate).measures[0]", "reference": "revenue",
   "resolved_to": "amount", "reason": "synonym of 'amount'"}]
```

### Resolution rules (in order)

Datasets: exact id → exact name → alias (an alias shared by several datasets is
reported as ambiguous, never picked) → normalized name → token scoring over
name/aliases/description/columns with temporal words (`today`/`今天`,
`yesterday`/`昨天`), origin words (`imported`/`导入`, `result`/`结果`), status
words (`published`/`发布`), and recency words (`latest`/`最新`, `earlier`/`之前`)
→ single clear winner, or a recency tie-break when the reference asks for it,
otherwise `needs_resolution`. Tokens are Unicode-aware: CJK runs contribute
character bigrams, so `股本变动` matches `北京股本变动`. A reference that hits a
deleted dataset returns `NOT_FOUND` with `restorable: true`.

Fields (resolved against the schema that is current at each step, so you can
sort by an aggregate alias): exact → case-insensitive/alias → normalized → small
built-in synonym groups (only if exactly one field matches) → fuzzy match (only
if exactly one) → `NOT_FOUND` with the list of available fields.

### Transform language

`transform` is one step object, a list of steps, or a compact object whose keys
are applied in the default order join → semi_join → filter → derive → select →
aggregate → sort → limit → rename. If that compact object's `select` names a
measure produced by its aggregate, the projection moves after the aggregate;
if its `sort` names a field the projection would drop, the projection moves
after the sort (a sort by a select alias keeps the projection first); an
explicit list of steps always keeps the written order:

```json
{"filter": "quantity >= 5 and region in ('West','East')",
 "group_by": ["region"],
 "measures": [{"function": "sum", "field": "amount", "alias": "revenue"}, {"count": "*", "as": "orders"}],
 "sort": "-revenue",
 "limit": 10}
```

Step types: `select`, `filter`, `aggregate`, `sort`, `limit`, `rename`,
`derive`, `join`, `semi_join`. A step may also be written without `type` when its key names
it, so `[{"filter": "amount > 100"}, {"sort": "-amount"}, {"limit": 3}]` is a
pipeline and `{"aggregate": {"group_by": [...], "measures": [...]}}` is an
aggregate step with its body nested under its own key; `select` and `derive`
accept SQL-style aliasing (`"end_date as report_period"`,
`"quantity * unit_price as line_total"`), `derive` accepts several
`{name, expression}` pairs at once, and `group_by` accepts a named expression
(`"date_trunc('day', observation_time) as day"`), which is derived ahead of
the aggregate. An aggregate with `group_by` and no measures returns the
distinct groups; an aggregate with neither is invalid.

`semi_join` keeps a left row when the referenced right dataset has at least
one row whose key or keys match, without copying right columns or multiplying
the left row when the right key is duplicated:

```json
{"type": "semi_join", "right": "allowed_customers",
 "on": {"customer_id": "customer_id"}}
```

The right dataset and version remain explicit in the canonical IR, plan and
lineage. When matches need a condition on the right side, filter and
materialize that dataset first, then reference the managed result here.

Expressions may be strings (`"quantity * unit_price"`,
`"amount > 100 and region in ['West', 'East']"`) or object trees; identifiers
are always resolved against the current schema and functions come from an
allowlist (`abs round floor ceil upper lower trim length substr substring left
right concat coalesce year month day date date_trunc strftime is_null contains
starts_with ends_with`; `||` is read as `concat`). A two-argument call written
the other way round (`strftime('%Y-%m-%d', ts)`) is reordered to its signature
and reported as a resolution note; a type error names the signature it
expected. There is no SQL passthrough.

### Exporting an answer

`export_result` writes a managed dataset to a file at the boundary of the
system. How values become text is decided there, by an optional `format_spec`
that is validated like the IR (unknown keys refused) and recorded in the
operation and the audit event, so the file is reproducible:

```json
{"dataset": "task_10_answer", "path": "prediction.csv",
 "format_spec": {"decimals": 4, "strip_trailing_zeros": true, "integer_min_decimals": 1,
                 "columns": {"end_date": {"timestamp_format": "%Y-%m-%d"}}}}
```

Keys are file-level defaults plus per-column overrides: `decimals` (rounded
half-up on the shortest decimal form, so `31783696.815` → `31783696.82` and not
the `.81` the binary double would give), `strip_trailing_zeros`,
`integer_min_decimals`, `date_format` / `timestamp_format` (strftime patterns,
locale-dependent directives refused) and `null_text`. The dataset itself keeps
its types; two differently formatted exports of the same version differ only in
the file.

### Output contracts

The shape of the deliverable is a contract the agent can write down while the
requirement is in front of it, and the backend holds every export to it
(MADR 0007):

```json
{"columns": ["treatmentname"], "rows": {"one_per": ["treatmentname"]}}
```

`declare_output` records the ordered column names (optionally typed), the row
cardinality (`one`, `at_least_one`, `{"one_per": [keys]}`) and a description
as an operation and an audit event. `export_result` then checks the dataset
against the current contract — names, order, declared types by family, row
cardinality — before anything is read or written. A mismatch is a recoverable
`CONTRACT_MISMATCH` that shows the declared and the actual shape, lists the
problems, and, when the fix is mechanical, carries the transform that repairs
it (`{"select": [...]}`, with `rename` for near-miss names); a matching export
records the contract as satisfied with the evidence (columns, rows, content
hash) in the audit trail. Changing an open contract needs a `reason`, recorded
as an amendment with the shape before and after; re-declaring the same shape
changes nothing; a new contract can be declared freely once the previous one is
satisfied. Exports without a contract behave as before.

This is the trust model of MADR 0008 made concrete: the declaration made
fresh is the reference, the export attempted twenty turns later is the thing
that gets checked, and the check runs outside the agent's context.

### Failure semantics

Every failure is a structured response, never an exception string:

```json
{"status": "error", "code": "NOT_FOUND", "recoverable": true,
 "field": "transform.steps[0] (aggregate).group_by",
 "message": "No field named 'colour' is available at this step.",
 "candidates": [{"name": "region", "type": "string", "id": "col_3", "role": "dimension"}, ...],
 "hint": "Use one of the listed field names."}
```

Codes: `NOT_FOUND`, `AMBIGUOUS_REFERENCE` (rendered as `status: needs_resolution`),
`INVALID_INTENT`, `INVALID_SCHEMA`, `INVALID_TRANSFORM`, `INVALID_STATE`,
`TYPE_MISMATCH`, `CONFLICT`, `CONTRACT_MISMATCH`, `PERMISSION_DENIED`,
`EXECUTION_FAILED`, `INTERNAL`.

A refusal also teaches (MADR 0010). What an agent may write is an open set;
what the backend accepts is small and closed, so every refusal is mapped onto
the nearest accepted shape by `core/recovery` and carried as `advice`: the
kind of mistake, what the backend read and what it accepts instead, and, when
the mapping is mechanical, `rewrite`, the agent's own request restated as the
tool calls to send as-is:

```json
{"status": "error", "code": "INVALID_TRANSFORM",
 "message": "Expected ')' at position 29 in expression \"patientunitstayid IN (SELECT ...\".",
 "advice": [{"kind": "subquery_as_semi_join",
             "explanation": "A subquery is not an expression in this language. To keep rows of the source whose patientunitstayid appears in cost.patientunitstayid, add a semi_join step ...",
             "rewrite": [{"tool": "materialize_result", "arguments": {"source": "cost", "transform": {"filter": "uniquepid = '025-44842'"}, "name": "cost_filtered"}},
                         {"tool": "transform_dataset", "arguments": {"source": "treatment", "transform": [{"semi_join": {"right": "cost_filtered", "on": {"patientunitstayid": "patientunitstayid"}}}, {"select": ["treatmentid", "treatmentname"]}]}}]}]}
```

Detectors so far: a subquery in an expression (semi_join), `distinct` as a key
or prefix (measureless `group_by`), a join `on` written as an equality (key
mapping), `LIKE` (`contains` / `starts_with` / `ends_with`), an aggregate
function inside `select` (aggregate step), an expression with an alias inside
`select` (derive step), a SQL `LIMIT` tail inside a filter (limit step), an
inline relation as `source` (materialize it first), an unknown key (nearest
accepted key), a string function on a date or
timestamp (how dates are computed, and that rendering belongs to export), and a
contract mismatch at export (the reshape and the export). A detector that
cannot rewrite still explains. Every rewrite is executed in its test and must
succeed.

Two silent failures get the same treatment on *successful* responses: an empty
result whose filter literal is absent from the filtered column says where in
the workspace that literal does occur (`value_not_found`), and a result that
already has, or mechanically reshapes to, the open output contract says so with
the next call (`matches_contract`, `near_contract`). The backend teaches its own
language and reports its own data; it still never reads the task. Pacing (the
turn budget, repeated previews) belongs to the agent harness.

## 2. Repository structure

```
agent_backend/
├── core/
│   ├── backend.py          semantic operations; operation lifecycle; commit/rollback; idempotency
│   ├── access.py           AccessPolicy hook (AllowAll, DenyActions)
│   ├── errors.py           error codes and structured error rendering
│   ├── logging.py          structured stage events
│   ├── naming.py           Unicode-aware identifier rules (normalize, slugify, tokens)
│   ├── models/             Dataset, Column, Artifact, DatasetVersion, LineageEdge, Operation, AuditEvent
│   ├── ir/                 canonical IR (pydantic), expression parser, shared type rules
│   ├── knowledge/          heuristic parser for knowledge.md-style semantic-layer documents
│   ├── resolver/           dataset / field / expression / transform resolvers
│   ├── validation/         IR validator (independent re-check)
│   ├── planner/            explicit execution plan
│   ├── execution/          IR → SQL compiler, executor
│   ├── export/             export format specification (value rendering at the file boundary)
│   ├── contracts/          output contracts: declared deliverable shape, checked at export
│   ├── recovery/           teach on refusal: structured advice and rewrites on refusals and silent failures
│   ├── lineage/            lineage recording and traversal
│   └── audit/              audit trail
├── storage/
│   ├── metadata/           MetadataStore protocol + SQLite implementation
│   ├── duckdb/             AnalyticsEngine protocol + DuckDB implementation
│   └── files/              managed workspace (metadata.sqlite, analytics.duckdb, files/)
├── mcp/
│   ├── server/             MCPServer entry point (`agent-backend-mcp`)
│   └── tools/              thin tool definitions
agent_harness/              the agent side, kept apart from the backend (MADR 0009); not in the wheel
├── config.py               .env and model gateway settings
├── model.py                OpenAI-compatible chat client (stdlib only)
├── loop.py                 tool-calling loop over an MCP server
├── perception/             readers of unstructured sources (documents, video, audio)
└── scenarios/dataspace/    framing, scripted agents, vendored evaluator, scoring, runners, measurements
tests/                      agent-unreliability, end-to-end and boundary tests
examples/                   orders.csv, demo.py, mcp_config.json
```

`agent_backend` and `agent_harness` are two packages in one repository. The
harness imports from the backend only its public API and
`agent_backend.mcp.server`; the backend imports nothing from the harness and
names no scenario; `tests/test_boundary.py` checks both. Reading a PDF, a
video or an audio track is the harness's job: a reader is a tool beside the
backend's, and what it extracts enters the backend through `import_dataset`
or `attach_metadata`, as a fresh agent output with provenance (MADR 0009).

## 3. Running the prototype

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                        # install duckdb, pydantic, mcp, pytest
uv run pytest -q               # test suite
uv run python examples/demo.py # end-to-end demonstration (throwaway workspace)
```

The demo imports `orders.csv`, resolves "the orders I imported today", groups
revenue by region, materializes `regional_sales`, describes it, prints its
provenance via "the result I created earlier", retries the materialization
(idempotent replay, still two datasets), and finally sends an ambiguous
reference to show the `needs_resolution` response and the repaired retry.

Use the backend directly from Python:

```python
from agent_backend import Backend

backend = Backend("./workspace")
backend.import_dataset("examples/orders.csv", description="Shop orders")
backend.materialize_result("orders", {"group_by": ["region"], "metric": "revenue"}, "regional_sales")
backend.get_provenance("regional_sales")
```

## 4. MCP configuration

Start the server over stdio:

```sh
uv run agent-backend-mcp --workspace ./workspace
```

Client configuration (Claude Desktop / Claude Code / Codex style; see
`examples/mcp_config.json`):

```json
{
  "mcpServers": {
    "agent-backend": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Intentum",
               "agent-backend-mcp", "--workspace", "/absolute/path/to/workspace"]
    }
  }
}
```

Claude Code: `claude mcp add agent-backend -- uv run --directory /abs/path/to/Intentum agent-backend-mcp --workspace /abs/path/to/workspace`.

Tools exposed (all semantic; no SQL, no file or table primitives):
`list_datasets`, `list_artifacts`, `describe_dataset`, `search_datasets`,
`import_dataset`, `import_workspace`, `attach_metadata`, `transform_dataset`,
`declare_output`, `materialize_result`, `export_result`, `publish_dataset`,
`update_metadata`, `delete_dataset`, `restore_dataset`, `get_provenance`,
`get_operation`, `get_output_contract`.

## 5. Example MCP calls

```jsonc
// import one file
{"name": "import_dataset", "arguments": {"path": "/data/orders.csv", "description": "Shop orders"}}
// → {"status": "success", "operation_id": "op_1", "dataset": {"id": "ds_1", "name": "orders", "rows": 12, ...},
//    "source": {"artifact_id": "art_1", "name": "orders.csv", "kind": "csv", "locator": null}}

// import a whole task workspace (csv + json + every table of every sqlite; docs/media become artifacts)
{"name": "import_workspace", "arguments": {"path": "/data/task_10/context"}}
// → {"status": "success", "datasets": [{"id": "ds_9", "name": "ed_moneyauthoritybs", "rows": 294,
//    "source": "db/sub_db.sqlite::ed_moneyauthoritybs", ...}, ...], "artifacts": [...], "failed": [], "replayed": [],
//    "summary": "Imported 16 dataset(s) from 14 artifact(s) in context."}

// attach the workspace's semantic layer
{"name": "attach_metadata", "arguments": {"source": "knowledge.md"}}
// → {"status": "success", "applied": [{"name": "ed_moneyauthoritybs", "description_set": true,
//    "columns": ["TotalAssets", "Forex", ...]}, ...],
//    "unmatched": {"tables": [{"name": "ed_grossdomesticproduct", "line": 88}], "columns": [...]}}

// write the shape of the answer down while the question is in front of you
{"name": "declare_output", "arguments": {"columns": ["region", "revenue"], "rows": {"one_per": ["region"]}}}
// → {"status": "success", "operation_id": "op_2", "contract": {"id": "oc_1", "status": "open", "revision": 1, ...},
//    "summary": "Output contract oc_1 declared: the exported file must carry exactly the columns [region, revenue] ..."}

// preview a transform with fuzzy references
{"name": "transform_dataset", "arguments": {
  "source": "the orders I imported today",
  "transform": {"filter": "amount > 100", "group_by": ["Region"], "metric": "Revenue", "sort": "-revenue"}}}
// → {"status": "success", "result": {"columns": [...], "rows": [...], "row_count": 4},
//    "plan": "Scan(orders v1) → Filter((amount > 100)) → HashAggregate(region, SUM(amount) AS revenue) → Sort(revenue desc) → Preview(limit=20)",
//    "resolution": [...]}

// persist it
{"name": "materialize_result", "arguments": {
  "source": "orders", "transform": {"group_by": ["region"], "metric": "revenue"},
  "name": "regional_sales", "description": "Revenue by region"}}
// → {"status": "success", "operation_id": "op_3", "dataset": {"id": "ds_2", "name": "regional_sales", "rows": 4,
//    "columns": ["region", "revenue"]}, "summary": "Created regional_sales from orders by summing amount as revenue grouped by region.",
//    "lineage": ["ds_1"]}

// retry → idempotent replay, no new dataset
// → {"status": "success", "idempotent_replay": true, "operation_id": "op_3", "dataset": {"id": "ds_2", ...}}

// export a dataset that carries one column too many → held to the contract, nothing written
{"name": "export_result", "arguments": {"dataset": "regional_sales_with_counts", "path": "prediction.csv"}}
// → {"status": "error", "code": "CONTRACT_MISMATCH", "recoverable": true,
//    "message": "regional_sales_with_counts does not have the shape declared in output contract oc_1: column 'orders' is not in the contract.",
//    "details": {"contract": {...}, "actual": {...}, "problems": [{"kind": "extra", "column": "orders", ...}],
//                "repair": {"select": ["region", "revenue"]}},
//    "hint": "Reshape it with materialize_result(source='regional_sales_with_counts', transform={\"select\": [\"region\", \"revenue\"]}) and export that dataset, or call declare_output again with a reason if the requirement itself changed."}

// the matching export satisfies the contract and records the evidence
{"name": "export_result", "arguments": {"dataset": "regional_sales", "path": "prediction.csv"}}
// → {"status": "success", "rows": 4, "columns": ["region", "revenue"], "content_hash": "...",
//    "contract": {"id": "oc_1", "status": "satisfied", "verified": {"columns": ["region", "revenue"], "rows": 4, "cardinality": "one_per", "distinct_keys": 4}}}

// ambiguity
{"name": "transform_dataset", "arguments": {"source": "sales", "transform": {"select": ["region"]}}}
// → {"status": "needs_resolution", "code": "AMBIGUOUS_REFERENCE", "field": "source",
//    "candidates": [{"id": "ds_1", "name": "sales_2026_08_26", ...}, {"id": "ds_2", "name": "sales_daily", ...}],
//    "recoverable": true, "hint": "Repeat the request with the dataset id or exact name."}

// provenance
{"name": "get_provenance", "arguments": {"dataset": "regional_sales"}}
// → {"produced_by": {"id": "op_3", "kind": "materialize_result", "original_intent": {...}, "plan": "..."},
//    "inputs": [{"id": "ds_1", "name": "orders", "version": 1, "relationship": "derived_from"}],
//    "canonical_intent": {...}, "audit": [...]}

// safe delete with restore information
{"name": "delete_dataset", "arguments": {"dataset": "orders_archive", "reason": "duplicate"}}
// → {"status": "success", "restore": {"operation": "restore_dataset", "dataset": "ds_3"}, "affected_downstream": []}
```

Set `"explain": true` on `transform_dataset` / `materialize_result` to receive
the canonical IR, the full execution plan and the generated SQL.

## 6. Tests

`tests/` targets agent unreliability explicitly:

- `test_resolution.py`: ids vs names, wrong capitalization, approximate names,
  aliases, temporal references, "the result I created earlier", ambiguity with
  candidates and repair, deleted-dataset references, fuzzy/ambiguous/invalid
  column names, search.
- `test_transforms.py`: every step type, compact and list forms, string and
  object expressions, joins, missing measures, unknown keys/step types/functions,
  type mismatches, partial transforms, identity transform, strictness of the
  canonical IR against tampering.
- `test_idempotency.py`: repeated materialization, same name/different request,
  explicit keys, key reuse, idempotent publish/delete.
- `test_failures.py`: execution failure halfway through a materialization,
  metadata commit failure (physical table dropped, later retry succeeds),
  audit and structured logs for failed operations, stale-version detection.
- `test_lifecycle.py`: publish validation, metadata updates, delete/restore,
  provenance, access control hook, concise listings.
- `test_mcp.py`: semantic-only tool surface, required-fields-only schemas,
  end-to-end through `call_tool`, errors as structured content.
- `test_e2e.py`: the demo scenario and intent-repair flows.
- `test_naming.py`: Chinese dataset/column names through import, resolution
  (exact, alias, fuzzy, ambiguity, Chinese hint words), expressions with
  punctuated names, derived/materialized names.
- `test_workspace.py`: `import_workspace` over csv + wrapper json + multi-table
  sqlite + documents + media: deterministic collision-free names, aliases,
  artifacts, per-source failure reporting, idempotent re-run, single-table
  import with locators.
- `test_knowledge.py`: knowledge.md parsing (table and bullet styles, scoped
  and global facts, units), `attach_metadata` application, overwrite
  semantics, unmatched reporting, source forms.
- `test_loose_shapes.py`: the intent shapes a model actually produced in the
  DataSpace runs — steps named by their key instead of `type`, `sort` with
  `order` as the key list, `"field as alias"` in `select` and `derive`,
  `derive` as a list, `||`, string slicing and `strftime`; from the second
  measurement, `strftime` with the pattern first, type errors naming the
  signature, `date`/`date_trunc`, `group_by` over a named expression, `in
  [a, b]`, and a step body nested under its own key.
- `test_contracts.py`: output contracts — declaration, identical
  re-declaration, amendment with a reason and its audit trail, validation of
  the declared shape; export held to the contract for extra, missing,
  renamed, misordered and mistyped columns and for row cardinality, with the
  repair transform carried in the error; evidence recorded on satisfaction;
  exports without a contract unchanged.
- `test_export.py`: export to csv/parquet, overwrite and export-root refusals,
  and the format specification: half-up rounding on the shortest decimal form,
  trailing zeros, whole numbers keeping a decimal, date patterns, null text,
  validation of unknown keys/columns/directives, and byte-identical re-export.
- `test_import.py` also covers temporal refinement: text dates promoted to
  `date`/`timestamp`, `type` hints winning over it, and values that only look
  like dates (`2002-02-31`) staying text.

Each state-changing test finishes by checking `Backend.integrity_report()`,
which cross-checks metadata versions, DuckDB tables, row counts, orphan tables
and pending operations.

## 7. What to implement next

The backend is being validated on the [DataSpace](https://github.com/BugMaker-Boyan/DataSpace)
benchmark (410 heterogeneous task workspaces) against the KDD Cup 2026 champion
pipeline as baseline; see `.seal/madr/0003-*` for the sequence. Unicode
identifiers, `import_workspace` and `attach_metadata` are done; a real
`task_10` workspace (8 csv, 8 sqlite tables, 1 wrapper json, knowledge.md)
imports in ~2 s and the task's query runs through the semantic steps.

0. **DataSpace smoke tests** — four public-reference tasks (`task_10`,
   `task_44`, `task_127`, `task_329`) run in both layers.
   `python -m agent_harness.scenarios.dataspace.smoke --task all --check` (scripted agent, six or
   seven MCP tool calls per task, each declaring its output contract first)
   passes the official evaluator on all four; `python -m agent_harness.scenarios.dataspace.agent`
   (qwen3.5-35b-a3b through the MCP tools, no SQL or dialect rules in the
   prompt) passes 3/3 on `task_10` and `task_127`. The third measurement
   (2026-08-30, `agent_harness/scenarios/dataspace/README.md`) re-ran `task_44` and
   `task_329` after the output contract and the vocabulary the second
   measurement asked for: the `strftime` refusals are gone, turns and tokens
   fell on both tasks, and five of six runs again exported the correct values
   with extra columns — this time columns the agent had *declared*, so the
   contract check passed on a misread question, and the declarations came
   late (turns 7–28) rather than fresh. The declaration-first experiment on
   2026-08-31 moved declarations to turns 1–3 without changing the model's
   mistaken answer shapes; the result confirms the trust boundary rather than
   fixing the reading. Compound post-aggregate projection, measureless
   grouping as distinct, and a lineage-preserving `semi_join` now cover the
   three general vocabulary findings; the post-semi-join run on `task_44`
   (2026-08-31) stayed 0/3: one run reached for `semi_join` on the wrong stay
   identifier, one exported the right values with a declared helper column,
   so the remaining gap is the model's reading and relationship choice, not
   backend expressiveness. Most DataSpace workspaces also carry PDFs (384 of
   410) or video (189); reading them is the harness's job
   (`agent_harness/perception/`, MADR 0009), and the first reader, chosen by
   the first measured task that needs it, is the next step there. The
   2026-09-02 runs showed that refusals never named the accepted shape and that
   silent failures (empty previews, an unexported correct preview) went
   unremarked; `core/recovery` now answers both with structured advice and
   rewrites (MADR 0010), and the harness reports refusals without advice and
   the repair rate beside the pass rate, so convergence is measured rather than
   read off task by task. With advice in place the model-driven layer
   exported the correct `task_44` values in five of six runs and scored its
   first official pass on that task (1/3 on 2026-09-02), the passing run
   having declared its output after seeing the data rather than first. The
   runner therefore has two framings, `--declaration fresh` (declare before
   exploring) and `--declaration informed` (declare once a preview shows the
   answer rows), measured against each other in the DataSpace README: on
   twelve runs the informed declarations named the gold shape three times out
   of six against one for fresh, and passes went from 1/6 to 3/6, so
   `informed` is the default. A validated read-only `raw_query` fallback step is
   recorded as MADR 0002 for the long tail; nothing measured so far has
   needed it. DataSpace is a validation scenario, not the goal (MADR 0004).
1. **Dataset versioning on write**: `replace_dataset` / re-import creating
   version N+1 with the previous table retained; the schema for versions is in
   place, only the operation is missing.
2. **Time-travel and lineage-aware reads**: `describe_dataset(version=…)`,
   downstream impact reports before delete/replace.
3. **Restricted read-only query capability** built on the same expression
   language, with the same validator, for ad-hoc analysis that does not fit
   the step vocabulary.
4. **Storage backends**: PostgreSQL `MetadataStore`, Parquet-on-object-storage
   for versions; the protocols are in place, the SQLite/DuckDB code is the only
   implementation.
5. **Resolver learning from feedback**: persist chosen candidates as aliases
   after a `needs_resolution` round-trip so the same reference resolves next time.
6. **Richer semantic layer**: user-defined metrics (`revenue := sum(amount)`),
   column-level lineage, unit/currency metadata used by the type rules.
7. **Concurrency**: the SQLite store uses `BEGIN IMMEDIATE` and DuckDB runs
   single-process; multi-writer deployments need a server-side queue or
   PostgreSQL advisory locks around materialization.
8. **Protocol-level input errors**: the MCP SDK currently reports wrong-typed
   arguments (e.g. a string `transform`) as tool errors with pydantic text;
   mapping those to the same structured error shape would close the last gap.
