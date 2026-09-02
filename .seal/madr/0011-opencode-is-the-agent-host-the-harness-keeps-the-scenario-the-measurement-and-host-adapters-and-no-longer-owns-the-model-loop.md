# 11. OpenCode is the agent host; the harness keeps the scenario, the measurement and host adapters, and no longer owns the model loop

Date: 2026-09-02

## Status

Accepted

## Context and Problem Statement

The harness's own loop (config, model client, tool loop: about 240 lines) drove seven measurements and made the convergence metrics possible, but it is the weakest link for what comes next: perception of PDFs and video would have to be written into it, the research hypothesis speaks of a general-purpose agent rather than a purpose-built loop, and the comparison that tests the hypothesis (the same agent with and without the backend) needs a real agent that can also be given shell and python. Candidates: keep the in-process loop; Kilo Code CLI (the user's gateway, but a fork of OpenCode with thinner documentation and an unclear MCP story in non-interactive mode); Claude Code headless or the Claude Agent SDK (native PDF and image reading, hooks, stream-json, but Claude models only, so no continuity with the qwen3.5 measurements); OpenCode (open source, documented config, opencode run --format json, per-agent tool allow and deny lists with MCP wildcard names, any OpenAI-compatible provider, session export). A spike on task_10 through OpenCode with only the backend's MCP tools passed the official evaluator, and its event stream carried every tool call with the backend's structured response, advice included, plus per-step tokens.

## Decision Drivers

* The hypothesis names a general-purpose agent; a real, documented, open host is the honest subject
* Continuity of measurement: the same model and gateway as the seven earlier measurements, through a host that takes any OpenAI-compatible provider
* Controllability: per-agent tool allow and deny lists with MCP wildcard names, a JSON event stream with tool inputs, outputs and tokens, and a config the harness writes per run

## Considered Options

* Keep the in-process loop as the only host (rejected as the main path: perception and the with-and-without-backend comparison would have to be built into it; kept as the control arm)
* Kilo Code CLI (rejected: a fork of OpenCode with thinner documentation; MCP in non-interactive runs not documented)
* Claude Code headless or the Claude Agent SDK (deferred: best perception and hooks, but Claude models only; the natural host for a strong-model arm later)
* OpenCode with a per-run config and a tool-restricted agent (chosen)

## Decision Outcome

OpenCode runs the model. agent_harness/hosts/opencode.py writes a per-run opencode.json (the harness's model settings as an OpenAI-compatible provider, the backend's MCP entry point on the run's workspace and export root, one primary agent whose prompt is the backend instructions plus the scenario framing, with every builtin tool disabled and only backend_* allowed), runs opencode run --format json --auto --pure under a timeout, and normalizes the event stream into the harness's tool-event record so convergence metrics, declaration turn and scoring apply unchanged. The in-process loop stays as the control arm (--host loop) and keeps its directory names; --host opencode is the external host. Pacing (nudges, turn caps) and perception (reading documents and media) are the host's from here on; the harness's perception package is not developed further unless the restricted arm needs a perception tool exposed over MCP. Each measurement records the host and its version. The direct arm, the same host with shell and python and without the backend, is the next experiment the hypothesis needs.

## Consequences

* Results now depend on a host with its own prompt assembly and version drift; every measurement records host and version, and the control arm stays available
* The stall nudge and turn cap do not apply under OpenCode; a run is bounded by a wall-clock timeout instead
* Cost is unavailable from the gateway and only estimated by the host; token counts are the comparable measure
* Perception moves to the host; MADR 0009's perception package stays only as the place for an MCP-exposed reader if the restricted arm ever needs one

## Decision History

<!-- driftseal-reconciliation: df4df95a-baa9-49d5-a2be-1ed04c1dc6d6 -->
### 2026-09-02T09:08:36.883Z — Outcome `2026-09-02-005`

Status: Accepted → Accepted

Implemented in outcome 2026-09-02-005: agent_harness/hosts/opencode.py (per-run opencode.json, tool-restricted agent, event-stream normalization), --host opencode as the runner's default with the in-process loop as --host loop. Measured against the loop on the same day and model: task_44 2/3 under both hosts with the same gold-shape declarations; task_329 0/3 against 1/3, within noise at three runs; prompt tokens higher under OpenCode (no tool-result truncation, host context); no run reached the timeout; the advice chain (rewrites taken up, contract mismatch repaired) worked unchanged under the foreign host. Host and version are recorded in every run.
