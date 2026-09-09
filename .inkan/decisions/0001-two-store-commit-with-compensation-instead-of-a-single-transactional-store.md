# 1. Two-store commit with compensation instead of a single transactional store

Date: 2026-08-27

## Status

Accepted

## Context and Problem Statement

Metadata lives in SQLite and analytical data in DuckDB; a materialization must create a physical table and its metadata atomically, but there is no cross-store transaction.

## Decision Drivers

* atomic visible state across two engines without 2PC
* failed operations must remain inspectable

## Considered Options

* Single DuckDB database for both metadata and data (rejected: loses SQLite/Postgres path for metadata and concurrent readers)
* Two-phase commit emulation with a journal (deferred: overkill for the prototype)

## Decision Outcome

Execute DuckDB work first (CREATE TABLE AS is atomic), then commit all metadata (dataset, columns, version, lineage, audit, operation, idempotency key) in one SQLite transaction; if that commit fails, drop the physical table. The operation record is written 'pending' before any work so failures stay visible. Idempotency is keyed on the canonical IR fingerprint so retries replay instead of duplicating.

## Consequences

* A crash between DuckDB commit and SQLite commit can leave an orphan table; integrity_report() detects orphan tables and pending operations for cleanup.

## Decision History

*Outcome ids in this section were realigned on 2026-08-28: merging two branches made `driftseal absorb` renumber the colliding ids in the outcome log, while these entries had been written with the pre-merge ids. Each entry was matched back to its outcome by timestamp; the log itself was not touched.*

<!-- driftseal-reconciliation: 4b5faafe-feca-4e19-bb77-2edba95a9cee -->
### 2026-08-27T15:54:49.317Z — Outcome `2026-08-27-001`

Status: Accepted → Accepted

Implemented in core/backend.py (_run_transform, _commit_materialization, _replay) and verified by tests/test_failures.py and tests/test_idempotency.py

<!-- driftseal-reconciliation: 01d2ea37-233a-42c4-9da1-fa1317406216 -->
### 2026-08-28T08:25:14.989Z — Outcome `2026-08-28-007`

Status: Accepted → Accepted

Extended import compensation to cover ordinary exceptions from loading, temporal refinement, and post-refinement schema inspection before metadata commit. Failed operations remain auditable and the same import can retry without orphan tables. The accepted process-crash window is unchanged.

<!-- driftseal-reconciliation: a7e7e030-1042-40c9-b6e3-13b8e16ba102 -->
### 2026-08-28T08:27:25.823Z — Outcome `2026-08-28-007`

Status: Accepted → Accepted

Reconciled after the overwrite-permission follow-up: import compensation and the accepted crash window are unchanged; the existing ordinary-failure regression tests remain part of the cumulative verifier.
