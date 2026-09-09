# DriftSeal outcome history (archive)

Frozen on 2026-09-09, when the repository moved from DriftSeal to Inkan.
This file is the readable rendering of `.seal/outcomes/events.jsonl`, produced
by `driftseal log --all --all-lanes` at driftseal 3.3.5. It is kept so the 21
outcomes below stay readable without the driftseal binary installed.

It is an archive: nothing appends to it, and the outcomes it records are
closed. Read it as history. Current work is sealed with Inkan in `.inkan/`.

The decision records these outcomes reference live in `.inkan/decisions/`,
where they kept their numbers 0001-0012.

---

```
[2026-08-27-001] completed
  outcome: Deliver a vertical-slice prototype of an agent-ready data backend: loose intent -> resolver -> canonical IR -> validation -> planner -> DuckDB execution -> SQLite metadata/lineage/audit, exposed through a thin MCP server, with tests and a runnable demo
  extend: Record and link the two-store commit/compensation decision (MADR 0001) that the backend implements
  accept: pytest suite passes, covering agent-unreliability cases (fuzzy names, aliases, ambiguity, invalid columns, type mismatch, retries/idempotency, deleted datasets, mid-operation failures) and end-to-end intent flows
  accept: examples/demo.py runs end to end: import orders.csv, resolve a loose reference, aggregate revenue by region, materialize regional_sales, describe it, show lineage, replay idempotently, and return a structured needs_resolution response for an ambiguous reference
  accept: MCP server exposes only semantic tools (no SQL/CRUD primitives) and README documents architecture, layout, run instructions, MCP config, example calls, and next steps
  decisions: 0001
  verify: uv run pytest -q && uv run python examples/demo.py
  machine-verification: passed (exit 0, 10573 ms, workspace 88c1f8112a94)
  note: Vertical slice delivered: agent_backend package (core resolver/IR/validation/planner/execution/lineage/audit, SQLite+DuckDB storage, thin MCP server with 12 semantic tools), 60 passing tests targeting agent unreliability, runnable examples/demo.py, README with architecture/run/MCP/next steps. Verified via uv run pytest -q && uv run python examples/demo.py.
  began: 2026-08-27T15:32:18.650Z  ended: 2026-08-27T15:55:29.413Z

[2026-08-28-001] completed
  outcome: Document the current project structure and key entry points in AGENTS.md
  accept: AGENTS.md describes the backend layers, tests, examples, project configuration, and key entry points in English using paths that exist in the repository
  accept: Existing DriftSeal outcome and decision protocols remain unchanged
  verify: git diff --check -- AGENTS.md && python3 -c 'import hashlib, re
from pathlib import Path
text = Path("AGENTS.md").read_text()
section = text.split("## Project structure\n", 1)[1].split("<!-- driftseal -->", 1)[0]
paths = re.findall(r"^\| `([^`]+)` \|", section, re.M)
required = {"agent_backend/__init__.py", "agent_backend/core/backend.py", "agent_backend/core/ir/", "agent_backend/core/resolver/", "agent_backend/core/validation/", "agent_backend/core/planner/", "agent_backend/core/execution/", "agent_backend/storage/metadata/", "agent_backend/storage/duckdb/", "agent_backend/storage/files/", "agent_backend/mcp/server/main.py", "agent_backend/mcp/tools/registry.py", "tests/", "examples/", "pyproject.toml", "uv.lock", "README.md", ".seal/"}
assert required <= set(paths), required - set(paths)
assert all(Path(p).exists() for p in paths), [p for p in paths if not Path(p).exists()]
assert all(re.fullmatch(r"\| `[^`]+` \| .+ \|", line) for line in section.splitlines() if line.startswith("| `"))
assert not re.search(r"[\u4e00-\u9fff]", section)
assert hashlib.sha256(text[text.index("<!-- driftseal -->"):].encode()).hexdigest() == "de74b8e95670fa4ebc3019ade823931bbc8cc95a41597344b9f8545125e851c2"
print(f"Verified {len(paths)} documented paths, English structure description, and unchanged DriftSeal protocols.")'
  machine-verification: passed (exit 0, 67 ms, workspace 28f2bb52b353)
  note: Added an English project structure section to AGENTS.md covering the backend layers, public and MCP entry points, tests, examples, configuration, and runtime workspace. Verified all 25 documented paths, Markdown whitespace, and unchanged DriftSeal protocols; no application code changed.
  head: d7208817ceb67ce447691f48ec95298baced89de..d7208817ceb67ce447691f48ec95298baced89de
  began: 2026-08-28T07:34:00.258Z  ended: 2026-08-28T07:34:36.325Z

[2026-08-28-002] completed
  outcome: Adapt the backend to DataSpace workspaces: Unicode-safe identifiers, import_workspace with Artifact entities and source provenance, and attach_metadata that ingests a knowledge.md semantic layer
  accept: Datasets, columns, aliases and derived names with Chinese or other non-ASCII identifiers import, resolve (exact, alias, normalized, fuzzy) and execute correctly
  accept: import_workspace loads a directory of csv, wrapper-style json and multi-table sqlite files into datasets in one operation, registers documents and media as Artifact records, records source artifact and locator on each dataset, handles name collisions deterministically, reports per-source failures, and is idempotent on re-run
  accept: attach_metadata parses a knowledge.md semantic layer into dataset and column descriptions, units and aliases, reports unmatched tables and columns, and describe_dataset exposes the result
  accept: Existing test suite still passes and new tests cover the three capabilities
  decisions: 0003
  verify: uv run pytest -q
  machine-verification: passed (exit 0, 11715 ms, workspace 216ee3dc0534)
  note: Delivered Unicode-safe identifiers (core/naming.py, parser/resolver/validator updated), Artifact entity with source provenance, import_workspace (csv/json-wrapper/multi-table sqlite, deterministic collision-free names, per-source failure reporting, idempotent re-run), attach_metadata with a heuristic knowledge.md parser (table and bullet styles, scoped/global facts, units), list_artifacts, MCP tools for all three, README updates. 83 tests pass; real DataSpace task_10 workspace imports 16 datasets in ~2s and the task query runs through semantic steps.
  head: d7208817ceb67ce447691f48ec95298baced89de..d7208817ceb67ce447691f48ec95298baced89de
  began: 2026-08-28T03:42:34.503Z  ended: 2026-08-28T04:05:42.856Z

[2026-08-28-003] completed
  outcome: Run the DataSpace task_10 smoke test: vendor the official evaluator, add a minimal export_result operation, and drive task_10 end to end through the MCP tool surface with a scripted agent, scored by the official evaluator and the champion repo's local scorer
  accept: examples/dataspace/evaluate.py is the official HKUSTDial/DataSpace evaluator with source commit and license recorded
  accept: export_result writes a managed dataset to a CSV file through the backend and is covered by tests
  accept: examples/dataspace_smoke.py produces predictions/task_10/prediction.csv purely through MCP tool calls and reports official Task Accuracy for task_10 plus the champion scorer's score, writing smoke_result.json with the tool-call trace
  decisions: 0003
  verify: uv run pytest -q && uv run python examples/dataspace_smoke.py --task task_10 --check
  machine-verification: passed (exit 0, 19235 ms, workspace cbb0f541bfa3)
  note: Vendored the official DataSpace evaluator with provenance header (examples/dataspace/evaluate.py), added export_result (engine COPY, audit with content hash, overwrite guard, MCP tool, tests; 86 tests pass), and examples/dataspace_smoke.py which drives task_10 through six MCP tool calls: official evaluator passed (242/242, order-sensitive); champion scorer 0.45 due to float text formatting (31783696.814999998 vs 31783696.815), recorded as the first requirement for export formatting rules. Findings in examples/dataspace/README.md.
  head: 9976c71dbc17bd3cce93166c6350f42b43fd3eac..9976c71dbc17bd3cce93166c6350f42b43fd3eac
  began: 2026-08-28T05:22:42.618Z  ended: 2026-08-28T05:29:08.045Z

[2026-08-28-004] completed
  outcome: Fold the two task_10 smoke findings (export needs a numeric format specification; SQLite date columns import as VARCHAR) into MADR 0003 as evidence shaping steps 4-5
  accept: MADR 0003 body carries a 'Evidence from the task_10 smoke test' subsection with both findings and their implication for steps 4 and 5
  decisions: 0003
  verify: grep -q '31783696.814999998' .seal/madr/0003-*.md && grep -q 'VARCHAR' .seal/madr/0003-*.md
  machine-verification: passed (exit 0, 13 ms, workspace d0151fbc30aa)
  note: MADR 0003 now carries both task_10 smoke findings as evidence for steps 4-5: export numeric format specification, and SQLite date-column type refinement on import.
  head: b8b72a3a6d93315d5792ae56e5353aecfc7a03f4..b8b72a3a6d93315d5792ae56e5353aecfc7a03f4
  began: 2026-08-28T05:46:37.531Z  ended: 2026-08-28T05:47:06.633Z

[2026-08-28-005] completed
  outcome: Run the LLM-driven layer of the DataSpace task_10 smoke: a thin OpenAI-compatible agent loop that exposes only the MCP tools to qwen3.5-35b-a3b, runs the task several times, scores each run with the official evaluator, and records turns, tool calls, tokens, cost and error distribution
  accept: examples/dataspace_agent.py runs a task with a real model through the MCP tool surface (no SQL, no dialect rules in the prompt), writes per-run traces and an aggregated summary, and scores with the official evaluator
  accept: task_10 has been run at least three times with qwen3.5-35b-a3b and the results (pass rate, turns, tokens, cost, error codes) are recorded in examples/dataspace/README.md
  decisions: 0003
  verify: uv run pytest -q && uv run python examples/dataspace_agent.py --task task_10 --runs 0 --check-config
  machine-verification: passed (exit 0, 18305 ms, workspace ef5a0425558d)
  note: examples/dataspace_agent.py: thin OpenAI-compatible agent loop over the MCP tools (no SQL, no dialect rules); task_10 run 3x with qwen3.5-35b-a3b: 2/3 passed the official evaluator, mean 27 turns, $0.004/run; per-run traces and aggregated summary written; findings recorded in examples/dataspace/README.md and MADR 0003. Along the way: export_result gained an export-root sandbox (PERMISSION_DENIED outside it, auditable), MCP server got --export-root, filter/sort accept model-style keys, sort 'order' bug fixed, MADR 0004 records that Intentum is a general backend and benchmarks are validation scenarios. 88 tests pass.
  head: 1738e6e34954554ea33a653a2f614bfe9676f193..1738e6e34954554ea33a653a2f614bfe9676f193
  began: 2026-08-28T06:39:26.608Z  ended: 2026-08-28T06:54:25.469Z

[2026-08-28-006] completed
  outcome: Close the gaps the task_10 smoke exposed: an export format specification (MADR 0005), import-time temporal type refinement (MADR 0006), acceptance of the loose step shapes a model actually used, string/date expression vocabulary, then re-run both smoke layers on task_10 and on two or three more structured-only public-reference tasks
  accept: export_result accepts a validated format specification (per-column decimals with half-up rounding, strip_trailing_zeros, integer_min_decimals, date/timestamp render pattern, null text; file-level defaults), records it in the operation and audit event, and the MCP tool description says rendering belongs to export
  accept: import refines string columns whose non-null values all parse as ISO date/timestamp to temporal logical and physical types, with a resolution note, replayable from the canonical IR, overridable by schema hints
  accept: the resolver accepts single-key step lists without type, 'x as y' aliases in select and derive expressions, derive given as a list, and '||' as concat; substr/left/right and a temporal format function are in the expression allowlist; each shape is covered by a test
  accept: examples/dataspace_smoke.py --check still passes on task_10, and examples/dataspace_agent.py re-run on task_10 (3 runs) plus the added tasks is recorded in examples/dataspace/README.md with pass rate, turns, cost and error distribution compared to the 2026-08-28 baseline
  decisions: 0005, 0006, 0003, 0002
  verify: uv run pytest -q && uv run python examples/dataspace_smoke.py --task task_10 --check
  machine-verification: passed (exit 0, 23541 ms, workspace 175976bd6e5b)
  note: Steps 4-5 delivered. export_result takes a validated format specification (MADR 0005, accepted): file-level defaults plus per-column overrides, half-up rounding on the shortest decimal form, recorded in the operation's canonical IR, in the plan as FormatValues and in the dataset.exported audit event; byte-identical on re-export. Import refines text columns whose values are all ISO dates/timestamps to date/timestamp (MADR 0006, accepted) with a resolution note, recorded in the canonical IR and overridable by type hints; measured cost 53 s vs 49 s on the task_127 workspace. The resolver accepts the shapes the model actually used (untyped single-key steps, 'x as y' in select and derive, derive as a list, '||' as concat) and the allowlist gained substr/substring/left/right/strftime; tests/test_loose_shapes.py covers each shape, 112 tests pass. The scripted smoke went from one task to four structured-only public-reference tasks (task_10, task_44, task_127, task_329) and --task all --check passes all four; task_10's champion score moved 0.45 to 1.0 from the format specification alone. LLM layer re-run three times per task: task_10 3/3 (baseline 2/3, mean turns 27 to 17, prompt tokens 489k to 339k), task_127 3/3, task_44 0/3, task_329 0/3. Four of those six failing runs exported the correct values with one column too many and thirteen refusals were strftime written pattern-first; the attribution and the next-step ordering are recorded in examples/dataspace/README.md and in MADR 0003. Work is verified but not committed.
  head: e494004f09b89c74309a887cb67b3c65514387eb..0ffce620226c0a21aef874a326c88d43070c8c43
  began: 2026-08-28T07:02:12.668Z  ended: 2026-08-28T07:58:20.172Z

[2026-08-28-007] completed
  outcome: Fix the four dataspace-foundation review defects without changing accepted architectural decisions
  extend: Address the independent review finding: preserve existing export target permission bits during atomic overwrite
  accept: Special-field quoting preserves string literals and quoted identifiers while still resolving bare punctuated field names
  accept: Knowledge facts with an unresolved explicit table scope never modify unrelated datasets and are reported as unmatched
  accept: Recoverable failures after import table creation, including temporal refinement, compensate the physical table and allow a clean retry
  accept: Formatted exports handle supported large numeric values and publish complete files without leaving partial output or destroying an existing target on write failure
  accept: Overwriting an existing export preserves its permission bits, including private 0600 targets
  decisions: 0001, 0003, 0005, 0006
  verify: PYTHONPATH=. /home/ruanboyu/dev/Intentum/.venv/bin/python -m pytest -o addopts="" -q && PYTHONPATH=. /home/ruanboyu/dev/Intentum/.venv/bin/python examples/demo.py && git diff --check -- agent_backend tests AGENTS.md README.md .seal/madr
  machine-verification: passed (exit 0, 18133 ms, workspace ba8e9146e804)
  note: Fixed all four review defects with regression coverage: token-safe field quoting, explicit knowledge scope isolation, import compensation across temporal refinement, and precision-safe atomic exports. Independent review caught an overwrite permission regression; fixed it and passed a second independent review including failure injection. Cumulative verification passed: 125 pytest tests, examples/demo.py, and diff checks. Accepted MADRs remain unchanged in intent and are reconciled.
  head: 187bc2907b28dd5993da956cb56d7f53e1437a11..187bc2907b28dd5993da956cb56d7f53e1437a11
  began: 2026-08-28T08:16:48.555Z  ended: 2026-08-28T08:28:49.915Z

[2026-08-28-008] completed
  outcome: Fix the six findings from the post-merge review of main: the MADR references that absorb's id remapping invalidated, a format specification that could target the wrong column, a date pattern silently ignored on timestamps, publish failures surfacing as INTERNAL, and the incomplete AGENTS.md structure table
  accept: Every Decision History entry in .seal/madr/ names the outcome whose began/ended window contains its timestamp, and the remapping is noted where a reader would look for it
  accept: format_spec.columns matches an exact column name before any lenient form and refuses a key that matches several columns, with candidates
  accept: date_format applies to timestamp values when no timestamp_format is given, symmetrically with how a date value falls back
  accept: A failed publish of an exported file returns a structured recoverable error naming the path instead of INTERNAL
  accept: AGENTS.md's structure table lists every module under agent_backend/core and the DataSpace smoke layers under examples/
  decisions: 0005
  verify: uv run pytest -q && grep -q 'core/export/' AGENTS.md && grep -q '2026-08-28-007' .seal/madr/0005-*.md
  machine-verification: passed (exit 0, 21674 ms, workspace da494bf05403)
  note: All six review findings fixed and verified. (1) The 18 MADR Decision History entries that absorb's post-merge id remapping had invalidated now name the outcome whose window contains their timestamp, and every affected record carries a line saying why; the outcome log itself was not touched. (2) format_spec column keys resolve exact names before any lenient form and refuse a key that fits several columns with candidates, so a key naming one column exactly can no longer be applied to another whose normalized form collides; lenient matches are reported as resolution notes and the renderer is built before staging so a bad key costs no work. (3) A date_format pattern now renders timestamp values instead of being silently ignored. (4) Publishing an export maps filesystem refusals to EXECUTION_FAILED with the path and a directory target to INVALID_INTENT, replacing the non-recoverable INTERNAL the staging rewrite had introduced. (5) The head range of outcome 2026-08-28-006 stops one commit short of the work it produced (187bc29 was committed after that outcome closed); recorded in MADR 0005 and the reason this outcome committed before closing. (6) AGENTS.md's structure table now lists core/export, core/knowledge, core/naming and the DataSpace smoke layers. 131 tests pass, examples/demo.py is green, and dataspace_smoke.py --task all --check passes all four tasks. Work committed as 96f60e7.
  head: c16d3096a91a12620206c44a3640e0619e804a0f..96f60e756470e0e3701fa0b1cd8272e208a9324f
  began: 2026-08-28T08:54:36.296Z  ended: 2026-08-28T09:01:40.111Z

[2026-08-28-009] abandoned
  outcome: Make the answer a first-class output contract at the export boundary, and close the expression and step-shape gaps the second DataSpace measurement exposed in the frequency order it produced
  accept: export_result accepts a column selection naming exactly the columns and the order the exported file must carry, refuses unknown or duplicate names with candidates, and records the selection in the operation, the execution plan and the dataset.exported audit event; the MCP tool description states that the file must carry exactly the columns the answer asks for
  accept: strftime accepts both (temporal, pattern) and (pattern, temporal), and a type error on an allowlisted function names the argument order it expected
  accept: A date truncation function is in the allowlist (day, month, year) and aggregate group_by accepts a derived expression with an alias, each covered by a test
  accept: A filter accepts an IN list written with brackets, and an untyped step whose single key names a step type with an object body is read as that step, each covered by a test
  accept: examples/dataspace_smoke.py --task all --check still passes, and examples/dataspace_agent.py re-run on task_44 and task_329 (3 runs each) is recorded in examples/dataspace/README.md with pass rate, turns, cost and error distribution against the second 2026-08-28 measurement
  decisions: 0005, 0003, 0002
  verify: uv run pytest -q && uv run python examples/dataspace_smoke.py --task all --check
  note: Closed before any durable content changed: the delivery goal moved during design discussion. Accept 1 asked export_result for a column projection, which still relies on the agent remembering the answer shape at export time; the agreed direction is a declared output contract that the backend verifies at export, so the projection form is dropped and a new outcome states the contract-based goal.
  head: 8ffc3b4f8af7aba64868badeb9c44eca423b4637..8ffc3b4f8af7aba64868badeb9c44eca423b4637
  began: 2026-08-28T09:23:26.528Z  ended: 2026-08-28T09:35:05.250Z

[2026-08-28-010] completed
  outcome: Output contracts (MADR 0007): the agent declares the shape of its deliverable and the backend holds export_result to it, plus the expression and step-shape gaps the second DataSpace measurement exposed, in the frequency order it produced
  extend: Record the trust model the output contract rests on as MADR 0008 (fresh output accepted, recalled output verified against backend records) and reconcile it at closure
  accept: A declare_output operation records an output contract (ordered column names, optional logical types, optional row cardinality: one, at_least_one, one_per keys) in the operation log and audit trail; one contract is open per workspace; amending it requires a reason and is recorded as its own audit event; each is covered by a test
  accept: export_result with an open contract verifies column names, order and types against the planned output schema before writing and the row cardinality against the physical table before publish; a mismatch returns CONTRACT_MISMATCH (recoverable) with declared versus actual and hints to reshape (carrying the select step) or to amend; a matching export closes the contract and the dataset.exported audit event carries the verification evidence; export without a contract is unchanged
  accept: The MCP surface exposes declare_output and its descriptions say the file must carry exactly the columns the answer asks for and that declaring them first lets the backend hold the export to it; README states the project boundary: consistency with what the agent declared, not correctness against what it never knew
  accept: strftime accepts both (temporal, pattern) and (pattern, temporal), and a type error on an allowlisted function names the argument order it expected
  accept: A date truncation function is in the allowlist (day, month, year) and aggregate group_by accepts a derived expression with an alias, each covered by a test
  accept: A filter accepts an IN list written with brackets, and an untyped step whose single key names a step type with an object body is read as that step, each covered by a test
  accept: examples/dataspace_smoke.py --task all --check still passes with each scripted task declaring its output contract first, and examples/dataspace_agent.py re-run on task_44 and task_329 (3 runs each) is recorded in examples/dataspace/README.md with pass rate, turns, cost and error distribution against the second 2026-08-28 measurement
  decisions: 0007, 0005, 0004, 0003, 0002, 0008
  verify: uv run pytest -q && uv run python examples/dataspace_smoke.py --task all --check && grep -q 'declare_output' agent_backend/mcp/tools/registry.py
  machine-verification: passed (exit 0, 91848 ms, workspace e5531080c1d6)
  note: All seven accepts delivered and verified (154 tests, scripted smoke 4/4 with contracts declared first, declare_output on the MCP surface). Output contracts (MADR 0007, accepted): declare_output records ordered columns, optional types and row cardinality as an operation and audit event, one current contract per workspace, amendment with a reason recorded before/after, identical re-declaration a no-op; export_result verifies names, order, types by family and cardinality before reading or writing, refuses with CONTRACT_MISMATCH carrying the repair transform, and records satisfaction with evidence; a satisfied contract keeps holding re-exports until a new one is declared. The trust model is recorded as MADR 0008. Vocabulary: strftime accepted in either order and reordered with a note, type errors carry the signature, date/date_trunc, group_by over a named expression derived first, IN with brackets, step bodies nested under their own key. Third measurement recorded in examples/dataspace/README.md: task_44 0/3 (turns 29.3 to 26.3, errors 11 to 8), task_329 0/3 (turns 22.0 to 15.0, errors 26 to 9), strftime refusals 13 to 0; five of six runs exported correct values with extra columns the agent had itself declared, at turns 7 to 28 rather than fresh, so the failure moved from drift to reading; one CONTRACT_MISMATCH was repaired in one turn. Next inputs recorded in both READMEs and MADR 0003: select after aggregate in compound objects, group_by without measures, a semi-join for filters referencing another dataset, and asking for the declaration first in the scenario prompt. Committed as 56063e6 before closing.
  head: 8ffc3b4f8af7aba64868badeb9c44eca423b4637..56063e6608e208c4f6793008b936ff5874425a0b
  began: 2026-08-28T09:35:41.247Z  ended: 2026-08-30T03:52:27.294Z

[2026-08-30-001] completed
  outcome: Record the champion-pipeline comparison on task_44 and task_329 in the DataSpace README, and leave a HANDOFF.md that tells the next agent what to read
  accept: examples/dataspace/README.md carries the upstream/main champion run (commit bdc874f, same model and gateway) on task_44 and task_329 with official verdict, output columns, turns and input tokens next to Intentum's third measurement, and states what the comparison shows
  accept: HANDOFF.md at the repository root names, in reading order, the documents, the outcome and the MADRs a new agent needs, and nothing else
  decisions: 0003, 0004
  verify: test -f HANDOFF.md && grep -q 'bdc874f' examples/dataspace/README.md && grep -q '0007' HANDOFF.md && uv run pytest -q
  machine-verification: passed (exit 0, 23689 ms, workspace 0f1a81313819)
  note: Champion comparison recorded in examples/dataspace/README.md next to the third measurement (task_44 passed on one column at 14 turns / 211k; task_329 failed with the same extra date column three Intentum runs had declared, at 12 turns / 183k; the semi-join is the remaining capability gap, the question reading a shared limit). HANDOFF.md at the root lists what a new agent reads and in what order: AGENTS.md, README.md sections 1 and 7, the DataSpace README, outcomes 2026-08-28-010 and this one, MADRs 0004, 0008, 0007, 0003, 0002. AGENTS.md's table points at it. Committed as 078079d before closing.
  head: 2be509bbd0a99d9931bcd1a4a7fac49c8e5df637..078079da6ccc6289619e029395235d48da565c83
  began: 2026-08-30T04:15:41.165Z  ended: 2026-08-30T04:17:17.334Z

[2026-08-31-001] completed
  outcome: Close the four next DataSpace findings in measured order: test a fresh output declaration, then support post-aggregate projection, distinct grouping, and cross-dataset semi-joins
  extend: Measure task_44 after exposing semi_join in the MCP transform vocabulary
  accept: examples/dataspace_agent.py asks the model to declare the exact ordered output columns before importing or exploring; task_44 and task_329 are re-run three times each before vocabulary changes, and examples/dataspace/README.md records pass rate, declaration turn, turns, prompt tokens, cost and error distribution against the third measurement
  accept: An untyped compound transform applies select after aggregate when the selected names require aggregate outputs, while preserving the existing pre-aggregate meaning when every selected name is already in the input; both orders are covered by tests and visible in canonical IR
  accept: An aggregate with group_by and no measures returns one row per distinct group, rejects an aggregate with neither groups nor measures, and is covered through resolver, validator, planner and SQL execution
  accept: A semi_join step resolves and validates a right dataset plus one or more comparable key pairs, keeps only left rows with a matching right row without duplicating them, records the right dataset in canonical IR, plan and lineage, and is covered by preview and materialization tests
  accept: The MCP and README transform documentation describe post-aggregate select, distinct grouping and semi_join without benchmark-specific core language; the full pytest suite and all four scripted DataSpace smoke tasks pass
  accept: After semi_join is available to the model, task_44 is re-run three times and examples/dataspace/README.md records official verdict, output columns, turns, prompt tokens, cost, error distribution and semi_join usage against the declaration-first experiment
  decisions: 0003, 0004, 0008, 0002
  verify: uv run pytest -q && uv run python examples/dataspace_smoke.py --task all --check && grep -q 'semi_join' agent_backend/mcp/tools/registry.py && grep -q 'declaration-first' examples/dataspace/README.md && grep -q 'post-semi-join' examples/dataspace/README.md
  machine-verification: passed (exit 0, 104481 ms, workspace 4fddef53855a)
  note: All six accepts delivered and freshly verified: declaration-first framing was measured on task_44 and task_329 before vocabulary changes; compound select now moves after aggregate only when it references aggregate outputs; measureless group_by produces distinct groups and empty aggregates remain invalid; semi_join carries a typed, versioned right dataset through IR, validation, plan, EXISTS execution and lineage without right-side duplication; MCP and README documentation state the general semantics. Final verification passed 160 tests and all four scripted DataSpace tasks at official accuracy 1.0. The post-semi-join task_44 model measurement remained 0/3 (26.7 mean turns, 336k prompt tokens, /usr/bin/zsh.0032, 14 tool errors): one run invoked semi_join successfully but chose mismatched stay identifiers, one exported all four correct procedure values with an extra treatment id column, and one exhausted its turns. The remaining measured gap is model reading and relationship selection, not backend expressiveness. MADRs 0003, 0004 and 0008 remain accepted; 0002 raw_query remains proposed.
  head: 91a06f4e0fe9d19bed9e161f4ae5f627aeb4fb32..4885c65ec25d7e795ea30849a6e57ebaa889adc0
  began: 2026-08-31T06:34:10.584Z  ended: 2026-08-31T07:22:06.422Z

[2026-09-02-001] completed
  outcome: Split the agent harness from the backend: agent_harness/ becomes a sibling package that drives agent_backend only through its MCP tool surface and owns the model loop, perception of unstructured sources and the DataSpace scenario moved out of examples/, with the boundary enforced by a test
  extend: Record the harness boundary and the location of perception as MADR 0009, and reconcile MADR 0004's statement that scenario code lives under examples/
  accept: agent_harness/ is a top-level package beside agent_backend/, excluded from the wheel, holding the model configuration and client, a generic tool-calling loop over an MCP server, a perception package reserved for readers of unstructured sources, and scenarios/dataspace/ with the task framing, scripted agents, vendored evaluator, scoring, run output and measurement README moved from examples/; examples/ keeps only the backend's own demo, sample data and MCP config
  accept: A pytest test enforces the boundary: modules under agent_harness/ import from agent_backend only its public API and agent_backend.mcp.server, no module under agent_backend/ imports agent_harness, and no module under agent_backend/ names the benchmark
  accept: python -m agent_harness.scenarios.dataspace.smoke --task all --check passes the official evaluator on all four scripted tasks with unchanged tool-call scripts, and both runners resolve DATASPACE_BENCHMARK and KDDCUP_CHAMPION from the environment or the repository .env, as the model configuration already is
  accept: AGENTS.md, README.md, HANDOFF.md, .gitignore and the moved DataSpace README describe the two packages, the boundary rule and the new commands, and HANDOFF.md names outcome 2026-08-31-001, which it had omitted
  accept: MADR 0009 records that perception of unstructured sources is the harness's responsibility and that its products enter the backend only through import_dataset and attach_metadata; MADRs 0004 and 0003 are reconciled at closure
  decisions: 0004, 0003, 0009
  verify: uv run pytest -q && uv run python -m agent_harness.scenarios.dataspace.smoke --task all --check && test ! -e examples/dataspace_smoke.py && test ! -e examples/dataspace_agent.py && test ! -e examples/dataspace && grep -q agent_harness AGENTS.md && grep -q agent_harness README.md && grep -q agent_harness HANDOFF.md && grep -q 'agent_harness/scenarios/dataspace/runs' .gitignore && ls .seal/madr/0009-*.md >/dev/null
  machine-verification: passed (exit 0, 30410 ms, workspace 9f9688bd341f)
  note: All five accepts delivered and freshly verified (165 tests including five boundary tests, scripted DataSpace 4/4 at official accuracy 1.0 through the new entry point). agent_harness/ now holds config, the OpenAI-compatible client, a generic tool loop over an MCP server, an empty perception package that states its rule, and scenarios/dataspace with framing, scripted agents, the vendored evaluator, scoring, both runners and the measurement README moved from examples/ with framing and scripts unchanged; runs land under agent_harness/scenarios/dataspace/runs (git-ignored). tests/test_boundary.py enforces the import rules, the absence of scenario names in the backend and the wheel exclusion; no agent_backend module changed. Both runners read DATASPACE_BENCHMARK and KDDCUP_CHAMPION from the environment or .env, and the local .env now carries the benchmark at ~/dataset/DataSpace/release/DataSpace-Benchmark; the model keys are absent locally, so model-driven runs need them added before the next measurement. AGENTS.md, README.md (sections 2 and 7), HANDOFF.md and the DataSpace README describe the two packages and the boundary; HANDOFF.md now names outcome 2026-08-31-001. MADR 0009 accepted; 0004 and 0003 reconciled. Committed as f78872c on branch agent-harness before closing. Next: the first perception reader (PDF) chosen by the first measured task that needs it.
  head: fdae3604850d7f1c3f39984c253824854f55dc8f..f78872c43b0dd4df54f0a4198bbbeebade98e996
  began: 2026-09-02T06:11:06.325Z  ended: 2026-09-02T06:19:37.653Z

[2026-09-02-002] completed
  outcome: Teach on refusal: a recovery module in the backend turns every refusal and two silent-failure signals into structured advice that names what the backend accepts instead and, when mechanical, rewrites the agent's own request; the harness measures convergence (refusals without advice, repair rate) and nudges on stalled previews; both measured on task_44 and task_329
  extend: Record the strategy as MADR 0010 and reconcile it at closure
  accept: agent_backend/core/recovery/ turns a BackendError into advice entries on the error response, each with a kind, an explanation of what the backend read and what it accepts instead, and, when the repair is mechanical, a rewrite: the agent's own request as tool calls it can send as-is; CONTRACT_MISMATCH carries the same structure
  accept: Detectors cover the refusals measured on 2026-09-02 as general rules, each with a test: a subquery inside an expression becomes a semi_join (with the right side materialized first when it is filtered); a distinct key or prefix becomes a measureless group_by; a join on written as an equality becomes a key mapping; LIKE becomes contains, starts_with or ends_with; an aggregate function inside select becomes an aggregate step; an inline relation given as source is materialized first; an unknown step key is mapped to the nearest known key
  accept: Successful transform responses carry advice on two silent-failure signals, each with a test: an empty result whose equality literal is absent from the filtered column reports where that literal does occur across the workspace; a result whose columns match, or mechanically reshape to, the open output contract says so with the export or reshape call
  accept: The MCP instructions and the transform tool description explain advice; README documents it under failure semantics and maps core/recovery in the repository structure; AGENTS.md maps the module
  accept: The harness records advice kinds per tool event, reports per run and in the summary the number of refusals, refusals without advice and the repair rate (a refusal followed by a successful call of the same tool within two calls), and the loop nudges the model after four consecutive previews of one source with nothing materialized
  accept: agent_harness/scenarios/dataspace/README.md records the pre-advice task_44 runs of 2026-09-02 (0/3, all at the turn limit, none exported, 12 refusals none naming the accepted shape) and, after the module, task_44 and task_329 three runs each with official verdict, declared columns, turns, prompt tokens, refusals, unadvised refusals, repair rate and nudges, against the pre-advice runs
  decisions: 0003, 0004, 0007, 0009, 0010
  verify: uv run pytest -q && uv run python -m agent_harness.scenarios.dataspace.smoke --task all --check && grep -q advice agent_backend/mcp/server/main.py && grep -q advice README.md && grep -q 'core/recovery' AGENTS.md && grep -q '2026-09-02' agent_harness/scenarios/dataspace/README.md && grep -q 'repair_rate' agent_harness/scenarios/dataspace/agent.py
  machine-verification: passed (exit 0, 31460 ms, workspace 28bb0ee6ffe2)
  note: All six accepts delivered and freshly verified (191 tests, scripted DataSpace 4/4). agent_backend/core/recovery turns every refusal into advice with kind, explanation and, when mechanical, the request rewritten as tool calls; eleven detectors from the measured refusals, CONTRACT_MISMATCH carrying the reshape and the export, and two success-side signals (value_not_found with the literal located across the workspace, matches_contract/near_contract with the next call); every rewrite is executed in its test. Along the way: a compact object's select waits for a sort that names a dropped field; documents resolve with a leading slash or by file name. The MCP instructions, the transform tool description, README failure semantics and structure, AGENTS.md and HANDOFF.md describe advice. The harness records advice kinds, reports refusals, unadvised refusals and repair rate per run and in the summary, nudges after four previews of one source, and the scripted layer now writes under runs/<task>/scripted after it wiped a model run's directory mid-measurement (that run was repeated). Measured, recorded in the DataSpace README: pre-advice task_44 0/3 with 12 refusals none naming the accepted shape and no export; task_329 two rounds 0/3 with unadvised refusals 7 to 3 to 0, repair rate 1.0, turns 15.0 to 11.7, prompt tokens 195k to 126k; task_44 0/3 but exports 0/3 to 3/3 each carrying the four gold values in gold order, turns 30.0 to 26.0, prompt tokens 425k to 336k, unadvised refusals 12 to 5, repair rate 0.8, semi_join taken up from the rewrite in one run. The verdicts stay 0/3 on the declared extra column, the model's reading, outside the backend by MADRs 0007/0008. The five unadvised task_44 refusals became four more fixes (document_as_dataset, document leniency, predicate_not_boolean, sort-aware select) that are not yet measured on a model run. MADR 0010 accepted; 0003, 0004, 0007, 0009 reconciled. Committed as 55d39ca on branch agent-harness before closing. Next: measure the four fixes on task_44, then the first perception reader for a PDF-bearing task (MADR 0009).
  head: 0538e4885bc6b541e81cfe5c80df46a7c7c3bfa4..55d39cae21ee3c5ecfe68cc7bae97c13df97120d
  began: 2026-09-02T07:42:40.877Z  ended: 2026-09-02T08:05:47.788Z

[2026-09-02-003] completed
  outcome: Record the task_44 measurement after the four post-round fixes in the DataSpace README, against the previous round
  accept: agent_harness/scenarios/dataspace/README.md replaces the note that the four fixes are unmeasured with a subsection recording three task_44 runs: official verdict, declared columns and declaration turn, turns, prompt tokens, refusals, unadvised refusals, repair rate, nudges, advice taken up and what the unadvised refusals were, against the previous round; README.md section 7 notes the first official pass
  decisions: 0003, 0010, 0007
  verify: uv run pytest -q tests/test_recovery.py tests/test_boundary.py && grep -q 'task_44., three runs with the four fixes' agent_harness/scenarios/dataspace/README.md && grep -q 'first official pass' README.md
  machine-verification: passed (exit 0, 1104 ms, workspace 4b96c864b311)
  note: Recorded three task_44 runs after the four post-round fixes: 1/3 official (the model-driven layer's first pass on task_44), turns 26.7, prompt tokens 368k, refusals 6, unadvised 3, repair rate 1.0, nudges 4, exports 2/3 both with the gold values. The passing run declared at turn 21 with the data in view and named the gold shape; both turn-1 declarations added an identifier column. The four fixes did not fire (no recurrence of their shapes); the unadvised refusals were a field out of scope and twice a SQL LIMIT tail inside a filter, the next detector. README section 7 notes the first pass; MADRs 0003, 0007, 0010 reconciled. Committed as af48227 before closing.
  head: cf7d74f303371571b197e6a481650b321a5c7336..af482270b0a35389eb1e8b5eee38b6e8056e72f9
  began: 2026-09-02T08:13:41.473Z  ended: 2026-09-02T08:14:29.720Z

[2026-09-02-004] completed
  outcome: Close the two findings of the sixth measurement: a detector for a SQL LIMIT tail inside a filter expression, and an experiment on declaration timing (fresh, before exploring, versus informed, once a preview shows the answer rows) on task_44 and task_329
  accept: A limit_tail_as_step detector strips a trailing SQL LIMIT from a filter expression into a limit step, explains when it cannot, and its rewrite is executed in a test
  accept: The DataSpace runner takes --declaration fresh|informed; the informed framing asks the model to declare once a preview shows the answer rows, everything else in the framing unchanged; informed runs land under runs/<task>/agent-informed; run results and the summary record the declaration mode and the turn of the first successful declaration
  accept: task_44 and task_329 are run three times each with the informed framing and agent_harness/scenarios/dataspace/README.md records, under a declaration timing heading, official verdict, declared columns, declaration turn, turns, prompt tokens, refusals, unadvised refusals, repair rate and nudges against the fresh-framing runs of the same day, and states what the comparison shows
  accept: MADRs 0007 and 0008 are reconciled with the finding; README section 7 and the framing module name the two modes
  decisions: 0007, 0008, 0003, 0010
  verify: uv run pytest -q tests/test_recovery.py tests/test_boundary.py && grep -q 'limit_tail' agent_backend/core/recovery/advice.py && grep -q 'informed' agent_harness/scenarios/dataspace/agent.py && grep -q 'declaration timing' agent_harness/scenarios/dataspace/README.md && grep -q 'informed' README.md
  machine-verification: passed (exit 0, 1125 ms, workspace 5e01b943b2ce)
  note: Both findings closed and freshly verified (192 tests). limit_tail_as_step strips a SQL LIMIT tail from a filter into a limit step, explains when a limit already exists, and its rewrite is executed in its test. The runner takes --declaration fresh|informed with the framing differing in one bullet; informed runs land under runs/<task>/agent-informed; results and summaries carry the declaration mode and the first declaration turn. Measured, recorded under 'Declaration timing' in the DataSpace README: task_44 informed 2/3 against fresh 1/3, task_329 informed 1/3 (its first pass) against fresh 0/3; gold-shape declarations 3/6 against 1/6; turns, prompt tokens, refusals and nudges no worse; the contract held informed declarations at export as it holds fresh ones. Informed is now the runner's default; README section 7 and framing.py say so. MADRs 0007 (requirement means question plus data shape), 0008 (unaffected), 0003 and 0010 reconciled. Committed as f455984 and a follow-up before closing. Next: the first perception reader for a PDF-bearing task (MADR 0009).
  head: 96412594efabc8c3e623eed10c398264121fcf8f..9d15dd592a51ceb6aa1959e458219331abcf2c92
  began: 2026-09-02T08:16:27.112Z  ended: 2026-09-02T08:25:56.322Z

[2026-09-02-005] completed
  outcome: Run the DataSpace scenario through OpenCode as the agent host: a host adapter launches opencode run against the backend's MCP server with only the backend tools enabled, normalizes its JSON event stream into the harness's tool-event record so the convergence metrics apply unchanged, and task_44 and task_329 are measured through it against the in-process loop
  extend: Record the host decision as MADR 0011 and reconcile it at closure
  accept: agent_harness/hosts/opencode.py generates a per-run opencode.json (custom OpenAI-compatible provider from the harness settings, a local MCP entry running agent-backend-mcp on the run's workspace and export root, a primary agent whose prompt is the backend instructions plus the scenario framing and whose builtin tools are disabled with only the backend MCP tools allowed), runs opencode run --format json --auto --pure under a timeout, and parses the event stream into tool events (tool, arguments, status, code, summary, advice kinds, turn) and token totals; a test parses a saved event stream
  accept: The DataSpace runner takes --host loop|opencode; an opencode run produces the same run_result.json fields (official verdict, declaration turn, convergence, tokens) plus host and host version, and the summary carries the host; runs land under runs/<task>/<host>-<declaration>
  accept: task_44 and task_329 are run three times each through OpenCode with the informed framing and agent_harness/scenarios/dataspace/README.md records official verdict, declared columns, declaration turn, steps, prompt tokens, refusals, unadvised refusals, repair rate and advice taken up against the in-process loop's informed runs, and states what the comparison shows
  accept: MADR 0011 records the host decision (OpenCode over the in-process loop, Kilo CLI and Claude Code), what stays in the harness (scenario, measurement, adapters), and that pacing and perception now come from the host; MADR 0009 is reconciled; README sections 2 and 7, AGENTS.md and HANDOFF.md describe the host adapter and how to run it
  decisions: 0009, 0003, 0004, 0011
  verify: uv run pytest -q tests/test_hosts.py tests/test_boundary.py tests/test_recovery.py && grep -q 'opencode' agent_harness/scenarios/dataspace/agent.py && grep -q 'OpenCode' agent_harness/scenarios/dataspace/README.md && ls .seal/madr/0011-*.md >/dev/null && grep -q 'hosts' AGENTS.md
  machine-verification: passed (exit 0, 1958 ms, workspace 16e30312164b)
  note: All four accepts delivered and freshly verified (host, boundary and recovery tests). agent_harness/hosts/opencode.py: per-run opencode.json with the harness's model settings as an OpenAI-compatible provider, the backend's MCP entry point on the run's workspace and export root, and a primary agent with every builtin tool disabled and only backend_* allowed; opencode run --format json --auto --pure under a 900 s timeout; the event stream parsed into tool events (backend status, code, summary, advice kinds, step), token totals and the host's cost estimate; host-side refusals read from state.error with SDK argument-validation errors mapped to INVALID_INTENT as the loop records them; a trimmed event stream from the task_10 spike is the test fixture. The runner takes --host opencode (default) or --host loop; run records carry host and version; runs land under runs/<task>/opencode-<declaration>. Measured, recorded in the DataSpace README: task_44 2/3 under OpenCode as under the loop with the same gold-shape declarations and fewer steps (23.3 against 26.3); task_329 0/3 against the loop's 1/3, within noise at three runs; prompt tokens higher under OpenCode (no tool-result truncation, host context); no run reached the timeout; the advice chain worked unchanged under the foreign host. Unadvised refusals from the host boundary recorded as next fixes: JSON strings where objects are expected (MCP layer), inline relations as strings and as join right sides. MADR 0011 accepted; 0009 partly superseded and reconciled; 0003, 0004 reconciled; README sections 2 and 7, AGENTS.md, HANDOFF.md and the DataSpace README describe the host and how to run it. Committed before closing. Next: those three fixes, then the with-and-without-backend comparison on one host, then the first PDF-bearing task read by the host.
  head: a4105f1251b223a3cdaad729b1b205118af74965..102a64f33d1f26c09ed0b2117c058a7ca90b3cb4
  began: 2026-09-02T08:54:21.535Z  ended: 2026-09-02T09:09:06.121Z

[2026-09-02-006] partial
  outcome: Run OpenCode from a neutral working directory so the model's system prompt carries only the agent prompt the harness writes: no AGENTS.md or CLAUDE.md picked up from the repository or the home directory; then re-measure task_44 and task_329 through OpenCode and replace the contaminated comparison
  extend: Package the host as a Docker image: OpenCode at a pinned version plus the backend's MCP entry point in a baked environment, run per measurement with the run directory and the benchmark mounted, an empty HOME and working directory inside, so no global config, plugin, instruction file or session state of the machine reaches the model; the re-measurement runs in the image
  accept: agent_harness/hosts/opencode.py runs opencode from a per-run directory outside any repository whose ancestors hold no AGENTS.md or CLAUDE.md, with the Claude Code fallbacks disabled, and references the prompt file by absolute path; a test checks the working directory has no instruction file above it and the config's prompt path is absolute
  accept: A task_10 run through the fixed adapter shows a first-step prompt within a few hundred tokens of the spike run made outside the repository (5194), against the 7.6k of the contaminated runs; the DataSpace README records the contamination (which runs, how many tokens per step) and the check
  accept: task_44 and task_329 are re-run three times each through OpenCode and the DataSpace README's OpenCode comparison is replaced by the clean runs, keeping the contaminated numbers as a footnote, with what the comparison shows against the in-process loop
  accept: MADR 0011 is reconciled with the finding: the host loads instruction files from the working directory's ancestors, so the adapter owns where it runs
  accept: agent_harness/hosts/opencode.Dockerfile builds an image with opencode pinned by build argument and the backend installed from the lockfile into /opt/venv (agent-backend-mcp on the image); --host opencode-docker runs each measurement in a fresh container from that image with the run directory mounted at /run, the benchmark read-only at /data and an empty HOME and cwd, paths in the framing translated to the container's, the same event stream parsed and the image tag recorded in the run
  decisions: 0011, 0003
  verify: uv run pytest -q tests/test_hosts.py tests/test_boundary.py && grep -q 'neutral' agent_harness/hosts/opencode.py && grep -q 'AGENTS.md' agent_harness/scenarios/dataspace/README.md && test -f agent_harness/hosts/opencode.Dockerfile && grep -q 'opencode-docker' agent_harness/scenarios/dataspace/agent.py
  note: Redirected by the user before completion: the container is to be the only OpenCode host. Delivered and kept: the Docker image (agent_harness/hosts/opencode.Dockerfile, OpenCode 1.18.26 plus the backend's MCP entry point in /opt/venv), the container mode of the adapter, and the measurement of the contamination (runs whose opencode.json sat inside the repository carried the repository's AGENTS.md: 7.6k first-step tokens against 5.2k outside and 5.06k in the container, about 2.4k tokens per step). Built and then dropped: the machine host's neutral working directory, which proved insufficient because OpenCode also searches for instruction files above the config file it loads; the user chose to run only in the container. The task_44 and task_329 re-measurement in the container is in progress and is recorded by the next outcome, which also removes the machine host.
  head: 99cf7d901192b6ecff900d1fd5edf45be14b8636..99cf7d901192b6ecff900d1fd5edf45be14b8636
  began: 2026-09-02T09:12:53.959Z  ended: 2026-09-02T09:31:03.671Z

[2026-09-02-007] completed
  outcome: The container is the only OpenCode host: remove the machine-host path and the neutral working directory, keep the Docker image (OpenCode pinned, the backend's MCP entry point baked in, run directory and data mounted, empty HOME) as --host opencode beside the in-process control arm, and replace the contaminated OpenCode comparison with runs made in the container
  accept: agent_harness/hosts/opencode.py runs OpenCode only in a container from the image built by opencode.Dockerfile: per-run opencode.json and prompt.txt written into the run directory and read at /run, the backend's MCP entry point at /opt/venv inside, the benchmark read-only at /data, an empty HOME and cwd, the caller's uid, paths in the framing translated to the container's, the JSON event stream parsed as before, stdin closed and the container killed on timeout; the module has no machine-host branch and no neutral-directory helper; tests cover the config, the command, the path translation and the event parsing
  accept: The DataSpace runner's --host is opencode (the container, default) or loop; --image names the image; runs land under runs/<task>/opencode-<declaration>; run records carry the image tag as the host version
  accept: agent_harness/scenarios/dataspace/README.md records the contamination (the machine runs of 2026-09-02 carried the repository's AGENTS.md, 7.6k first-step tokens against 5.06k in the container) and replaces the OpenCode comparison with task_44 and task_329 three runs each in the container against the in-process loop's informed runs, with what the comparison shows
  accept: MADR 0011 is reconciled (the host loads instruction files from the working and config directories, so the container is the only host; the machine host is gone); README sections 2 and 7, AGENTS.md and HANDOFF.md describe the container host, the image build and how to run it
  decisions: 0011, 0003
  verify: uv run pytest -q tests/test_hosts.py tests/test_boundary.py && test -f agent_harness/hosts/opencode.Dockerfile && ! grep -q 'neutral_cwd' agent_harness/hosts/opencode.py && grep -q 'AGENTS.md' agent_harness/scenarios/dataspace/README.md && grep -q 'opencode.Dockerfile' HANDOFF.md
  machine-verification: passed (exit 0, 1148 ms, workspace 0c1faaf34615)
  note: Delivered the container-only OpenCode host, updated the DataSpace runner and isolated comparison documentation, reconciled MADRs 0011 and 0003, and passed the recorded verification.
  head: 99cf7d901192b6ecff900d1fd5edf45be14b8636..99cf7d901192b6ecff900d1fd5edf45be14b8636
  began: 2026-09-02T09:31:03.763Z  ended: 2026-09-02T09:38:08.322Z

[2026-09-02-008] completed
  outcome: Separate what a deliverable carries from what it is organized by in the output contract, answer a declaration with the workspace facts about the names it uses, and require the row cardinality
  accept: declare_output takes order_by; order keys and one_per keys may name columns the answer does not carry; the backend orders the export by them and leaves them out of the file, and refuses a dataset that lacks one of them the way it refuses a missing declared column
  accept: A declaration is answered with what the workspace holds under each declared name (dataset, column, type, role, whether it is unique per row) or that it matches nothing yet; it is never a refusal and the backend still never sees the question
  accept: rows is required in a declaration; a declaration without it is refused with advice naming the three forms it accepts
  accept: A MADR records why the contract separates carried columns from organizing columns and what was rejected; README section 1 and the DataSpace framing describe the grammar
  decisions: 0007, 0010
  verify: uv run pytest -q
  machine-verification: passed (exit 0, 7668 ms, workspace 48bb0932b47c)
  note: The output contract now names two kinds of column (MADR 0012). columns is what the answer carries, in order; order_by and the one_per keys are what it is organized by, may name columns the answer does not carry, are expected in the dataset at export so the backend can sort and count by them, and are left out of the file. A missing organizing column is refused like a missing declared column and the mechanical repair keeps them. rows is required, with declaration_rows and declaration_order advice extending teach-on-refusal to declare_output, which had none. A declaration is answered with what the workspace holds under each of its names (dataset, column, type, role, unique per row), never as a refusal; the backend still never sees the question. Verified: 206 tests pass, and the four scripted DataSpace tasks still pass end to end through the MCP surface under the official evaluator. MADR 0012 accepted with the four rejected options; 0007 and 0010 reconciled; README section 1, the DataSpace framing and HANDOFF describe the grammar. Committed as 79f311f before closing. Not measured: the prediction is that early declarations stop carrying the sort key, testable on task_44 and task_329 against the recorded fresh and informed arms. That is the next outcome.
  head: 99cf7d901192b6ecff900d1fd5edf45be14b8636..79f311f5fea45c5d2bf5833ba7c7b86951935587
  began: 2026-09-02T09:12:46.479Z  ended: 2026-09-02T09:25:39.159Z
```
