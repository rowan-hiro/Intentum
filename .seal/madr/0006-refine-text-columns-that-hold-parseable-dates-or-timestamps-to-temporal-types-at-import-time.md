# 6. Refine text columns that hold parseable dates or timestamps to temporal types at import time

Date: 2026-08-28

## Status

Proposed

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
