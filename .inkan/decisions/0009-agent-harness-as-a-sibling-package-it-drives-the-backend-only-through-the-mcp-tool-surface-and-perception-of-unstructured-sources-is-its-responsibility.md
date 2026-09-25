# 9. Agent harness as a sibling package: it drives the backend only through the MCP tool surface, and perception of unstructured sources is its responsibility

Date: 2026-09-02

## Status

Accepted

## Context and Problem Statement

Intentum's validation scenario, DataSpace, has 410 task workspaces: 384 carry PDF documents, 189 carry mp4 video and 368 carry markdown documents beyond knowledge.md. The four tasks measured so far are purely tabular. The champion pipeline handles the rest with agent-side preprocessing (PDF to markdown, video frame and slide extraction, speech transcription) before any reasoning. Until now the model loop, the scripted agents, the vendored evaluator and the measurements lived as scripts under examples/, importing the backend in-process; the backend's import_workspace lists non-tabular files as skipped. The question is where perception of unstructured sources lives, and how the agent side and the backend are kept apart while one developer iterates on both.

## Decision Drivers

* Keep the backend's contract at structured data with lineage (MADR 0004): perception is probabilistic and belongs with the agent
* Findings from the harness must flow back into general backend requirements quickly, which a second repository would slow
* The separation must be checkable by a test, not a convention

## Considered Options

* Backend ingests PDFs, video and audio into datasets itself (rejected: puts probabilistic extraction behind the deterministic commit path and hides the agent's reading from the trust model)
* Harness in a separate repository depending on agent-backend as a package (deferred: the import boundary makes this split mechanical later; today it would slow the finding-to-requirement loop)
* Sibling package in the same repository with an enforced import boundary (chosen)

## Decision Outcome

The agent side becomes agent_harness/, a top-level package beside agent_backend/ in the same repository, excluded from the wheel. It holds the model client, a generic tool-calling loop over an MCP server, a perception package for readers of unstructured sources, and scenario packages, scenarios/dataspace/ first. The boundary: agent_harness imports from agent_backend only the public API (Backend, BackendError, ErrorCode) and agent_backend.mcp.server; agent_backend never imports agent_harness and never names a benchmark; a pytest test enforces both. The model sees one tool list, backend tools for structured data and harness tools for perception; whatever perception produces enters the backend only through import_dataset or attach_metadata, so it is a fresh agent output under MADR 0008 and carries provenance from the moment it is imported. The backend's contract stays structured data plus lineage; it does not read PDFs, video or audio.

## Consequences

* Scenario code, prompts, evaluators and measurements move from examples/ to agent_harness/scenarios/; examples/ keeps the backend's own demo
* Multimodal DataSpace tasks are addressed by adding readers under agent_harness/perception/ and registering them as tools beside the backend's, never by widening import_workspace
* The harness can later move to its own repository without touching agent_backend

## Decision History

<!-- driftseal-reconciliation: 974ac638-d2f6-446b-8418-f3c0cb276668 -->
### 2026-09-02T06:18:36.009Z — Outcome `2026-09-02-001`

Status: Accepted → Accepted

Implemented in outcome 2026-09-02-001: agent_harness/ with config, model, loop, perception and scenarios/dataspace; tests/test_boundary.py enforces the import rules, the absence of scenario names in the backend and the wheel exclusion; the scripted DataSpace runs pass 4/4 unchanged after the move; no backend module changed.

<!-- driftseal-reconciliation: eb14617b-7860-4782-8958-e2b93ae1a51f -->
### 2026-09-02T08:04:43.066Z — Outcome `2026-09-02-002`

Status: Accepted → Accepted

The split held under a new module on each side: advice (language and data facts) went into the backend, pacing (the preview-streak nudge) and the convergence metrics into the harness loop and runner. One harness fix on the way: the scripted layer now writes under runs/<task>/scripted so it never wipes the model-driven layer's runs/<task>/agent.

<!-- driftseal-reconciliation: 6c3fc3d4-bbe9-43b1-906d-891bd969312e -->
### 2026-09-02T09:08:36.969Z — Outcome `2026-09-02-005`

Status: Accepted → Accepted

Partly superseded by 0011: the harness no longer owns the model loop; OpenCode runs it with only the backend's MCP tools. The boundary itself holds (tests/test_boundary.py unchanged: the hosts package imports nothing from the backend), perception now comes from the host, and agent_harness/perception/ is reserved for an MCP-exposed reader only if the tool-restricted arm ever needs one.

### 2026-09-10T07:21:00.637Z, outcome 2026-09-10-0707-x3nb

Status: accepted -> accepted

The restricted host now needs a video reader. Outcome 2026-09-10-0707-x3nb adds deterministic FFmpeg frame tools and separately saved, revisable agent observations under agent_harness/perception; the backend and wheel boundary are unchanged. The first task_312 run passed its scorer but did not save or import an observation, so compliance with that boundary workflow remains a measured gap.

### 2026-09-10T07:55:13.157Z, outcome 2026-09-10-0735-sd88

Status: accepted -> accepted

Outcome 2026-09-10-0735-sd88 extends harness perception to offline speech transcription. FFmpeg extracts bounded, timestamp-aligned clips; a separately configured local Whisper worker supplies attributed transcript segments. The host imports the segment JSON through the public MCP surface, and frame or segment evidence can support explicitly revised observations. No backend logic or wheel dependency changed. User-supplied medium and tiny weights were copied and verified; the medium model drove the audio check.

### 2026-09-25T16:42:44.230Z, outcome 2026-09-25-1637-r4xp

Status: accepted -> accepted

import_dataset now takes rows written inline, a list of objects given with a name, in place of a path, so what the agent reads from a document, an image or a video, or a literal answer it holds, still enters the backend through import_dataset as this record requires. The rows are kept as a content-addressed JSON source in the workspace, registered as an artifact marked as inline rows, and imported through the same path as a file: type inference, temporal refinement, provenance, audit and replay. Set aside: a separate tool for rows, which would have been a third entry point beside import_dataset and attach_metadata; and letting a raw_query write out VALUES without reading its source, which MADR 0002 refuses so that every query result derives from a bound dataset. That refusal now points at rows written inline. Recorded in Intentum-DataSpace's GAPS.md as G4.
