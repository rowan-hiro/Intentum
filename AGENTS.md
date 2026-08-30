# Agent instructions

## Project structure

Intentum is a Python 3.11+ data backend that turns an agent's semantic intent
into validated, deterministic data operations. The main transform flow is
resolve -> canonical IR -> validate -> plan -> execute -> commit, coordinated
by `Backend`. Business logic lives in `core/`, persistence in `storage/`, and
the MCP interface in `mcp/`, all under `agent_backend/`.

| Path | Responsibility |
| --- | --- |
| `agent_backend/__init__.py` | Public Python API: `Backend`, `BackendError`, and `ErrorCode`. |
| `agent_backend/core/backend.py` | Semantic operations, operation lifecycle, idempotency, metadata commits, and failure compensation. |
| `agent_backend/core/access.py` | Access policy protocol and built-in policies. |
| `agent_backend/core/errors.py` | Error codes and structured responses, including ambiguity and recovery hints. |
| `agent_backend/core/logging.py` | Structured events for the execution stages. |
| `agent_backend/core/naming.py` | Unicode-aware identifier rules shared by the IR, validator and resolvers. |
| `agent_backend/core/models/` | Dataset, column, version, operation, lineage, and audit entities. |
| `agent_backend/core/ir/` | Canonical Pydantic models, expression parser, and shared type rules. |
| `agent_backend/core/resolver/` | Resolve loose dataset, field, expression, and transform references into canonical IR. |
| `agent_backend/core/validation/` | Independently validate IR types, schemas, input versions, and dataset state. |
| `agent_backend/core/planner/` | Build explicit, inspectable execution plans. |
| `agent_backend/core/execution/` | Compile IR to internal SQL, execute plans, and roll back physical tables on failure. |
| `agent_backend/core/export/` | Export format specification: how typed values are rendered as text at the file boundary. |
| `agent_backend/core/contracts/` | Output contracts: read a declared deliverable shape into canonical form and check a dataset against it. |
| `agent_backend/core/knowledge/` | Parse knowledge.md-style semantic-layer documents into table and column facts. |
| `agent_backend/core/lineage/` | Record lineage edges and traverse upstream and downstream dependencies. |
| `agent_backend/core/audit/` | Record and retrieve audit events for entities and operations. |
| `agent_backend/storage/metadata/` | `MetadataStore` protocol and SQLite implementation. |
| `agent_backend/storage/duckdb/` | `AnalyticsEngine` protocol and DuckDB implementation. |
| `agent_backend/storage/files/` | Managed workspace paths and imported source-file copies. |
| `agent_backend/mcp/server/main.py` | MCP server creation and the `agent-backend-mcp` CLI entry point over stdio. |
| `agent_backend/mcp/tools/registry.py` | Semantic MCP tool definitions that delegate to `Backend`. |
| `tests/` | pytest coverage for imports, resolution, transforms, lifecycle, idempotency, failures, MCP, and end-to-end flows. |
| `tests/conftest.py` | Shared temporary workspace, backend, sample orders, and deterministic clock fixtures. |
| `examples/` | Sample `orders.csv`, runnable `demo.py`, and MCP client configuration in `mcp_config.json`. |
| `examples/dataspace_smoke.py`, `examples/dataspace_agent.py` | DataSpace validation runs through the MCP tools: a scripted agent and a model-driven one, both scored by the official evaluator vendored in `examples/dataspace/`. |
| `pyproject.toml` | Package metadata, dependencies, CLI entry point, build configuration, and test settings. |
| `uv.lock` | Locked dependency resolution for uv. |
| `README.md` | Detailed architecture, transform language, setup, MCP usage, examples, and next steps. |
| `HANDOFF.md` | What a new agent reads, and in what order: documents, the last outcomes, the MADRs. |
| `.seal/` | DriftSeal outcome history and MADR records; follow the protocols below. |

Runtime data belongs to the configured backend workspace: `metadata.sqlite`
stores metadata, `analytics.duckdb` stores analytical tables, and `files/`
holds imported sources. These are runtime artifacts, not source directories.

<!-- driftseal -->
<!-- driftseal-version: 2.1 -->
<!-- driftseal-log-language: en -->

## Agent protocol: outcome write-ahead log

