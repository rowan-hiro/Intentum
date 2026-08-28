# 3. Sequence the DataSpace adaptation: identifiers, workspace import, semantic layer, then a task_10 smoke test, then vocabulary and export

Date: 2026-08-28

## Status

Accepted

## Context and Problem Statement

Intentum is to be validated on the DataSpace benchmark (410 heterogeneous task workspaces, 60 with public gold) as the scenario behind the research hypothesis, using the KDD Cup 2026 champion pipeline (~/dev/kddcup2026_champion, MADR 0002 there) as the comparison baseline. Reviewing that pipeline against Intentum exposed six gaps: (1) slugify only keeps [a-z0-9], so the Chinese table and column names that dominate DataSpace cannot be imported; (2) there is no bulk import of a task workspace (csv + {table,records} json + multi-table sqlite) and no Artifact entity for raw files/documents that lineage can point to; (3) each task ships a knowledge.md column-level semantic layer (meaning, unit, grain) that the resolver cannot yet ingest; (4) the transform vocabulary lacks distinct, union, window/rank with ties, not-null shorthand and date formatting; (5) there is no export operation that validates the output contract (rectangular UTF-8 CSV, English column names, unit-free numbers) and applies formatting rules such as rounding and trailing-zero removal; (6) no raw_query fallback (MADR 0002). Doing all six before any end-to-end run risks building vocabulary and export rules against imagined rather than observed needs.

## Decision Drivers

* Get to a real end-to-end DataSpace case as early as possible
* Let observed task requirements, not guesses, shape the transform vocabulary and export rules
* Keep each step independently verifiable and closable as its own DriftSeal outcome

## Considered Options

