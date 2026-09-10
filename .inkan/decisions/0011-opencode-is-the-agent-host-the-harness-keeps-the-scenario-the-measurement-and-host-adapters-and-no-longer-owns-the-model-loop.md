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

<!-- driftseal-reconciliation: ef136a33-81bf-40b8-99ef-d75a6ee737ae -->
### 2026-09-02T09:31:03.486Z — Outcome `2026-09-02-006`

Status: Accepted → Accepted

The host loads instruction files (AGENTS.md, CLAUDE.md) upwards from both its working directory and the directory of the config file it loads: machine runs whose opencode.json sat in the run directory inside this repository carried the repository's AGENTS.md (7.6k first-step tokens against 5.2k for the same prompt outside the repository and 5.06k in the container). The Docker image built in this outcome (opencode.Dockerfile: OpenCode 1.18.26, the backend's MCP entry point in /opt/venv, run directory at /run, data read-only at /data, empty HOME) passes task_10 with a clean prompt. The user then chose the container as the only OpenCode host; the machine host and its neutral directory are removed in the next outcome.

<!-- driftseal-reconciliation: 1444ebd7-497b-4def-9aaf-1ae71e0d8818 -->
### 2026-09-02T09:35:50.094Z — Outcome `2026-09-02-007`

Status: Accepted → Accepted

The container is the only OpenCode host (outcome 2026-09-02-007): the machine-host path and its neutral working directory are removed, since OpenCode also loads instruction files from above the config it reads and the run directories live inside this repository. Clean measurement in the container: task_44 2/3 (as the loop), task_329 0/3 (loop 1/3), first-step prompt 5.0k tokens against 7.6k on the machine; the advice chain and the export sandbox worked unchanged under the host. Every run records the image tag as the host version; rebuild the image after a backend change.

<!-- driftseal-reconciliation: 195eee20-b3bb-4aff-a20e-71f16ad31992 -->
### 2026-09-02T09:37:49.993Z — Outcome `2026-09-02-007`

Status: Accepted → Accepted

Reconciled with outcome 2026-09-02-007: OpenCode is now container-only because it discovers instruction files from working and configuration directory ancestry; the machine-host path was removed while the in-process loop remains the control arm.

### 2026-09-10T07:21:00.696Z, outcome 2026-09-10-0707-x3nb

Status: accepted -> accepted

Outcome 2026-09-10-0707-x3nb activates the reserved MCP perception path with an opt-in --video flag: OpenCode still owns the model loop and interpretation, the harness supplies deterministic timestamped images, and builtin tools remain disabled. A random visual-code probe verified image delivery through the configured model and gateway before one task_312 run; audio transcription remains outside this reader.
