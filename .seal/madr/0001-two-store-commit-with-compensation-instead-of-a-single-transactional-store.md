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

<!-- driftseal-reconciliation: 4b5faafe-feca-4e19-bb77-2edba95a9cee -->
### 2026-08-27T15:54:49.317Z — Outcome `2026-08-27-001`

Status: Accepted → Accepted

Implemented in core/backend.py (_run_transform, _commit_materialization, _replay) and verified by tests/test_failures.py and tests/test_idempotency.py
