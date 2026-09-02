# 10. Teach on refusal: every refusal names the shape the backend accepts and rewrites the request when mechanical; the backend owns the language and the data facts, the harness owns pacing

Date: 2026-09-02

## Status

Accepted

## Context and Problem Statement

Twelve model-driven DataSpace runs across four measurements each surfaced a fresh handful of loose shapes that the resolver refused, and each measurement added acceptance for them. That does not converge: what a model may write is an open set. In the 2026-09-02 runs on task_44, twelve refusals reached the model and none said what the backend accepts for what was written (five carried no hint, six a generic one); all three runs also died on silent failures: empty previews marked success after filtering one identifier by another's value, and a preview holding the exact gold answer at turn 27 that was never materialized or exported.

## Decision Drivers

* Convergence: mapping an open set of attempts onto a closed accepted language lands every refusal in a finite target
* MADR 0004 keeps prompt text and task reading out of core; advice is structured and about the backend's own language and data
* Advice must be checkable: a rewrite the backend proposes is executed in its test and must succeed

## Considered Options

* Accept every observed loose shape in the resolver, measurement by measurement (rejected as the only strategy: the set is open and the language turns mushy; acceptance stays for shapes that are general)
* A prompt-template library inside the backend that composes agent-facing messages (rejected: prompt text in core violates 0004; the backend emits structured advice and the MCP layer renders it)
* Advice produced by the harness by matching error text (rejected: the harness knows neither the language nor the data; the backend has the intent, the scope and the tables)
* Structured advice with rewrites in core, plus pacing and convergence metrics in the harness (chosen)

## Decision Outcome

The backend keeps its transform language small and closed and teaches it on refusal. agent_backend/core/recovery turns every refusal into structured advice on the response: the kind of mistake, an explanation of what the backend read and what it accepts instead, and, when the mapping is mechanical, the agent's own request rewritten into tool calls it can send as-is. Two silent-failure signals get the same treatment on successful responses: an empty result whose filter literal is absent from its column reports where that literal does occur in the workspace; a result that already has, or mechanically reshapes to, the open output contract says so with the next call. Convergence is measured rather than assumed: the harness reports refusals without advice (to go to zero) and the repair rate, a refusal followed by a successful call of the same tool within two calls (to go to one). Pacing stays in the harness: the loop knows the turn budget and nudges after repeated previews of one source; the backend, which knows only the language and the data, never does.

## Consequences

* Every new refusal pattern becomes a detector with a test that executes its rewrite; the language does not grow for it
* Measurement reports carry refusals without advice and repair rate beside pass rate
* A detector that cannot rewrite still explains; advice never breaks a response
* Relationships that are declared nowhere in a workspace remain the model's to find; advice can only say where a value occurs

## Decision History

<!-- driftseal-reconciliation: b9032423-633a-46bb-8d78-a40f52c87eef -->
### 2026-09-02T08:04:42.804Z — Outcome `2026-09-02-002`

Status: Accepted → Accepted

Implemented in outcome 2026-09-02-002: agent_backend/core/recovery with eleven detectors (subquery as semi_join, LIKE, distinct, join on as mapping, aggregate in select, expression in select, inline source, unknown key, temporal as text, document as dataset, predicate not boolean) plus reshape_to_contract at export and two success-side signals (value_not_found, matches_contract/near_contract); every rewrite is executed in its test. Measured: task_329 unadvised refusals 7 to 3 to 0 over two rounds, repair rate 1.0, turns 15.0 to 11.7; task_44 exports 0/3 to 3/3 with the four gold values in every file, turns 30.0 to 26.0, unadvised refusals 12 to 5, repair rate 0.8; official verdicts unchanged at 0/3 because the declared extra columns are the model's reading. The five unadvised task_44 refusals became four more fixes, not yet measured.

<!-- driftseal-reconciliation: 33771115-08de-4612-8f25-561d0e9ef6b7 -->
### 2026-09-02T08:14:19.300Z — Outcome `2026-09-02-003`

Status: Accepted → Accepted

Sixth measurement (task_44 with the four post-round fixes): 1/3 official, refusals 10 to 6, unadvised 5 to 3, repair rate 1.0. The four fixes did not fire on this sample (the shapes did not recur); the pass came through near_contract, taken up under the proposed name. Next detector from the unadvised refusals: a SQL LIMIT tail inside a filter expression (twice).

<!-- driftseal-reconciliation: 5b85e14f-d13b-469d-a86f-b340e4844664 -->
### 2026-09-02T08:25:07.019Z — Outcome `2026-09-02-004`

Status: Accepted → Accepted

limit_tail_as_step added (a SQL LIMIT tail inside a filter becomes a limit step, rewrite executed in its test). In the informed-framing runs the remaining unadvised refusal was one TYPE_MISMATCH (string compared with integer), which already carries the types.

<!-- driftseal-reconciliation: c935bfde-0c44-46a7-b4c9-b9568ae28cf5 -->
### 2026-09-02T09:23:15.817Z — Outcome `2026-09-02-008`

Status: Accepted → Accepted

Two detectors extend teach-on-refusal to declare_output, which had none: declaration_rows names the three row cardinalities and says a one_per key need not be carried; declaration_order names the order_by shapes and says a sort column does not belong in columns. Neither carries a rewrite - the backend cannot guess the cardinality. The successful declaration now also carries data facts (what each declared name is in this workspace, and whether it is unique per row), which is the same division of labour on a response rather than a refusal: the backend owns the facts, the agent owns the reading.
