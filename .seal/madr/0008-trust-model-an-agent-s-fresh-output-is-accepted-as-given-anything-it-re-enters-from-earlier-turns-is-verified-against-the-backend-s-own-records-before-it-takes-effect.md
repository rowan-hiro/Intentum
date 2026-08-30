# 8. Trust model: an agent's fresh output is accepted as given; anything it re-enters from earlier turns is verified against the backend's own records before it takes effect

Date: 2026-08-28

## Status

Accepted

## Context and Problem Statement

The backend sits between a probabilistic agent and durable data, and it has no access to the task the agent is working on. It therefore cannot verify the agent's first statement on any subject: how it read the task, a number it just computed, a transform it just wrote. What it can verify is consistency over time, because it keeps the durable record: every operation, its canonical IR, the resulting datasets and versions, the audit trail and the physical tables. The DataSpace measurements show where errors actually enter: not in the first reading of a requirement but in its reuse many turns later, after the context has filled with exploration. The output contract (0007), idempotent replay and materialize-by-reference all rest on the same assumption and it should be stated once.

## Decision Drivers

* The backend cannot judge correctness against a task it never sees, but it can judge consistency against records it owns
* Errors enter through reuse under a long context, not through the first reading; verification effort should go where the errors are
* Keep agent-framework concerns (self-review, multi-round critique) out of the backend

## Considered Options

* Trust the agent uniformly and verify nothing beyond schema and type validity (rejected: leaves the observed drift failures untouched)
* Distrust the agent uniformly and demand justification for every input (rejected: the backend has nothing to check a first reading against; it would only add ceremony)
* Fresh output accepted, recalled output verified against the backend record, unverifiable changes recorded with a reason (chosen)

## Decision Outcome

Adopt an asymmetric trust model. The agent's fresh output, produced while the subject is in front of it, is accepted as given; whether the agent framework reviews it with further rounds is the framework's concern, not the backend's. From then on, whenever the agent reuses an earlier result, value or shape from its own memory, the backend treats that input as unreliable and releases it only after checking it against its records: the declared contract, the operation log, the metadata store or the physical table. Where a check is possible the backend performs it and refuses on mismatch (an export against a declared contract, a replayed request against its fingerprint). Where it is not possible, because the input is a new judgement about something the backend never saw, such as an amended contract, the backend requires a stated reason and records it so the drift is visible to whoever reads the audit trail. Design consequence: the agent's context is not a store. Anything that must survive turns lives in the backend and is used by reference (dataset, version, contract, operation); operations should make pointing at a record cheaper than retyping its content.

## Consequences

* Justifies the shape of 0007: a contract declared fresh is the reference; an export attempted later is the thing checked
* Idempotent replay returning the recorded result rather than recomputing is the same rule applied to requests
* The transform vocabulary should let the agent reference earlier results instead of copying values into literals, for example filtering against another dataset; a copied literal is exactly the input this model distrusts (deferred until measurement asks for it)
* A wrong first reading is enforced faithfully; the model accepts that limit and leaves it to the agent framework

## Decision History

<!-- driftseal-reconciliation: 177c489d-6dab-4ffa-9b4b-1bd5b4f21e7c -->
### 2026-08-30T03:29:24.843Z — Outcome `2026-08-28-010`

Status: Accepted → Accepted

Implemented where the backend can check a recalled input against its record: export_result against the contract declared earlier (0007), idempotent replay against the stored fingerprint, and an identical re-declaration of an open contract recognized as unchanged rather than refused. Amending a contract, which the backend cannot verify, requires a reason and is recorded with the shape before and after. The deferred consequence stands: the vocabulary does not yet let a filter reference another dataset, so a copied literal is still the way to carry a value between steps.

<!-- driftseal-reconciliation: af4411a0-1739-4008-852a-635ee2a094a7 -->
### 2026-08-30T03:48:36.715Z — Outcome `2026-08-28-010`

Status: Accepted → Accepted

Observed in the third measurement: a wrong first reading enforced as faithfully as a right one (five of six runs), exactly the accepted limit; and the deferred consequence made concrete, five subquery-in-filter refusals where the agent wanted to reference another dataset rather than copy values.
