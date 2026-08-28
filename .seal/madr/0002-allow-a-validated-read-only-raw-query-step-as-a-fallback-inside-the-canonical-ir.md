# 2. Allow a validated read-only raw_query step as a fallback inside the canonical IR

Date: 2026-08-28

## Status

Proposed

## Context and Problem Statement

The semantic transform vocabulary (select, filter, aggregate, sort, limit, rename, derive, join) cannot express a long tail of analytical shapes that real tasks need, e.g. DataSpace questions requiring window functions ('one row per fund, prefer the public code'), ties on extremal queries (WHERE x = (SELECT MAX(x) ...)), or UNION across same-shaped sources. Returning INVALID_TRANSFORM for all of them would make comparison experiments against the KDD Cup 2026 champion pipeline (which has full SQL via solver.py) fail on vocabulary rather than on design. The project principle still holds: arbitrary SQL must not be the primary agent-facing API and the backend must keep ownership of identifiers, provenance, idempotency and replay.

## Decision Drivers

* Reach task coverage parity with a full-SQL agent without abandoning provenance, idempotency and replay
* Measure the gap of the semantic vocabulary empirically instead of guessing which steps to add
* Keep the backend the owner of identifiers and versions even when SQL is written by the agent

## Considered Options

* Reject everything the semantic steps cannot express (rejected: blocks experiments on vocabulary, not on design)
* Expose a separate execute_sql / restricted query tool (rejected: results bypass the IR, operation record, lineage and materialization path; becomes the primary interface in practice)
* Extend the semantic vocabulary first and only then decide (deferred: still needed, but the long tail is unbounded; raw_query supplies the data on what to add)
* raw_query step inside the canonical IR with placeholder-bound inputs and read-only validation (proposed)

## Decision Outcome

Add a raw_query step type to the canonical IR rather than a separate execute_sql tool. The step carries read-only SQL that references inputs only through placeholders (e.g. $input) which the backend binds to concrete dataset versions; physical table names are never visible to the agent. The backend validates the statement against a SELECT/WITH allowlist and a banned-keyword list (DDL, DML, ATTACH, COPY, LOAD, ...), derives output_schema via DESCRIBE before execution, and then treats the step like any other: it is part of the operation record, the idempotency fingerprint, explain output, lineage, and model-free replay; results still require materialize_result to persist. Responses flag used_raw_query so experiments can measure how often the semantic vocabulary was insufficient and which shapes fell through, guiding which semantic steps to add next.

## Consequences

* SQL text inside the IR is not type-checked by the validator's rules; correctness of raw_query relies on DuckDB DESCRIBE for schema and on the allowlist for safety
* Placeholder binding means raw_query SQL is portable across dataset versions and replayable against a snapshot
* Every raw_query use is countable; a high rate on a query shape is a signal to promote it to a semantic step
* Prompt guidance for SQL dialect details partially returns for the fallback path only; the primary path stays SQL-free

## Decision History

<!-- driftseal-reconciliation: 1a62dc04-a1ee-4bdb-ae1b-c73c87dd11c6 -->
### 2026-08-28T07:02:23.103Z — Outcome `2026-08-28-005`

Status: Proposed → Proposed

Still deferred after the task_10 smoke: across one scripted and three model-driven runs no query shape required SQL; every refusal was a loose-shape or formatting issue now scoped to MADRs 0005/0006 and outcome 2026-08-28-005. Revisit after the next structured-only tasks are measured.

<!-- driftseal-reconciliation: 7842e592-8bfc-4920-9596-368fe930352e -->
### 2026-08-28T07:57:20.159Z — Outcome `2026-08-28-005`

Status: Proposed → Proposed

Still deferred after the second measurement. Across four scripted tasks and twelve model-driven runs no answer required SQL: the scripted layer expresses all four tasks with join, filter, derive, aggregate, sort, limit and select, and every model refusal was a loose shape, an argument order, a missing date-part function or an output-contract mistake. Revisit if a measured task needs a window, a tie-aware extremum or a union.
