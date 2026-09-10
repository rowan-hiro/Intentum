# Handoff

Read these, in this order, before changing anything. Nothing else is needed
to start.

1. `AGENTS.md` — the Inkan protocol you must follow and the module map.
   `CLAUDE.md` is a symlink to it.
2. `README.md` — architecture. Sections 1 ("Output contracts", "Transform
   language", "Exporting an answer", "Failure semantics"), 2 (the two
   packages and the boundary between them) and 7 ("What to implement next")
   are the ones that matter.
3. `agent_harness/scenarios/dataspace/README.md` — the validation
   measurements, newest first. The 2026-09-10 sections carry the model
   first video-frame run on `task_312`, model rounds five to seven (three models on the closed advice gap, the gpt-5.6
   family, the Claude family with gpt-6-astra, and the two-family table that
   gathers eight models on the same columns) and the replay of the recorded
   refusals; the 2026-09-09 sections the MADR 0012 rounds and the first
   claude-opus-5 round; the 2026-09-02 sections what the runs looked like
   before and after the recovery module; the 2026-08-30 section the champion
   comparison.

Then re-anchor:

```sh
inkan status
inkan log -n 3
```

Outcomes under Inkan (`inkan log` carries them), newest first:

- `2026-09-10-0707-x3nb` adds an opt-in harness MCP video reader, FFmpeg in
  the OpenCode image, explicit image modalities, and a synthetic visual-code
  probe. Qwen correctly read the probe and passed one `task_312` run (242 rows,
  3 columns). The agent read six timestamped frames but skipped
  `record_observation` and the import of its reading. It also revised its
  output contract only after export failed, dropping an organizing key. The
  score therefore establishes this answer, not compliance with the observation
  or declaration-review instructions. No backend or task-specific extraction
  code changed. See the first measurement section for run paths and limits.
- The 2026-09-10 outcomes on the branch `refusal-diagnostics` closed the gap
  between MADR 0010 and the code. `2026-09-10-0415-n1n6`: advice is attached
  for every semantic operation at the operation boundary
  (`semantic_operation`, with the call's arguments as written), raise sites
  carry what is accepted in `details` with the step path kept in `field`, and
  fourteen detectors were added for the refusal families of the 2026-09-09
  runs; a replay of the recorded refusals is the check (every recorded
  refusal advised, every rewrite succeeding). `2026-09-10-0533-8p1h`:
  semantic operations are serialized on the backend's re-entrant lock, after
  a model that calls tools in parallel hit the shared SQLite connection; a
  derive with a name and nothing to compute is taught. `2026-09-10-0654-fm37`
  taught the two shapes the sixth round left bare and wrote this section.
  `2026-09-10-0441-cpj3` raised the Inkan protocol to version 8: a plan
  states the `inkan begin` text verbatim.
- The measurement rounds of 2026-09-10 (`-0444-7v8q`, `-0543-08cf`,
  `-0603-92qs`) put qwen3.5-35b-a3b, the gpt-5.6 tiers, gpt-6-astra and the
  Claude tiers on the same prompt and tooling. What they established: the
  backend's language is used equally well by every current model (eight to
  twelve steps, refusals advised and taken); the failure that remains is one
  reading made in the declaration step, the organizing key carried into the
  file, at every price; claude-opus-5 (18/18 across three rounds) and
  gpt-6-astra (6/6) did not fail it. The advice change alone cut qwen's
  refusals and steps on `task_44` by more than half. The next question is the
  harness's: what in the framing or the declaration exchange turns that
  reading, measured with more than three runs per cell.
- The 2026-09-09 outcomes measured MADR 0012 (`-0711-905x`, `-0748-cy16`,
  `-0834-7ms3`, with `-0738-rz35` answering a name given as both carried and
  organizing and `-0822-br58` letting agents review a declaration),
  recorded reversibility as README section 7 item 9 (`-0900-srhn`), and ran
  the first claude-opus-5 round (`-0952-yjvm`: 3/3 and 3/3 where qwen had
  3/3 and 0/3).

The outcomes named below closed under DriftSeal, before the repository moved
to Inkan; `inkan log` does not carry them. Read them in `.seal/HISTORY.md`,
which is the frozen rendering of that history.

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

MADRs, in reading order (`.inkan/decisions/`):

- `0004` — what Intentum is: a general backend; benchmarks validate it, they
  do not define it.
- `0011` — OpenCode as the agent host: what the harness keeps (scenario,
  measurement, adapters) and what the host owns (loop, pacing, perception).
- `0012` — why the contract separates the columns the answer carries from the
  columns it is organized by, and what was rejected on the way there.
- `0010` — teach on refusal: why refusals carry advice and rewrites instead of
  the resolver accepting every shape, and what convergence is measured by;
  its 2026-09-10 history entries add the two implementation rules (dispatch
  at the operation boundary, the accepted shape from the raise site).
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
