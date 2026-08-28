# 5. Apply value formatting at the export boundary through an explicit format specification, not inside transforms or the engine

Date: 2026-08-28

## Status

Proposed

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
