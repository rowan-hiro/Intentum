# 4. Intentum is a general agent-ready backend; benchmarks such as DataSpace are validation scenarios, not the product

Date: 2026-08-28

## Status

Accepted

## Context and Problem Statement

Intentum was started from a general specification: a deterministic semantic substrate between probabilistic agents and data systems (resolve -> canonical IR -> validate -> plan -> execute -> commit -> audit). The KDD Cup 2026 DataSpace benchmark and its champion pipeline were adopted as the first realistic validation scenario because they supply hundreds of heterogeneous workspaces with gold answers and a strong baseline. Working against one benchmark creates constant pressure to add task-specific conveniences, prompt text, evaluator logic or naming heuristics to the core, which would turn a general backend into a competition entry.

## Decision Drivers

* Preserve the research hypothesis: a small semantic interface plus a deterministic backend, independent of any one agent or dataset
* Keep the abstraction level high enough that a second scenario (different domain, different agent) needs no core changes
* Avoid benchmark overfitting masquerading as backend design

## Considered Options

* Optimize Intentum directly for KDD Cup / DataSpace scores (rejected: collapses the project into a competition entry and invalidates the hypothesis)
* Keep benchmarks entirely out of the repository (rejected: loses the only realistic, gold-labelled validation data available)
* General core, scenario code confined to examples/, findings generalized before entering core (chosen)

## Decision Outcome

Intentum remains an independent, general backend. Benchmarks are used to validate it, never to define it. Concretely: the core package (agent_backend/) accepts only general invariants and general loose-intent tolerance (e.g. export sandbox root, Unicode identifiers, knowledge-document ingestion, key-name aliases, output-format specifications); no task ids, benchmark paths, evaluator logic, scoring code or agent prompt text may live in core. Everything scenario-specific (vendored evaluators, scripted or LLM task drivers, task framing prompts, findings) lives under examples/ and is documented as a validation scenario. A benchmark finding becomes a core change only after it is restated as a general backend requirement; findings that cannot be generalized stay in examples. Documentation and decision records describe DataSpace as 'used to validate', not as the goal.

## Consequences

* Some benchmark-driven improvements will be slower to land because they must first be restated generally
* examples/ carries scenario-specific code, vendored evaluators and prompts; the core carries none
* A second validation scenario should be added when convenient to keep the abstraction honest

## Decision History

<!-- driftseal-reconciliation: 92994ae6-a73f-422d-a782-4ac094d19382 -->
### 2026-08-30T03:29:25.267Z — Outcome `2026-08-28-010`

Status: Accepted → Accepted

Boundary sharpened by 0007 and 0008 and stated in README: the backend guards consistency with what the agent declared (knew but did not do), not correctness against a task it never sees (did not know), which belongs to the model and the agent framework. Nothing in core names a benchmark; the contract, the date functions, the argument-order rule, bracket IN lists and nested step bodies were each restated as general requirements before entering core.

<!-- driftseal-reconciliation: 20105a6a-070c-497b-972a-abcbb0d96c8f -->
### 2026-08-30T04:16:53.048Z — Outcome `2026-08-30-001`

Status: Accepted → Accepted

The champion comparison supports the boundary: the full-SQL pipeline misreads task_329 the same way, so the extra column is the model's reading, not a property of the backend; the general step it points at (a semi-join) is restated in the README before it enters core.

<!-- driftseal-reconciliation: 8cb86a21-3c3c-4191-a591-5a491ee61eaa -->
### 2026-08-31T07:18:25.248Z — Outcome `2026-08-31-001`

Status: Accepted → Accepted

The boundary remains intact. post-aggregate select, distinct grouping and semi_join are general IR operations with no benchmark identifiers or evaluator logic in agent_backend; declaration framing, model traces, scoring and task-specific findings remain under examples/. The post-semi-join measurement did not justify any task-specific core behavior.
