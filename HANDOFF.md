# Handoff

Read these, in this order, before changing anything. Nothing else is needed
to start.

1. `AGENTS.md` — the DriftSeal protocol you must follow and the module map.
   `CLAUDE.md` is a symlink to it.
2. `README.md` — architecture. Sections 1 ("Output contracts", "Transform
   language", "Exporting an answer", "Failure semantics"), 2 (the two
   packages and the boundary between them) and 7 ("What to implement next")
   are the ones that matter.
3. `agent_harness/scenarios/dataspace/README.md` — the validation
   measurements, newest first. The 2026-08-31 sections say what was last
   changed on the backend, what it did, and where the model runs still fail;
   the 2026-08-30 section carries the champion comparison.

Then re-anchor:

```sh
driftseal status
driftseal log --last 3
```

- Outcome `2026-09-02-001` split the repository into two packages:
  `agent_backend/` (the backend) and `agent_harness/` (the agent side: model
  loop, framing, perception, scenarios), with the boundary enforced by
  `tests/test_boundary.py`. Nothing in the backend changed.
- Outcome `2026-08-31-001` is the last substantive backend change:
  post-aggregate projection, measureless grouping as distinct, and
  `semi_join`, measured on `task_44` afterwards (still 0/3; the gap is the
  model's reading and relationship choice).
- Outcome `2026-08-28-010` is where output contracts and the trust model
  came from; its closing note is still the best summary of the backend.

MADRs, in reading order (`.seal/madr/`):

- `0004` — what Intentum is: a general backend; benchmarks validate it, they
  do not define it.
- `0009` — the harness/backend boundary, and why perception of unstructured
  sources (PDF, video, audio) is the harness's job.
- `0008` — the trust model: fresh agent output is accepted, anything recalled
  from earlier turns is checked against the backend's records.
- `0007` — output contracts: the agent declares the shape of its deliverable,
  the backend holds the export to it.
- `0003` — the DataSpace sequence and, in its Decision History, what came
  next at each measurement.
- `0002` — the deferred `raw_query` fallback, and why the subquery shape the
  model reaches for is a semi-join, not that.

`0001`, `0005` and `0006` only matter if you touch commit semantics, export
rendering or import-time temporal refinement.

Settings for the harness live in a git-ignored `.env` at the repository root
(or the environment): `DEFAULT_MODEL_API_URL`, `DEFAULT_MODEL_API_KEY` and
`DEFAULT_MODEL_NAME` for the model gateway, `DATASPACE_BENCHMARK` for the
benchmark package and `KDDCUP_CHAMPION` for the champion repository. Run the
scenario from the repository root:

```sh
uv run python -m agent_harness.scenarios.dataspace.smoke --task all --check
uv run python -m agent_harness.scenarios.dataspace.agent --task task_44 --runs 3
```
