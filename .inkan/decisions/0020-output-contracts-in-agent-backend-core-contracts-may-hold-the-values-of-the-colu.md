# 20. Output contracts in agent_backend/core/contracts may hold the values of the columns they name to not_null and inclusive range checks, verified at export with evidence, with nulls unknown to a range

Date: 2026-09-30

## Status

Accepted

## Context and Problem Statement

An output contract (MADR 0007) holds the export to a declared shape: columns, order, types by family, row cardinality and the organizing columns. A file of the right shape could still carry a null where the requirement named a value, or a value outside the range the requirement stated (a percentage above 100, a date outside the reporting period), and nothing checked it. MADR 0007 deferred row-level assertions until they were asked for; issue 17 (item 3) asks for them, starting with declared non-null and range checks and leaving reconciliation and coverage relative to a source dataset for separate work. describe_dataset already profiles non-null counts and ranges; what was missing was a requirement retained from the declaration and checked at export.

## Decision Drivers

* Hold the agent to what it wrote down while the requirement was in front of it (MADR 0007, 0008), without reading the requirement
* Evidence over verdicts: a refusal shows the counts and the rows, so the agent can find where the values came from
* Say a result matches only when export would accept it, as for one_per keys

## Considered Options

* not_null and inclusive ranges on named columns, counted at export (chosen)
* Arbitrary predicates in the expression language (rejected for now: a predicate is a transform the agent can write and filter by, and as a contract it would need the resolver at declaration time, before the columns exist)
* Reconciliation with a source column and key coverage relative to a source dataset (deferred to separate work, bound to managed dataset and version references rather than copied values)
* Nulls violate a range (rejected: a null is unknown, and not_null already says whether one is allowed; treating it as outside would make every range imply not_null)
* Drop or clamp violating rows at export (rejected: the backend would change the answer to fit the declaration)

## Decision Outcome

declare_output accepts checks on names the contract uses, carried or organizing: not_null, and an inclusive min and max whose bounds are numbers, or ISO dates or timestamps for temporal columns. They are written as a list of {column, not_null, min, max} objects, as an object keyed by column, or inside a column declaration, and are stored in canonical form with the contract; changing them on an open contract is an amendment that needs a reason. export_result binds each check to the dataset column its name stands for (MADR 0018) and counts, before anything is written, the null values and the non-null values outside the range. A null value is unknown to a range and violates only not_null; a NaN is outside every range; an empty dataset passes every check, because rows already states whether the answer may be empty. A range on a column that is neither numeric nor temporal, or with bounds of the other kind, can never hold and is a mismatch. A violation is a recoverable CONTRACT_MISMATCH whose problem carries the check, the counts, the observed minimum and maximum and up to five offending rows of the columns the contract names, and no repair: the backend never changes, drops or invents a value to pass a check. A passing export records each check with its counts in the contract evidence. Transform advice counts the checks the same way before it says a result matches the contract, and names a result whose values fail (contract_checks_failed). A contract without checks behaves as before.

## Consequences

* The contract grammar grows by one optional argument and one key set inside column declarations; declarations without checks are unchanged
* Each check costs one scan of the exported dataset, plus one bounded sample query when it fails; transform advice adds the same scan when the open contract has checks
* A wrong check is enforced as faithfully as a right one; amending it needs a reason, as for the shape

## Decision History

### 2026-09-30T09:29:08.919Z, outcome 2026-09-30-0924-nvtr

Status: accepted -> accepted

Review of PR 18: temporal values and bounds are compared in the finer type of the two. A date compared with a timestamp stands for its midnight, so a timestamp bound on a date column keeps its time instead of being cut to its date, and a date bound on a timestamp column is that day's midnight.
