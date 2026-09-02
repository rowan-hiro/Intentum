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
   measurements, newest first. The 2026-09-02 sections say what the model
   runs looked like before and after the recovery module; the 2026-08-30
   section carries the champion comparison.

Then re-anchor:

```sh
driftseal status
driftseal log --last 3
```

- Outcome `2026-09-02-006` split the output contract in two (MADR 0012):
  `columns` is what the answer carries, `order_by` and the `one_per` keys are
  what it is organized by and may name columns the answer does not carry — the
  export sorts and counts by them and leaves them out of the file. `rows` is
  now required, and a declaration is answered with what the workspace holds
  under each of its names. Not yet measured: the prediction is that early
  declarations stop carrying the sort key.
- Outcome `2026-09-02-005` made OpenCode the agent host (`--host opencode`,
  MADR 0011): the model sees only the backend's MCP tools, and the event
  stream is normalized into the same run record as the in-process loop.
  Outcomes `-003` and `-004` measured the recovery module's fixes and the
  declaration timing (informed beats fresh; it is the default).
- Outcome `2026-09-02-002` added `agent_backend/core/recovery/`: every
  refusal now carries `advice` naming what the backend accepts, with the
  request rewritten as tool calls when mechanical; empty results and results
  that fit the declared contract are named too. The harness reports refusals
  without advice and the repair rate, and nudges on stalled previews. The
  DataSpace README's 2026-09-02 sections hold the before and after runs.
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
- `0011` — OpenCode as the agent host: what the harness keeps (scenario,
  measurement, adapters) and what the host owns (loop, pacing, perception).
- `0012` — why the contract separates the columns the answer carries from the
  columns it is organized by, and what was rejected on the way there.
- `0010` — teach on refusal: why refusals carry advice and rewrites instead of
  the resolver accepting every shape, and what convergence is measured by.
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
docker build -f agent_harness/hosts/opencode.Dockerfile --build-arg OPENCODE_VERSION=1.18.26 -t intentum-opencode:1.18.26 .
uv run python -m agent_harness.scenarios.dataspace.agent --task task_44 --runs 3              # OpenCode in the container (default)
uv run python -m agent_harness.scenarios.dataspace.agent --task task_44 --runs 3 --host loop  # in-process loop, the control arm
```

The OpenCode host needs Docker and the image above; rebuild the image after a
backend change, since the backend's MCP server is baked in. OpenCode is never
run on the machine itself: it folds any `AGENTS.md` above its working or config
directory into the system prompt, and this repository has one. Each run
writes its own `opencode.json`, `prompt.txt`, `events.jsonl` and `host.log`
beside the run's workspace.
