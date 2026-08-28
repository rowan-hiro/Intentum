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
