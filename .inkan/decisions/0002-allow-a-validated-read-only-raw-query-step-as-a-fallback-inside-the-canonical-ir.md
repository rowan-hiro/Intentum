# 2. Allow a validated read-only raw_query step as a fallback inside the canonical IR

Date: 2026-08-28

## Status

Accepted

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

*Outcome ids in this section were realigned on 2026-08-28: merging two branches made `driftseal absorb` renumber the colliding ids in the outcome log, while these entries had been written with the pre-merge ids. Each entry was matched back to its outcome by timestamp; the log itself was not touched.*

<!-- driftseal-reconciliation: 1a62dc04-a1ee-4bdb-ae1b-c73c87dd11c6 -->
### 2026-08-28T07:02:23.103Z — Outcome `2026-08-28-006`

Status: Proposed → Proposed

Still deferred after the task_10 smoke: across one scripted and three model-driven runs no query shape required SQL; every refusal was a loose-shape or formatting issue now scoped to MADRs 0005/0006 and outcome 2026-08-28-005. Revisit after the next structured-only tasks are measured.

<!-- driftseal-reconciliation: 7842e592-8bfc-4920-9596-368fe930352e -->
### 2026-08-28T07:57:20.159Z — Outcome `2026-08-28-006`

Status: Proposed → Proposed

Still deferred after the second measurement. Across four scripted tasks and twelve model-driven runs no answer required SQL: the scripted layer expresses all four tasks with join, filter, derive, aggregate, sort, limit and select, and every model refusal was a loose shape, an argument order, a missing date-part function or an output-contract mistake. Revisit if a measured task needs a window, a tie-aware extremum or a union.

<!-- driftseal-reconciliation: 06c3d536-6de9-4005-8676-1ec12a4c5fa5 -->
### 2026-08-30T03:48:36.270Z — Outcome `2026-08-28-010`

Status: Proposed → Proposed

Still not needed after the third measurement. The one SQL shape the model reached for, a subquery inside a filter (five times on task_44), is a request to reference another dataset's values, which a semi-join step expresses inside the IR with full provenance; raw_query would hide exactly the reference the trust model (0008) wants visible. Remains proposed.

<!-- driftseal-reconciliation: 597e08a2-a6bf-4422-8fe6-66a4cd7f8286 -->
### 2026-08-31T07:18:43.765Z — Outcome `2026-08-31-001`

Status: Proposed → Proposed

raw_query remains unnecessary for the measured contract. The repeated SQL-subquery shape from task_44 is now represented by semi_join with typed keys, versioned references and lineage; measureless grouping covers the distinct-key shape. The follow-up failures came from output-shape reading and identifier semantics rather than an inexpressible query. Revisit only when a measured task needs a remaining long-tail shape such as a window, union or tie-aware extremum.

### 2026-09-11T12:28:26.132Z, outcome 2026-09-11-1124-g80s

Status: proposed -> accepted

Implemented in outcome 2026-09-11-1124-g80s. raw_query is the first step of a transform, one read-only DuckDB SELECT or WITH statement whose placeholders are bare table names: input is the source, other datasets are bound under inputs and resolved like any reference. DuckDB's parser drives validation: DDL, DML, ATTACH, COPY, LOAD, INSTALL, PRAGMA, SET, CALL, multiple statements, file, network and other non-allowlisted table functions, replacement scans, storage and qualified names, and parameters are refused, each with advice naming the accepted shape. The output schema comes from DESCRIBE. The statement runs in a sandbox with no external access, extension loading or setting changes. The step is part of the operation record, idempotency fingerprint, explain, lineage and replay, and responses carry used_raw_query. An optional --query-timeout interrupts a long statement with a structured error. Accepted because windows over groups, tie-aware extrema and unions are common analytical shapes the semantic steps cannot express; the semantic steps remain the primary language.

### 2026-09-11T22:52:41.209Z, outcome 2026-09-11-2249-91ms

Status: accepted -> accepted

Review of the implementation found that the deadline bounded less than it claimed: DuckDB evaluates some expressions while binding a statement, a COLUMNS lambda for example, without checking for interrupts, so an interrupt could arrive while a statement bound and be lost before it ran. The deadline is now what one connection can enforce. The guard keeps interrupting until the work ends and raises as soon as a binding that ignored the interrupts returns, so nothing runs after the deadline has passed, and each step that binds or runs is guarded. A single binding can still outlast the deadline; bounding that too would mean a process per sandbox step, which is not worth its cost for an expression shape no ordinary statement has. The limit is stated in the README, in the CLI help and in the pull request, and is covered by tests.
