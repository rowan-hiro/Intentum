# 5. Apply value formatting at the export boundary through an explicit format specification, not inside transforms or the engine

Date: 2026-08-28

## Status

Accepted

## Context and Problem Statement

Task questions often prescribe how values must be rendered (round to at most N decimals, strip trailing zeros, integers keep one decimal, dates without time). In the task_10 smoke both layers showed that leaving number-to-text rendering to the engine produces text such as 31783696.814999998 for 31783696.815, and that a model in the loop tries to satisfy rendering rules inside the transform with string functions (strftime, format, substr, ||), looping until the turn cap even after a correct result existed. Rendering is not a property of the data; it is a property of the file handed to another system.

## Decision Drivers

* End the formatting loops observed with a model in the loop by making the capability discoverable where the need arises
* Keep datasets typed and comparable; rendering must not leak into stored data
* Deterministic, reproducible files: the same dataset version plus the same specification yields the same bytes

## Considered Options

* Render inside transforms via string functions (rejected: turns typed columns into text, pollutes lineage, and is what the model attempted unsuccessfully)
* Rely on the engine's default number-to-text conversion (rejected: engine-dependent digits such as 31783696.814999998; no way to express task rules)
* Explicit format specification applied at export (proposed)

## Decision Outcome

export_result takes an optional format specification applied only when writing the file: per-column rules (decimals with half-up rounding, strip_trailing_zeros, integer_min_decimals, date/timestamp render pattern, null text) plus file-level defaults. The managed dataset keeps its typed values; the specification is recorded in the export's operation record and audit event together with the content hash, so an export is reproducible. The transform vocabulary gains no text-rendering functions for this purpose, and the tool description states that rendering belongs to export so agents can discover it.

## Consequences

* export_result grows a small, well-typed sub-schema that must be validated like the IR
* Two exports of the same dataset can differ in text while the dataset is unchanged; the audit trail must carry the specification
* Some evaluators compare typed values and would accept unformatted output; the specification still matters for human-facing files and for scorers that compare text

## Decision History

*Outcome ids in this section were realigned on 2026-08-28: merging two branches made `driftseal absorb` renumber the colliding ids in the outcome log, while these entries had been written with the pre-merge ids. Each entry was matched back to its outcome by timestamp; the log itself was not touched.*

<!-- driftseal-reconciliation: ca7f6bc7-bd6b-4313-ac44-44ad2db9446a -->
### 2026-08-28T07:56:58.848Z — Outcome `2026-08-28-006`

Status: Proposed → Accepted

Implemented: export_result takes a validated format specification (file-level defaults plus per-column overrides; decimals rounded half-up on the shortest decimal form, strip_trailing_zeros, integer_min_decimals, date_format/timestamp_format with locale-dependent directives refused, null_text), recorded in the operation's canonical IR, in the plan as a FormatValues step, and in the dataset.exported audit event next to the content hash; two exports of the same version with the same specification are byte-identical. The MCP tool description states that rendering belongs to export. Evidence: task_10's champion-scorer score went 0.45 to 1.0 with the data unchanged, and the model-driven layer went 2/3 to 3/3 with mean turns 27 to 17 because the rendering loop ended.

<!-- driftseal-reconciliation: 329642f2-18c0-43a5-8a3b-db297ec4959e -->
### 2026-08-28T08:25:15.850Z — Outcome `2026-08-28-007`

Status: Accepted → Accepted

Formatted numeric export now uses an isolated Decimal context sized for the result and a rounding carry, preserving half-up behavior for large stored values. All file exports are staged on the destination filesystem and published only after writing and hashing succeed; failed rendering preserves existing targets, and non-overwrite publication refuses a concurrently created target.

<!-- driftseal-reconciliation: 5810bc03-d9e0-48fe-b10b-5a701195e3fd -->
### 2026-08-28T08:27:26.544Z — Outcome `2026-08-28-007`

Status: Accepted → Accepted

Independent review found that atomic formatted overwrite could widen a private target from 0600 to 0644. Publication now preserves an existing target's permission bits before replacement, with regression tests for formatted CSV, native CSV, and Parquet; the format specification and typed data contract are unchanged.

<!-- driftseal-reconciliation: b13694d8-ced6-4aee-a8b1-e5f77fa4993f -->
### 2026-08-28T09:00:43.782Z — Outcome `2026-08-28-008`

Status: Accepted → Accepted

Two corrections from the post-merge review, both inside the accepted decision rather than changing it: a format_spec column key now matches an exact column name before any lenient form and refuses a key that fits several columns (previously a key naming one column exactly could be applied to another whose normalized form collided), and a date_format pattern also renders timestamp values instead of being silently ignored. Lenient column matches are now reported as resolution notes. For provenance: the specification itself was implemented by outcome 2026-08-28-006, whose recorded head range e494004..0ffce62 stops one commit short of the work, which landed in commit 187bc29 created after that outcome was closed.