This repository uses DriftSeal (`driftseal`) to prevent agent drift. This
`AGENTS.md` protocol is the source of truth; use the CLI by default, with MCP
and lifecycle hooks as optional adapters.

**Log language:** `en`. Write outcome-log prose (outcome, extension, note,
verify-result, and reclaim/unreclaim reason) in that language. Keep command
names, flags, status tokens, ids, and lane names in English.

1. **Write the outcome first**, before changing durable project content:
   `driftseal begin "<coherent delivery outcome>" --accept "<observable result>" --verify "<exact command that proves the cumulative contract>"`.
   Repeat `--accept` for independently observable criteria and add one
   `--decision <id>` for each existing MADR this outcome may change.
   Record outcomes for changes intended to persist in the project: code,
   configuration, documentation, dependencies, and equivalent files, inside or
   outside Git. Git operations, checks, temporary auxiliary work, and external
   state changes are exempt when they do not write durable project content here.
2. **Extend only the same outcome.** For another step toward the same coherent
   delivery goal, append `driftseal extend "<addition>"`. It may add
   `--accept`, `--decision`, and a replacement `--verify`; adding acceptance
   requires a replacement verifier that proves the complete accumulated contract.
   Every extension invalidates earlier verification and MADR reconciliation. If
   the delivery goal changes, close the current outcome honestly and begin a new one.
   One open outcome belongs to one worktree, or one configured non-Git project
   root. Every agent changing durable content in the same root re-anchors and
   continues it; separate worktrees hold separate outcomes.
   Outcomes belong to one named lane (`driftseal lane`). The default lane is
   `main`; untagged history lives there. Re-anchoring and `driftseal log`
   follow the current lane. Close the open outcome before switching lanes.
   Create a lane only for a long-lived capability you expect to leave and resume.
3. **Reconcile, verify, then close.** After the final extension, reconcile every
   linked MADR with `driftseal decision update`. Inspect `driftseal status`,
   then run `driftseal verify` for an acceptance-bound outcome. A verifier
   without matching local provenance is untrusted and requires
   `--allow-tracked-command` after inspection. Finish with
   `driftseal end -s completed|partial|failed|abandoned -n "<what happened>"`.
   Completed outcomes require fresh successful verification bound to both the
   current contract hash and Git-visible workspace. Never report success without
   closing the outcome.
4. **Re-anchor after context loss or handoff:** run `driftseal status` and
   `driftseal log --last 3` before changing durable content. Both follow the
   current lane. Resume the open outcome when it still matches; otherwise close
   it and begin a new one. If the requested work belongs to a different existing
   lane, switch first.

**Log access goes only through DriftSeal.** Never read, edit, move, or delete
`.seal/outcomes/events.jsonl` (or its configured equivalent) directly. Use
`reclaim`/`unreclaim` for visibility markers and `absorb` after merge
collisions or when Decision History outcome references are stale. These operations preserve append-only single-lineage history.

Seal root: `.seal/` (override with `$DRIFTSEAL_HOME`); outcome log:
`.seal/outcomes/events.jsonl`; commit `.seal/` with the code.
<!-- /driftseal -->

<!-- driftseal-decisions -->
<!-- driftseal-decisions-version: 2.1 -->
<!-- driftseal-log-language: en -->

## Agent protocol: decision log

Record a MADR only when it preserves context that the outcome log and Git cannot
recover: rejected or deferred paths worth revisiting, non-obvious rationale for
long-lived or costly-to-reverse choices, and deprecated or superseded decisions.
Do not record routine, local, readily reversible choices.

**Log language:** `en`. Write decision-log prose (title, context,
outcome, drivers, options, consequences, and update notes) in that language.
Keep MADR section headings, status tokens, and ids in English.

`driftseal decision add "<title>" --context "<problem and constraints>" --outcome "<decision and rationale>" --driver "<decision driver>" --option "<considered option>" --consequence "<result>"`

Use `proposed|accepted|rejected|deferred|deprecated|superseded` statuses. Link
existing MADRs from `begin` or `extend`, then reconcile each linked record
with `driftseal decision update` before successful or partial closure. After a
merge, `driftseal absorb` remaps colliding ids and repairs managed Decision History
outcome references; it never auto-merges concurrent edits of a shared MADR.
Commit `.seal/madr/` with the code.
<!-- /driftseal-decisions -->
