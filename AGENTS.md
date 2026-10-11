# Agent instructions

pstr holds the plan. Call `context` when starting or resuming, passing the node you pick up as `node`; put your plan in `plan` before durable changes and `report` each step as you finish it.

## Project structure

Intentum is a Python 3.11+ data backend that turns an agent's semantic intent
into validated, deterministic data operations. The main transform flow is
resolve -> canonical IR -> validate -> plan -> execute -> commit, coordinated
by `Backend`. Business logic lives in `core/`, persistence in `storage/`, and
the MCP interface in `mcp/`, all under `agent_backend/`.

The agent side lives in `agent_harness/`, a sibling package that drives the
backend only through its MCP tool surface and is not shipped in the wheel. It
owns the task framing, the measurement, the host adapters (OpenCode runs the
model loop, MADR 0011; an in-process loop remains as the control arm) and the
validation scenarios. The boundary is
enforced by `tests/test_boundary.py` and recorded in MADR 0009: the harness
imports from `agent_backend` only its public API and `agent_backend.mcp.server`;
the backend imports nothing from the harness and names no scenario; what
perception extracts enters the backend only through `import_dataset` or
`attach_metadata`.

| Path | Responsibility |
| --- | --- |
| `agent_backend/__init__.py` | Public Python API: `Backend`, `BackendError`, and `ErrorCode`. |
| `agent_backend/core/backend.py` | Semantic operations, operation lifecycle, idempotency, metadata commits, and failure compensation. |
| `agent_backend/core/access.py` | Access policy protocol and built-in policies. |
| `agent_backend/core/errors.py` | Error codes and structured responses, including ambiguity and recovery hints. |
| `agent_backend/core/logging.py` | Structured events for the execution stages. |
| `agent_backend/core/naming.py` | Unicode-aware identifier rules shared by the IR, validator and resolvers. |
| `agent_backend/core/models/` | Dataset, column, version, operation, lineage, and audit entities. |
| `agent_backend/core/ir/` | Canonical Pydantic models, expression parser, shared type rules, the raw_query step's rules (`raw_query.py`, MADR 0002), and the unit rules that follow declared units through a transform (`units.py`, MADR 0019). |
| `agent_backend/core/resolver/` | Resolve loose dataset, field, expression, and transform references into canonical IR. |
| `agent_backend/core/validation/` | Independently validate IR types, schemas, input versions, and dataset state. |
| `agent_backend/core/planner/` | Build explicit, inspectable execution plans. |
| `agent_backend/core/execution/` | Compile IR to internal SQL, execute plans, and roll back physical tables on failure. |
| `agent_backend/core/export/` | Export format specification: how typed values are rendered as text at the file boundary. |
| `agent_backend/core/contracts/` | Output contracts: read a declared deliverable shape and its value checks into canonical form and check a dataset against them (MADR 0020). |
| `agent_backend/core/evidence/` | Evidence references from imported rows and cells to registered artifacts: read them into canonical form, and check a span or quote against a text document (MADR 0021). |
| `agent_backend/core/recovery/` | Teach on refusal (MADR 0010): turn a refusal or a silent-failure signal into structured advice naming what the backend accepts, with the request rewritten as tool calls when mechanical. |
| `agent_backend/core/knowledge/` | Parse knowledge.md-style semantic-layer documents into table and column facts. |
| `agent_backend/core/lineage/` | Record lineage edges and traverse upstream and downstream dependencies. |
| `agent_backend/core/audit/` | Record and retrieve audit events for entities and operations. |
| `agent_backend/storage/metadata/` | `MetadataStore` protocol and SQLite implementation. |
| `agent_backend/storage/duckdb/` | `AnalyticsEngine` protocol and DuckDB implementation, plus the sandbox raw_query statements are parsed, described and run in (`sandbox.py`). |
| `agent_backend/storage/files/` | Managed workspace paths, imported source-file copies, and rows written inline kept as JSON sources. |
| `agent_backend/mcp/server/main.py` | MCP server creation and the `agent-backend-mcp` CLI entry point over stdio. |
| `agent_backend/mcp/tools/registry.py` | Semantic MCP tool definitions that delegate to `Backend`. |
| `tests/` | pytest coverage for imports, resolution, transforms, lifecycle, idempotency, failures, MCP, end-to-end flows, and the harness/backend boundary. |
| `tests/conftest.py` | Shared temporary workspace, backend, sample orders, and deterministic clock fixtures. |
| `examples/` | Sample `orders.csv`, runnable `demo.py`, and MCP client configuration in `mcp_config.json`. |
| `agent_harness/` | The agent side: `config.py` (`.env` and model gateway settings), `model.py` (OpenAI-compatible chat client), `loop.py` (tool-calling loop over an MCP server). |
| `agent_harness/hosts/` | External agent hosts (MADR 0011): `opencode.py` runs pinned OpenCode in a container with backend MCP tools and optional video perception tools, an empty HOME, and per-run config. It normalizes events; `vision_probe.py` checks actual image delivery using a synthetic video. |
| `agent_harness/perception/` | Opt-in MCP perception: FFmpeg supplies timestamped frames and audio clips inside the supplied context; the host model reads images, and optional offline Whisper transcribes speech. Source/model hashes, segments and revisable observations are retained as importable JSON. ASR and its dependencies stay outside the backend and wheel; see this package's README. |
| `agent_harness/scenarios/dataspace/` | DataSpace validation: task framing, scripted agents, the vendored official evaluator, scoring, the runners `smoke.py` (scripted) and `agent.py` (model-driven), run output under `runs/`, and the measurement README. |
| `pyproject.toml` | Package metadata, dependencies, CLI entry point, build configuration, and test settings. |
| `uv.lock` | Locked dependency resolution for uv. |
| `README.md` | Detailed architecture, transform language, setup, MCP usage, examples, and next steps. |
| `HANDOFF.md` | What a new agent reads, and in what order: documents, the last outcomes, the MADRs. |

Runtime data belongs to the configured backend workspace: `metadata.sqlite`
stores metadata, `analytics.duckdb` stores analytical tables, and `files/`
holds imported sources. These are runtime artifacts, not source directories.