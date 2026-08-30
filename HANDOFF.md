# Handoff

Read these, in this order, before changing anything. Nothing else is needed
to start.

1. `AGENTS.md` — the DriftSeal protocol you must follow and the module map.
   `CLAUDE.md` is a symlink to it.
2. `README.md` — architecture. Sections 1 ("Output contracts", "Transform
   language", "Exporting an answer", "Failure semantics") and 7 ("What to
   implement next") are the ones that matter.
3. `examples/dataspace/README.md` — the validation measurements. The top
   section (2026-08-30) says what was last changed, what it did, where the
   runs still fail, how the champion pipeline compares, and what comes next.

Then re-anchor:

```sh
driftseal status
driftseal log --last 2
```

- Outcome `2026-08-28-010` is the substantive one: output contracts plus the
  vocabulary from the second measurement. Its closing note is the summary of
  the current state.
- Outcome `2026-08-30-001` only recorded the champion comparison and wrote
  this file.

MADRs, in reading order (`.seal/madr/`):

- `0004` — what Intentum is: a general backend; benchmarks validate it, they
  do not define it.
- `0008` — the trust model: fresh agent output is accepted, anything recalled
  from earlier turns is checked against the backend's records.
- `0007` — output contracts: the agent declares the shape of its deliverable,
  the backend holds the export to it.
- `0003` — the DataSpace sequence and, in its Decision History, the next
  steps in frequency order.
- `0002` — the deferred `raw_query` fallback, and why the subquery shape the
  model reaches for is a semi-join, not that.

`0001`, `0005` and `0006` only matter if you touch commit semantics, export
rendering or import-time temporal refinement.

The model configuration for `examples/dataspace_agent.py` is three
`DEFAULT_MODEL_*` variables in a git-ignored `.env` at the repository root;
the benchmark package is expected at `$DATASPACE_BENCHMARK` or
`$DATASPACE_BENCHMARK`.
