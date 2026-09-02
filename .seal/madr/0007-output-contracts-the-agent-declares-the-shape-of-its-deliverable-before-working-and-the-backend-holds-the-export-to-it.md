# 7. Output contracts: the agent declares the shape of its deliverable before working, and the backend holds the export to it

Date: 2026-08-28

## Status

Accepted

## Context and Problem Statement

The second DataSpace measurement (2026-08-28) lost four runs whose exported values were correct but carried one column too many, although the prompt said "exactly the columns the question asks for". The agent had read the requirement at turn 1 and no longer acted on it at turn 17: the failure is drift between what it knew and what it did, not a missing capability. Today nothing at the export boundary can express the expected shape of the output; export_result writes the dataset as it is and reports its columns afterwards. The backend, unlike a coding-agent verifier, already owns the facts that decide the question: the planner knows the output schema before execution, the physical table exists before publish, and export is staged so refusing to publish costs nothing. This mirrors the DriftSeal protocol (write the outcome first, bind verification to the contract, close only with fresh evidence) with one difference that makes it stronger here: the verifier is the backend, not a command the agent wrote.

## Decision Drivers

* Guard the failure class the backend can actually see: inconsistency between a declared intent and an executed operation
* Keep the check outside the agent context so it survives long sessions and context loss
* Verification by the backend from facts it owns (planned schema, physical table), with no agent-supplied verifier to trust
* Stay general (0004): every agent that delivers data to a consumer has an output contract; nothing here names a benchmark

## Considered Options

* Require the agent to add a select step before export (rejected: the same memory the drift already lost; nothing verifies it)
* A column projection parameter on export_result (rejected as the primary form: still relies on the agent remembering the shape at export time; it is the degenerate case of a contract declared at the last moment)
* Let export silently project the dataset to the declared columns (rejected: hides the inconsistency the contract exists to surface, and the audited file would no longer match its dataset)
* Make a contract mandatory for every export (rejected: turns a lever into a toll on every call; encouraged through the tool description instead)
* Embed the DriftSeal CLI or its event log (rejected: it is shaped around Git worktrees; only the protocol shape, declare, bind, verify, close, is absorbed onto the existing operation and audit records)
* Declared contract, verified by the backend at export, closed with evidence, amendable only explicitly (chosen)

## Decision Outcome

Add an output contract to the core. The agent may declare, before or during its work, the shape of the deliverable: ordered column names with optional logical types and an optional row cardinality (exactly one row, at least one row, one row per key). The declaration is an operation, recorded in the audit log, and one contract is open per workspace at a time; changing it is an explicit amendment that requires a reason and is recorded, never a silent overwrite. export_result verifies against the open contract: column names, order and types against the planned output schema before anything is written, cardinality against the physical table before publish. A mismatch is a structured recoverable error (CONTRACT_MISMATCH) that shows declared versus actual and offers two ways out: reshape the dataset (the hint carries the select step) or amend the contract. A matching export closes the contract and the audit event carries the verification evidence (columns, rows, content hash). Exports without a contract behave as before. The backend checks consistency with what the agent declared, not correctness against a question it never sees; that boundary is the project scope (see 0004): failures of "knew but did not do" are Intentum's problem, failures of "did not know" belong to the model and the agent framework.

## Consequences

* A new operation kind and a new error code enter the public API and the MCP surface
* Row-level assertions beyond cardinality (uniqueness of a key, no nulls, value predicates) are a natural extension and are deferred until measurement asks for them
* The audit trail can show when a contract was declared, amended and why, and which export satisfied it
* A wrong contract is enforced as faithfully as a right one; the backend does not and cannot judge the question

## Decision History

<!-- driftseal-reconciliation: 40913461-3c3d-4d86-af14-4e39b8983483 -->
### 2026-08-30T03:29:24.617Z — Outcome `2026-08-28-010`

Status: Accepted → Accepted

Implemented in outcome 2026-08-28-010: declare_output (agent_backend/core/backend.py) records an OutputContract (core/models/entities.py, storage output_contracts table), core/contracts/spec.py reads the loose declaration and compares a dataset with it, and export_result verifies columns, order, types by family and row cardinality before anything is read or written, raising CONTRACT_MISMATCH with the repair transform when the fix is mechanical. One refinement to the decision text: a satisfied contract stays the workspace's current contract, so a re-export over the deliverable is still held to it, until a new contract is declared; declaring a new one after satisfaction needs no reason, amending an open one does. The scripted DataSpace layer declares a contract first on all four tasks and passes.

<!-- driftseal-reconciliation: 7d090afe-d3de-4d27-8a9c-8be6194791fa -->
### 2026-08-30T03:48:36.482Z — Outcome `2026-08-28-010`

Status: Accepted → Accepted

Evidence from the third measurement: the model declared its contract at turns 7 to 28, not while the question was fresh, and in five of six runs the declaration encoded a misreading (extra descriptive columns), which export_result then enforced faithfully; one CONTRACT_MISMATCH was repaired in a single turn from the error's problem list. The mechanism works as decided; whether a fresh declaration reads the question better is the next experiment, to be run from the scenario prompt in examples/, not from core.

<!-- driftseal-reconciliation: 5c29497d-050b-49de-99a1-9f307d920197 -->
### 2026-09-02T08:04:42.976Z — Outcome `2026-09-02-002`

Status: Accepted → Accepted

Contracts now teach as well as hold: a preview or dataset with the declared shape is told so (matches_contract) with the materialize call, one that is a rename or projection away gets the reshape (near_contract), and CONTRACT_MISMATCH at export carries the reshape and the export as a rewrite. Measured on task_329 and task_44: every run took matches_contract up and exported within two or three calls; the verdicts stayed 0/3 because the declared shape itself carried an extra column, which the contract holds by design.

<!-- driftseal-reconciliation: 6371cba5-bf5f-4ac1-84a5-54fc419eaf9d -->
### 2026-09-02T08:14:19.388Z — Outcome `2026-09-02-003`

Status: Accepted → Accepted

First counter-example to declaration-first: the run that passed task_44 declared at turn 21 with the answer rows in view and named the gold shape exactly; both turn-1 declarations in the same round added an identifier column, as every early declaration has. One run is not evidence; the trust model (fresh accepted, recalled verified) is unaffected, but when to declare belongs in the next experiment.

<!-- driftseal-reconciliation: d8d21ad2-5115-482e-ac3e-92d77b2e55be -->
### 2026-09-02T08:25:06.800Z — Outcome `2026-09-02-004`

Status: Accepted → Accepted

Declaration timing measured (2026-09-02, twelve runs): declarations made once a preview showed the answer rows named the gold shape 3/6 times against 1/6 for declarations made from the question alone; passes 1/6 to 3/6 (task_44 2/3, task_329 1/3, its first), turns, tokens and refusals no worse. The contract held informed declarations at export exactly as fresh ones. 'While the requirement is in front of you' now reads as the question and the shape of the data together; the harness default framing is informed. The MCP instruction sentence is unchanged and compatible; small sample, same direction on both tasks.

<!-- driftseal-reconciliation: 3b443840-d174-45ce-95e6-a11144fab492 -->
### 2026-09-02T09:23:08.930Z — Outcome `2026-09-02-006`

Status: Accepted → Accepted

The contract grammar now separates the columns the deliverable carries from the columns it is organized by (MADR 0012): order_by and the one_per keys may name columns the answer does not carry, the export sorts and counts by them and leaves them out of the file, and rows is required. The declaration is also answered with what the workspace holds under each declared name. What the backend holds the export to is unchanged; what the agent can say in a declaration is wider.
