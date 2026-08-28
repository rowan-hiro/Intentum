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

## Decision History

<!-- driftseal-reconciliation: c1815772-878f-428d-a6a2-6da3854a15dd -->
### 2026-08-28T04:05:23.140Z — Outcome `2026-08-28-001`

Status: Accepted → Accepted

Steps 1-3 implemented: core/naming.py (Unicode identifiers), import_workspace + Artifact entity + source provenance, attach_metadata with core/knowledge parser; task_10 workspace imports and its query runs; 83 tests pass. Next in sequence: task_10 smoke through MCP with the official evaluator, then gaps 4-5.