* Implement all six gaps before the first end-to-end run (rejected: designs vocabulary and export against imagined needs; delays feedback)
* Start with raw_query (MADR 0002) to reach coverage quickly (rejected for now: would hide vocabulary gaps the experiment is meant to measure)
* Start with document/video tasks (rejected: depends on the champion pipeline's extraction stack; structured-only tasks isolate the backend under test)
* 1-3, task_10 smoke, then 4-5 (chosen)

## Decision Outcome

Implement in this order: first gaps 1, 2 and 3 (Unicode-safe identifiers, import_workspace with an Artifact entity and source provenance, attach_metadata from knowledge.md); then run an end-to-end smoke test on DataSpace task_10 (a structured-only task: sqlite + csv + json, no documents or video, public gold available) through the MCP tool surface and score it with the official DataSpace evaluator; only then implement gaps 4 and 5 (transform vocabulary extensions and export_result with the output contract), shaped by what the smoke test actually required. MADR 0002 (raw_query) is not scheduled inside this sequence; its timing is decided after the smoke test and gaps 4-5 based on observed vocabulary misses. Rationale: 1-3 are prerequisites without which no DataSpace task can even be loaded, while 4-5 are best specified from a real failing case rather than in advance.

## Consequences

* The first measurable result is a single structured-only task; document and video tasks come later and depend on importing extraction outputs as datasets
* Gaps 4 and 5 may be re-scoped after the smoke test; their MADR-worthy sub-decisions (e.g. how ties are expressed) are recorded when made
* task_10 must be scored with the official evaluator, not the champion repo's local scorer, whose semantics differ (partial credit, order ignored)

### Evidence from the task_10 smoke test (2026-08-28, scripted-agent layer)

The scripted agent solved task_10 in six MCP tool calls and the official
evaluator marked it correct (242/242 rows, order-sensitive). Two findings
constrain steps 4 and 5:

1. **Export needs a numeric format specification (step 5).** The same table
   scored 1.0 with the official evaluator and 0.45 with the champion repo's
   scorer. The only difference was text: DuckDB writes the double
   `31783696.815` as `31783696.814999998`; the champion scorer rounds to two
   decimals half-up and reads `.81` against gold `.82`, while the official
   evaluator compares at the task config's four decimal places and accepts.
   The task text itself prescribes "round to at most 4 decimals, strip
   trailing zeros, integers keep one decimal", so `export_result` must take an
   output-format specification (per-column rounding / trailing-zero policy)
   applied at the boundary, rather than leaving number-to-text rendering to
   the engine or pushing it into the transform as string manipulation.
   A minimal `export_result` without formatting was added during the smoke
   because no prediction file could exist otherwise; it is the seed of step 5,
   not its completion.
2. **SQLite date columns import as `VARCHAR` (step 4/5 candidate).** DuckDB's
   SQLite scanner maps dynamically typed date text to `VARCHAR`, so
   `EndDate` shows as `string` in `describe_dataset` although it holds ISO
   timestamps. Sorting still came out right because ISO strings sort
   lexicographically, but an agent reading the schema would not expect a
   date to be a string, and date functions would refuse the column. Import
   should refine such columns to `date`/`timestamp` when every non-null value
   parses, recording the refinement as a resolution note.

A third observation is not a backend gap: the Chinese table name in the
question (货币当局资产负债表) does not resolve because the workspace's
knowledge document is English; the scripted agent searched in English.
Whether a model does that in one round is what the LLM-driven layer measures.

### Evidence from the task_10 smoke test (2026-08-28, LLM layer: qwen3.5-35b-a3b, 3 runs)

Official result 2/3 passed; mean 27 turns, ≈ $0.004 and 81 s per run
(`examples/dataspace_agent.py`, traces under `examples/dataspace/runs/`).

- Dataset discovery took two turns in every run (import listing →
  describe); the cross-language concern above did not materialize.
- The failed run divided the amount by 100 after reading the attached unit
  `亿元 (100M CNY)` as "needs conversion" — an agent semantics error, not a
  backend gap.
- Both passing runs had the correct file exported early (turn 8 / 12) and
  then spent the remaining turns trying to satisfy the question's formatting
  clause with string/date rendering in the transform (`strftime`, `format`,
  `substr`, `||`), each refused. This confirms finding 1 above with a model
  in the loop: step 5's export-format specification is what ends these
  loops, and it must be discoverable from the tool surface.
- Loose step shapes the model used and the resolver rejected — all general
  and to be accepted in step 4: single-key steps without `type`
  (`[{"filter": …}, {"select": …}, {"sort": …}]`, five occurrences);
  `sort` with `order: [{field, direction}]` (fixed immediately); SQL-style
  `x as y` aliases in `select` and in derive expressions; `derive` as a
  list of `{name, expression}`; string slicing and temporal formatting
  functions; `year()` on the VARCHAR date column (finding 2 above).
- A new invariant surfaced and was added: `export_result` must be confined
  to an export root. Before the guard, the first exploratory run wrote a
  stray file into the benchmark's input directory; with it, run 3's attempt
  to write `/tmp/...` was refused with `PERMISSION_DENIED` and the model
  recovered inside the allowed root.

### Evidence from the second measurement (2026-08-28, after steps 4-5)

Steps 4 and 5 are implemented: the export format specification (MADR 0005),
import-time temporal refinement (MADR 0006), the loose step shapes the model
had used, and a string/date vocabulary. The scripted layer now covers four
structured-only public-reference tasks (task_10, task_44, task_127, task_329)
and passes the official evaluator on all four. task_10's champion-scorer score
moved from 0.45 to 1.0 with no change to the data, which isolates the export
specification as the cause. The model-driven layer went from 2/3 to 3/3 on
task_10 with mean turns 27 → 17 and mean prompt tokens 489k → 339k: the
formatting loop that consumed the baseline's passing runs is gone, and no
refusal in twelve runs was one of the accepted loose shapes. task_127 passes
3/3 in 9-13 turns.

The two harder tasks fail for reasons the measurement can name, none of them
expressiveness — the scripted layer answers both in five calls:

1. four of the six failing runs exported the *correct values* with one column
   too many, which the evaluator rejects outright; the answer table is a
   contract the tool surface never states;
2. thirteen refusals were `strftime` written pattern-first (Python's order,
   which DuckDB also accepts) against an allowlist that takes only
   `(temporal, pattern)`;
3. the rest were date-part vocabulary (`date`, `to_char`, `strptime`, casts
   inside expressions, `group_by` over a derived expression) and, in task_44,
   finding that the patient id is reachable only through `cost.eventid`.

That ordering — argument order, answer-table contract, date parts, then the
long tail — is the input to the next outcome. Full tables and traces are in
`examples/dataspace/README.md`.

## Decision History

<!-- driftseal-reconciliation: c1815772-878f-428d-a6a2-6da3854a15dd -->
### 2026-08-28T04:05:23.140Z — Outcome `2026-08-28-001`

Status: Accepted → Accepted

Steps 1-3 implemented: core/naming.py (Unicode identifiers), import_workspace + Artifact entity + source provenance, attach_metadata with core/knowledge parser; task_10 workspace imports and its query runs; 83 tests pass. Next in sequence: task_10 smoke through MCP with the official evaluator, then gaps 4-5.

<!-- driftseal-reconciliation: 8f43f714-fb84-497c-a678-fab6eeead02b -->
### 2026-08-28T05:28:48.139Z — Outcome `2026-08-28-002`

Status: Accepted → Accepted

task_10 smoke (scripted-agent layer) done: examples/dataspace_smoke.py drives the task through six MCP tool calls; the vendored official evaluator (HKUSTDial/DataSpace 6491caa) marks it correct (242/242 rows, order-sensitive). A minimal export_result was needed to produce prediction.csv and was added. Finding for step 5: DuckDB prints 31783696.815 as 31783696.814999998, so export needs a numeric format spec; SQLite date columns arrive as VARCHAR. LLM-driven layer and steps 4-5 remain.

<!-- driftseal-reconciliation: d3930c91-93c1-45fb-b8f9-a49684a81eeb -->
### 2026-08-28T05:47:05.789Z — Outcome `2026-08-28-003`

Status: Accepted → Accepted

Body amended with 'Evidence from the task_10 smoke test': (1) export_result needs a numeric format specification (31783696.815 rendered as 31783696.814999998 scored 0.45 on the champion scorer, 1.0 officially); (2) SQLite date columns import as VARCHAR and should be refined to date/timestamp on import. Both constrain steps 4-5.

<!-- driftseal-reconciliation: 34ab744f-6955-473f-8c9b-7b2a70bf0db9 -->
### 2026-08-28T06:54:06.245Z — Outcome `2026-08-28-004`

Status: Accepted → Accepted

LLM layer of the task_10 smoke run: qwen3.5-35b-a3b through the MCP tools, 3 runs, 2/3 passed officially, mean 27 turns and ~$0.004 per run. Evidence subsection added to the body: discovery is not the problem; the failure was a unit-reading error by the agent; passing runs looped on formatting (confirms export-format specification, step 5); five loose step shapes to accept in step 4; export sandbox root added as a new invariant.

<!-- driftseal-reconciliation: e4409656-a943-4cf3-895d-d8ec8e1a7cd5 -->
### 2026-08-28T07:02:23.296Z — Outcome `2026-08-28-005`

Status: Accepted → Accepted

Steps 4-5 scoped from evidence and handed off as outcome 2026-08-28-005: export format specification (MADR 0005), import-time temporal refinement (MADR 0006), loose step shapes and string/date vocabulary; distinct/union/window remain unscheduled until a task requires them; next tasks to measure: two or three structured-only public-reference tasks.

<!-- driftseal-reconciliation: 27931ee7-a89b-4706-8967-9278dec35668 -->
### 2026-08-28T07:57:14.066Z — Outcome `2026-08-28-005`

Status: Accepted → Accepted

Steps 4 and 5 of the sequence are done. Body amended with 'Evidence from the second measurement': scripted layer extended to four structured-only public-reference tasks (task_10, task_44, task_127, task_329), all four pass the official evaluator; model-driven layer 3/3 on task_10 (was 2/3, 27 to 17 mean turns) and 3/3 on task_127. The six failing runs on task_44 and task_329 are attributed: four exported correct values with one column too many, thirteen refusals were strftime written pattern-first, the rest were date-part vocabulary and path finding. Next sequence step is that ordering, not distinct/union/window, which no measured task has required.

<!-- driftseal-reconciliation: 3e03a522-fc92-49b4-adc7-07ebc5a4b58c -->
### 2026-08-28T08:25:15.441Z — Outcome `2026-08-28-006`

Status: Accepted → Accepted

Corrected loose-expression quoting to preserve literal and quoted-identifier tokens, including doubled escapes and overlapping field names. Explicit knowledge scopes that do not match the selected datasets are now reported as unmatched instead of being applied globally. No deferred vocabulary or benchmark work was added.

<!-- driftseal-reconciliation: 66497238-f752-4a5a-ae94-db10e28e73e0 -->
### 2026-08-28T08:27:26.181Z — Outcome `2026-08-28-006`

Status: Accepted → Accepted

Reconciled after the overwrite-permission follow-up: scope-safe knowledge attachment and token-safe loose expressions are unchanged; deferred scenario and vocabulary work remains deferred.
