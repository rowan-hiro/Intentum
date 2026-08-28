# 6. Refine text columns that hold parseable dates or timestamps to temporal types at import time

Date: 2026-08-28

## Status

Accepted

## Context and Problem Statement

DuckDB's SQLite scanner maps SQLite's dynamically typed date text to VARCHAR, and CSV inference can do the same for unusual patterns. In task_10 the EndDate column arrived as string; sorting happened to be correct because ISO text sorts lexicographically, but describe_dataset showed a string where agents expect a date, and a model's year(EndDate) was refused with TYPE_MISMATCH. Physical types from the source are not always the semantic types.

## Decision Drivers

* Schemas shown to agents should reflect what the data means, not what the source driver happened to emit
* Temporal functions and comparisons must work on temporal data without agent-side casting
* Refinement must be deterministic and overridable

## Considered Options

* Keep source physical types as-is (rejected: agents see string dates and cannot use temporal functions)
* Let agents cast in transforms (rejected: every consumer repeats the cast; lineage shows casts instead of semantics)
* Infer with a broad set of locale-dependent patterns (rejected for now: ambiguous day/month orders would create silent errors)
* Deterministic ISO-pattern probe at import with hint override and a recorded note (proposed)

## Decision Outcome

During import, after loading, the backend probes each string column: if every non-null value parses as an ISO date or timestamp (a deterministic, documented set of patterns), the column's logical type becomes date or timestamp, the physical column is cast in the managed table, and a resolution note records the refinement. Explicit schema hints (type) always win over inference, and a column that fails the probe stays string. The refinement is recorded in the import's canonical IR so it is replayable.

## Consequences

* Import does one extra scan per string column
* A column whose values are dates today but free text tomorrow changes type between versions; the version record makes that visible
* Non-ISO date formats remain string until a pattern is added deliberately

## Decision History

<!-- driftseal-reconciliation: f23591d7-0a1a-4ef7-bce1-703ad43e8357 -->
### 2026-08-28T07:57:06.628Z — Outcome `2026-08-28-005`

Status: Proposed → Accepted

Implemented: at import every text column whose non-null values all match the documented ISO patterns and cast cleanly is promoted to date or timestamp (all bare dates give date, a mix gives timestamp), the physical column is cast, the refinement is returned as a resolution note and recorded in the import's canonical IR under refined_types, excluded from the idempotency fingerprint because it is derived from the source. Explicit type hints win; a column holding 2002-02-31 stays text. Measured cost on the task_127 workspace (10 datasets, 854k vitalperiodic rows): 53 s with the probe against 49 s without. Evidence: task_10's EndDate and task_329's intakeoutputtime now arrive as timestamp from SQLite and json text, and no run needed an agent-side cast to sort or compare a date.
