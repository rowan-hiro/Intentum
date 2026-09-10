# DataSpace smoke tests

Scripts for running Intentum against the [DataSpace](https://dataspace-bench.github.io)
benchmark (KDD Cup 2026 Data Agent track). This scenario lives in the agent
harness, beside the backend and apart from it (MADR 0009); it moved here from
`examples/` on 2026-09-02 with framing, scripts and scoring unchanged, so the
measurements below are comparable across the move.

- `evaluate.py` — the official evaluator, vendored unmodified from
  `HKUSTDial/DataSpace` (commit `6491caa`, MIT). Task Accuracy is binary per
  task: one one-to-one column mapping must make the whole predicted table equal
  to the gold table under the task's config (types, precision, row order).
  A prediction with more columns than the gold table fails outright.
- `smoke.py` — drives a task end to end through the MCP tool
  surface with a scripted agent (no LLM), writes
  `predictions/<task>/prediction.csv`, scores it with the official evaluator
  and (when the champion repository is available) with its local
  column-signature scorer, and records the full tool-call trace in
  `runs/<task>/scripted/smoke_result.json`; the model-driven layer writes
  under `runs/<task>/agent/` and neither layer touches the other's directory.
- `agent.py` — the same tool surface driven by a real model, through OpenCode in
  a container (`--host opencode`, the default, MADR 0011: the image from
  `../../hosts/opencode.Dockerfile`, only the `backend_*` MCP tools enabled) or
  through the in-process reference loop (`--host loop`); runs land under
  `runs/<task>/<host>-<declaration>/` with the host's event stream: a
  thin OpenAI-compatible agent loop with no SQL and no dialect rules in the
  prompt, scored the same way, with per-run traces and an aggregated summary.

The benchmark package itself (task inputs, 60 public `gold.csv` files and
their configs) is expected at `$DATASPACE_BENCHMARK`, and the champion
repository for the comparison scorer at `$KDDCUP_CHAMPION`; both are read from
the environment or the repository `.env`, like the model settings.

```sh
uv run python -m agent_harness.scenarios.dataspace.smoke --task all --check
uv run python -m agent_harness.scenarios.dataspace.agent --task task_127 --runs 3
```

## Tasks under measurement

Four public-reference tasks whose answers come from structured sources alone:

| task | question (abridged) | what it exercises |
|---|---|---|
| `task_10` | 货币当局资产负债表: reporting period and total assets where total assets is not null, oldest first | Chinese question against an English knowledge document; the question prescribes how numbers are rendered |
| `task_44` | procedures a patient received at the latest treatment timestamp, in treatment id order | the patient is only named in `cost`, so the answer needs a join and a second round on the maximum |
| `task_127` | maximum respiration for a patient on one day | json sources; a day filter on a timestamp that the source stores as text |
| `task_329` | daily maximum enteral formula volume for a patient | derive a day key, then aggregate twice through two managed datasets |

## Current prompt conditions · declaration review added 2026-09-09

After the rounds below, the shared MCP instructions, `declare_output` tool
description and both declaration framings gained a general review step: read
the declaration response before dependent calls, compare its consequences
with the original request, and either retain it or amend with a reason for a
changed requirement or corrected interpretation. Amendment guidance also
allows correcting an earlier misreading; a dataset mismatch alone does not
justify changing the declaration. The new wording names no task or data field
and does not prescribe adding or removing columns.

This changes the prompt conditions for both hosts. The third round below is
the first measurement of this addition; earlier rounds used the earlier
wording. A broader measurement should include cases where the declaration
should be retained as well as corrected, across different deliverable shapes;
an increased amendment rate alone would not establish improvement.

## 2026-09-10, seventh round · the Claude family, fable-5.1, opus-5 and sonnet-5, with gpt-6-astra, and the two families side by side

**Official verdicts on `task_44` and `task_329`: claude-fable-5.1 3/3 and 2/3,
claude-opus-5 3/3 and 3/3, claude-sonnet-5 0/3 and 2/3, gpt-6-astra 3/3 and
3/3.** Two models now pass both tasks every time: opus-5, which is at 9/9 and
9/9 across its three rounds, and gpt-6-astra on its first. Neither is the top
of its family by price. fable-5.1 carried the day key into the file once on
`task_329`, the same reading failure as every other miss on that task; sol
did so twice in the sixth round. sonnet-5 declared `treatmentid` beside
`treatmentname` in all three `task_44` runs and spent 19 to 26 steps getting
there, most of them exploring the workspace (search, describe, the artifact
list, a document described as a dataset, an already imported SQLite table
imported again), each refusal advised and none of them the cause of the
verdict.

Conditions: source `c3205af`, the sixth round's image
(`sha256:1f9414f1b4ca0b218d5eab02a68f021ef8c24db6ad5b230f51a40c998f1bb7c6`, the
two commits since changed only this README), OpenCode 1.18.26, informed
declaration framing, three sequential runs per task per model with the
runner's defaults and a 900 s timeout, through the same gateway with only
`DEFAULT_MODEL_NAME` changed. Cost is computed from the recorded tokens at
the vendors' official prices as published on their pricing pages on this day,
which the gateway lists unchanged: fable-5.1 $10 / $50 per million input /
output with cache reads at $0.25, opus-5 $5 / $25 with $0.50, sonnet-5 $2 /
$10 with $0.20, gpt-6-astra $10 / $50 with $1.00; reasoning billed as output.
The round comes to about $3.87: fable $1.32, opus $0.96, sonnet $0.79, astra
$0.80.

| model | task | official | mean steps | mean calls | mean prompt tokens (incl. cache) | refusals | unadvised | repair rate | mean elapsed | cost per run | cost per pass |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-6-astra | `task_44` | 3/3 | 10.0 | 9.0 | 60k | 5 | 0 | 0.60 | 42.9 s | $0.134 | $0.134 |
| gpt-6-astra | `task_329` | 3/3 | 9.7 | 8.7 | 59k | 3 | 0 | 1.00 | 36.8 s | $0.134 | $0.134 |
| claude-sonnet-5 | `task_44` | 0/3 | 23.0 | 28.3 | 438k | 8 | 0 | 0.12 | 160.6 s | $0.211 | no pass |
| claude-sonnet-5 | `task_329` | 2/3 | 7.7 | 7.7 | 96k | 0 | 0 | n/a | 38.2 s | $0.053 | $0.079 |
| claude-opus-5 | `task_44` | 3/3 | 10.3 | 10.7 | 134k | 2 | 0 | 0.00 | 56.5 s | $0.186 | $0.186 |
| claude-opus-5 | `task_329` | 3/3 | 7.7 | 7.7 | 94k | 0 | 0 | n/a | 38.4 s | $0.133 | $0.133 |
| claude-fable-5.1 | `task_44` | 3/3 | 11.0 | 10.0 | 138k | 1 | 0 | 1.00 | 77.7 s | $0.245 | $0.245 |
| claude-fable-5.1 | `task_329` | 2/3 | 8.3 | 8.0 | 99k | 0 | 0 | n/a | 67.3 s | $0.195 | $0.292 |

Per run. Every declaration stayed at revision 1; no run amended.

| model | task | run | official | declared columns | declaration step | steps / calls | refusals (unadvised) | repaired | prompt tokens (uncached + cache read) | output tokens | elapsed | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-6-astra | `task_44` | 1 | passed | `treatmentname` | 7 | 10 / 9 | 2 (0) | 1/2 | 8,461 + 53,676 | 478 | 45.0 s | $0.162 |
| gpt-6-astra | `task_44` | 2 | passed | `treatmentname` | 6 | 9 / 8 | 1 (0) | 1/1 | 3,775 + 49,361 | 445 | 40.1 s | $0.109 |
| gpt-6-astra | `task_44` | 3 | passed | `treatmentname` | 8 | 11 / 10 | 2 (0) | 1/2 | 4,140 + 61,918 | 555 | 43.7 s | $0.131 |
| gpt-6-astra | `task_329` | 1 | passed | `daily_maximum_amount_ml` | 7 | 9 / 8 | 1 (0) | 1/1 | 7,694 + 46,150 | 443 | 37.9 s | $0.145 |
| gpt-6-astra | `task_329` | 2 | passed | `maximum_amount` | 7 | 10 / 9 | 1 (0) | 1/1 | 4,206 + 57,570 | 545 | 35.3 s | $0.127 |
| gpt-6-astra | `task_329` | 3 | passed | `maximum_enteral_formula_volume_bolus_amt_ml` | 7 | 10 / 9 | 1 (0) | 1/1 | 4,375 + 57,791 | 575 | 37.2 s | $0.130 |
| claude-sonnet-5 | `task_44` | 1 | failed: extra column | `treatmentid, treatmentname` | 23 | 26 / 36 | 3 (0) | 0/3 | 31,009 + 521,032 | 10,433 | 190.2 s | $0.271 |
| claude-sonnet-5 | `task_44` | 2 | failed: extra column | `treatmentid, treatmentname` | 21 | 24 / 28 | 2 (0) | 0/2 | 18,421 + 432,029 | 9,143 | 172.0 s | $0.215 |
| claude-sonnet-5 | `task_44` | 3 | failed: extra column | `treatmentid, treatmentname` | 16 | 19 / 21 | 3 (0) | 1/3 | 14,227 + 298,263 | 5,998 | 119.5 s | $0.148 |
| claude-sonnet-5 | `task_329` | 1 | passed | `max_amount` | 4 | 7 / 7 | 0 (0) | 0/0 | 15,531 + 72,465 | 1,563 | 37.9 s | $0.061 |
| claude-sonnet-5 | `task_329` | 2 | failed: extra column | `day, max_amount` | 4 | 7 / 7 | 0 (0) | 0/0 | 7,114 + 80,805 | 1,395 | 33.5 s | $0.044 |
| claude-sonnet-5 | `task_329` | 3 | passed | `max_amount_ml` | 6 | 9 / 9 | 0 (0) | 0/0 | 7,435 + 104,520 | 1,758 | 43.1 s | $0.053 |
| claude-opus-5 | `task_44` | 1 | passed | `treatmentname` | 8 | 11 / 11 | 1 (0) | 0/1 | 16,894 + 124,017 | 2,751 | 58.8 s | $0.215 |
| claude-opus-5 | `task_44` | 2 | passed | `treatmentname` | 8 | 11 / 12 | 1 (0) | 0/1 | 8,114 + 132,344 | 3,107 | 63.0 s | $0.184 |
| claude-opus-5 | `task_44` | 3 | passed | `treatmentname` | 6 | 9 / 9 | 0 (0) | 0/0 | 8,661 + 111,761 | 2,317 | 47.7 s | $0.157 |
| claude-opus-5 | `task_329` | 1 | passed | `max_volume_ml` | 6 | 9 / 9 | 0 (0) | 0/0 | 15,777 + 95,384 | 1,965 | 45.7 s | $0.176 |
| claude-opus-5 | `task_329` | 2 | passed | `max_volume` | 4 | 7 / 7 | 0 (0) | 0/0 | 7,165 + 80,094 | 1,528 | 34.5 s | $0.114 |
| claude-opus-5 | `task_329` | 3 | passed | `max_volume_ml` | 4 | 7 / 7 | 0 (0) | 0/0 | 6,485 + 77,200 | 1,552 | 34.9 s | $0.110 |
| claude-fable-5.1 | `task_44` | 1 | passed | `treatmentname` | 7 | 10 / 9 | 1 (0) | 1/1 | 15,508 + 103,866 | 2,059 | 72.1 s | $0.284 |
| claude-fable-5.1 | `task_44` | 2 | passed | `treatmentname` | 9 | 12 / 11 | 0 (0) | 0/0 | 9,097 + 148,312 | 2,172 | 84.7 s | $0.237 |
| claude-fable-5.1 | `task_44` | 3 | passed | `treatmentname` | 8 | 11 / 10 | 0 (0) | 0/0 | 7,668 + 128,661 | 2,112 | 76.3 s | $0.214 |
| claude-fable-5.1 | `task_329` | 1 | passed | `max_volume_ml` | 5 | 8 / 8 | 0 (0) | 0/0 | 14,997 + 80,873 | 1,560 | 61.4 s | $0.248 |
| claude-fable-5.1 | `task_329` | 2 | failed: extra column | `day, max_enteral_formula_volume_ml` | 5 | 8 / 8 | 0 (0) | 0/0 | 7,058 + 89,604 | 1,522 | 65.6 s | $0.169 |
| claude-fable-5.1 | `task_329` | 3 | passed | `max_volume_ml` | 6 | 9 / 8 | 0 (0) | 0/0 | 6,393 + 98,643 | 1,581 | 74.9 s | $0.168 |

What the refusals were, and what followed:

- gpt-6-astra opened every run of both tasks by attaching
  `/data/input/<task>/context/knowledge.md`, a document that does not exist;
  the refusal named the documents that do (`document_not_found`, with the
  rewrite to `doc/microlab.md` or `doc/patient.md`), and the next call was
  that rewrite, six times out of six. On `task_44` it also sent
  `doc/patient.md` to `import_dataset` twice and moved on. It then declared
  the value alone with the day under `one_per` and `order_by` on every
  `task_329` run, in one call, at step 7. Its output tokens are the smallest
  of any model measured, about 500 per run.
- sonnet-5 on `task_44` met eight refusals across three runs, all advised:
  `document_as_dataset` four times (a document described or transformed as
  a dataset), `file_as_dataset` three times (the workspace's SQLite file
  imported again as a table), `declaration_rows` once (`rows: 4`, corrected
  to `one_per`). The verdicts do not turn on them: all three runs reached the
  right rows at the end and declared two columns.
- fable-5.1 met one refusal in six runs, `join_scope_names` on `task_44`,
  and its next call succeeded. opus-5 met its usual two, `doc/patient.md`
  given to `import_dataset`, and went on without the document.
- Nothing in this round was refused without advice.

The two families side by side. The rows below gather the sixth and seventh
rounds (source `c3205af`) with qwen's fifth-round row (`d7a7eb5`, which lacks
only the operation lock and one detector that never fired for it), all on the
same prompt and tooling, priced at each vendor's official rates.

| model | list price, input / output per M | `task_44` | `task_329` | mean steps 44 / 329 | cost per run 44 / 329 | cost per pass 44 / 329 |
|---|---|---|---|---|---|---|
| qwen3.5-35b-a3b | $0.16 / $1.30 | 2/3 | 0/3 | 19.0 / 14.3 | $0.061 / $0.037 | $0.092 / no pass |
| gpt-5.6-luna | $0.20 / $1.20 | 2/3 | 3/3 | 9.7 / 9.7 | $0.005 / $0.004 | $0.007 / $0.004 |
| gpt-5.6-terra | $2 / $12 | 3/3 | 2/3 | 10.3 / 8.7 | $0.044 / $0.038 | $0.044 / $0.057 |
| gpt-5.6-sol | $4 / $20 | 3/3 | 1/3 | 9.7 / 9.3 | $0.096 / $0.096 | $0.096 / $0.288 |
| gpt-6-astra | $10 / $50 | 3/3 | 3/3 | 10.0 / 9.7 | $0.134 / $0.134 | $0.134 / $0.134 |
| claude-sonnet-5 | $2 / $10 | 0/3 | 2/3 | 23.0 / 7.7 | $0.211 / $0.053 | no pass / $0.079 |
| claude-opus-5 | $5 / $25 | 3/3 | 3/3 | 10.3 / 7.7 | $0.186 / $0.133 | $0.186 / $0.133 |
| claude-fable-5.1 | $10 / $50 | 3/3 | 2/3 | 11.0 / 8.3 | $0.245 / $0.195 | $0.245 / $0.292 |

Four readings of that table, each within what 48 runs on two tasks allow:

- **The language is not where the families differ.** Every model but qwen
  and sonnet-on-`task_44` finished in eight to twelve steps with at most a
  handful of refusals, all advised and mostly taken on the next call. The
  detectors written from qwen's refusals were taken by sol, fable, astra and
  luna, models that never produced the shapes they came from.
- **The reading of the deliverable is where they differ, and it does not
  follow price.** Carrying the organizing key into the file, on `task_329`,
  happened to qwen, luna (fifth round), terra, sol, sonnet and fable, and
  not to opus or astra; on `task_44` the extra `treatmentid` happened to
  qwen, luna and sonnet. Within each family the top tier did not read it
  better than the tiers below: sol 1/3 against terra 2/3 and luna 3/3, fable
  2/3 against opus 3/3.
- **Steps, not price, set the cost.** The two models that pass everything
  cost $0.13 and $0.13 to $0.19 per pass; sonnet's `task_44` runs, at a fifth
  of opus's price per token, cost more per run than opus's because they took
  twice the steps and three times the tokens. luna, at a fiftieth of astra's
  price, is the cheapest pass on both tasks when it passes.
- **Two models are, on this sample, reliable: claude-opus-5 and
  gpt-6-astra.** opus at 18 of 18 across three rounds, astra at 6 of 6 on
  one; astra takes fewer tokens per step and, at twice the list price, costs
  less per run on `task_44` and the same on `task_329`. Everything else passed `task_329` between one
  and three times in three.

Raw runs are retained under `runs/<task>/opencode-informed-<model>-20260910-1403/`
for `claude-fable-5.1`, `claude-opus-5`, `claude-sonnet-5` and `gpt-6-astra`,
each with `agent_summary.json` and three run directories. Earlier rounds'
directories are untouched. Commands used, on the sixth round's image:

```sh
for MODEL in anthropic/claude-fable-5.1 anthropic/claude-opus-5 anthropic/claude-sonnet-5 openai/gpt-6-astra; do
  for TASK in task_44 task_329; do
    DEFAULT_MODEL_NAME=$MODEL PYTHONUNBUFFERED=1 .venv/bin/python -m agent_harness.scenarios.dataspace.agent --task $TASK --runs 3 \
      --out agent_harness/scenarios/dataspace/runs/$TASK/opencode-informed-${MODEL#*/}-20260910-1403
  done
done
```

Three runs per cell and two tasks cannot rank eight models, and a 2/3
against a 3/3 is one run. What the sample establishes: on these two tasks
the backend's language is used equally well by every current model of both
families, the failures that remain are readings made in one declaration
step, the same reading fails at every price, and two models did not fail it.
The next question is still the harness's: what in the framing or the
declaration exchange turns that reading, measured on the models that fail
it, with more than three runs per cell.

## 2026-09-10, sixth round · the gpt-5.6 family, sol, terra and luna, on `c3205af`

**Official verdicts on `task_44` and `task_329`: gpt-5.6-sol 3/3 and 1/3,
gpt-5.6-terra 3/3 and 2/3, gpt-5.6-luna 2/3 and 3/3.** The three tiers move
alike through the tool surface: eight to twelve steps, eight to fourteen calls,
almost no language refusals, and prompt tokens between about 55k and 110k per run.
What separates them is not the language but the price and, on `task_329`, a
reading that goes one way or the other on each run: every failed `task_329`
run at every tier declared the day key as a carried column beside the value,
and every passing run declared the value alone with the key under `one_per`
and `order_by`. Three runs per cell cannot rank the tiers on that reading;
across the family it went right in six runs of nine. On `task_44` the only
miss is luna's third run, which exported the treatments of another timestamp
(five rows against the gold's four) after declaring the right column.

Conditions: source `c3205af`, image `intentum-opencode:1.18.26` rebuilt before
the round, ID `sha256:1f9414f1b4ca0b218d5eab02a68f021ef8c24db6ad5b230f51a40c998f1bb7c6`,
OpenCode 1.18.26, informed declaration framing, three sequential runs per task
per tier with the runner's defaults and a 900 s timeout, through the same Kilo
gateway and provider block with only `DEFAULT_MODEL_NAME` changed. Since the
fifth round the backend gained the operation lock and the
`derive_without_expression` detector (`c3205af`); prompt, framing, tool
descriptions and runner are unchanged. Cost is computed from the recorded
tokens at OpenAI's official standard prices as published on its pricing page
on this day, which the gateway lists unchanged: sol $4 / $20 per million input
/ output with cached input at $0.40, terra $2 / $12 with $0.20, luna $0.20 /
$1.20 with $0.02; reasoning billed as output; every run stayed under the
long-context threshold. The whole round comes to about $0.85: sol $0.58, terra
$0.25, luna $0.03. The fifth round's rows are repeated for comparison.

| round | task | official | mean steps | mean calls | mean prompt tokens (incl. cache) | refusals | unadvised | repair rate | mean elapsed | cost per run | cost per pass |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-5.6-sol, this round | `task_44` | 3/3 | 9.7 | 11.0 | 83k | 5 | 1 | 1.00 | 49.8 s | $0.096 | $0.096 |
| gpt-5.6-terra, this round | `task_44` | 3/3 | 10.3 | 11.3 | 80k | 0 | 0 | n/a | 40.2 s | $0.044 | $0.044 |
| gpt-5.6-luna, this round | `task_44` | 2/3 | 9.7 | 12.3 | 82k | 0 | 0 | n/a | 40.3 s | $0.005 | $0.007 |
| gpt-5.6-sol, this round | `task_329` | 1/3 | 9.3 | 8.7 | 87k | 0 | 0 | n/a | 45.3 s | $0.096 | $0.288 |
| gpt-5.6-terra, this round | `task_329` | 2/3 | 8.7 | 9.3 | 68k | 1 | 1 | 1.00 | 34.0 s | $0.038 | $0.057 |
| gpt-5.6-luna, this round | `task_329` | 3/3 | 9.7 | 10.0 | 68k | 1 | 0 | 1.00 | 37.5 s | $0.004 | $0.004 |
| gpt-5.6-luna, fifth round (`d7a7eb5`) | `task_44` | 2/3 | 11.3 | 13.7 | 96k | 0 | 0 | n/a | 45.6 s | $0.006 | $0.008 |
| gpt-5.6-luna, fifth round (`d7a7eb5`) | `task_329` | 0/3 | 9.7 | 10.0 | 68k | 1 | 1 | 1.00 | 35.5 s | $0.004 | no pass |
| claude-opus-5, fifth round (`d7a7eb5`) | `task_44` | 3/3 | 11.0 | 11.0 | 143k | 3 | 0 | 0.00 | 61.8 s | $0.201 | $0.201 |
| claude-opus-5, fifth round (`d7a7eb5`) | `task_329` | 3/3 | 7.3 | 7.3 | 89k | 0 | 0 | n/a | 39.7 s | $0.132 | $0.132 |
| qwen3.5-35b-a3b, fifth round (`d7a7eb5`) | `task_44` | 2/3 | 19.0 | 18.0 | 330k | 5 | 1 | 1.00 | 76.6 s | $0.061 | $0.092 |
| qwen3.5-35b-a3b, fifth round (`d7a7eb5`) | `task_329` | 0/3 | 14.3 | 13.3 | 204k | 2 | 0 | 0.50 | 49.9 s | $0.037 | no pass |

Per run. Every declaration stayed at revision 1; no run amended.

| tier | task | run | official | declared columns | declaration step | steps / calls | refusals (unadvised) | repaired | prompt tokens (uncached + cache read) | output tokens | elapsed | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sol | `task_44` | 1 | passed | `treatmentname` | 8 | 11 / 11 | 2 (0) | 2/2 | 11,813 + 76,663 | 1,392 | 54.6 s | $0.106 |
| sol | `task_44` | 2 | passed | `treatmentname` | 7 | 10 / 12 | 2 (1) | 2/2 | 12,051 + 84,531 | 1,401 | 55.9 s | $0.110 |
| sol | `task_44` | 3 | passed | `procedure` | 5 | 8 / 10 | 1 (0) | 1/1 | 7,394 + 57,322 | 959 | 38.8 s | $0.072 |
| sol | `task_329` | 1 | failed: extra column | `date, daily_maximum_enteral_formula_volume_bolus_amt_ml` | 6 | 9 / 8 | 0 (0) | 0/0 | 10,252 + 56,972 | 814 | 41.9 s | $0.080 |
| sol | `task_329` | 2 | passed | `daily_maximum_amount_ml` | 6 | 9 / 8 | 0 (0) | 0/0 | 10,527 + 72,305 | 1,099 | 47.5 s | $0.093 |
| sol | `task_329` | 3 | failed: extra column | `date, daily_maximum_amount_of_enteral_formula_volume_bolus_amt_ml` | 5 | 10 / 10 | 0 (0) | 0/0 | 12,704 + 98,833 | 1,229 | 46.4 s | $0.115 |
| terra | `task_44` | 1 | passed | `treatmentname` | 8 | 11 / 12 | 0 (0) | 0/0 | 10,276 + 72,458 | 1,143 | 41.1 s | $0.049 |
| terra | `task_44` | 2 | passed | `treatmentname` | 7 | 10 / 11 | 0 (0) | 0/0 | 7,091 + 73,510 | 1,172 | 40.5 s | $0.043 |
| terra | `task_44` | 3 | passed | `treatmentname` | 7 | 10 / 11 | 0 (0) | 0/0 | 6,352 + 69,320 | 1,194 | 39.0 s | $0.041 |
| terra | `task_329` | 1 | failed: extra column | `date, maximum_enteral_formula_volume_bolus_amt_ml` | 5 | 8 / 9 | 0 (0) | 0/0 | 9,587 + 49,211 | 865 | 32.1 s | $0.039 |
| terra | `task_329` | 2 | passed | `maximum_enteral_formula_volume_bolus_amt_ml` | 6 | 10 / 10 | 1 (1) | 1/1 | 6,871 + 74,391 | 1,045 | 39.3 s | $0.041 |
| terra | `task_329` | 3 | passed | `daily_maximum_enteral_formula_volume_bolus_amt_ml` | 5 | 8 / 9 | 0 (0) | 0/0 | 6,294 + 56,526 | 860 | 30.6 s | $0.034 |
| luna | `task_44` | 1 | passed | `treatmentname` | 6 | 10 / 11 | 0 (0) | 0/0 | 10,668 + 69,538 | 1,459 | 41.6 s | $0.005 |
| luna | `task_44` | 2 | passed | `treatmentname` | 6 | 9 / 12 | 0 (0) | 0/0 | 8,280 + 66,466 | 1,516 | 39.8 s | $0.005 |
| luna | `task_44` | 3 | failed: wrong rows | `treatmentname` | 6 | 10 / 14 | 0 (0) | 0/0 | 8,489 + 83,243 | 1,250 | 39.4 s | $0.005 |
| luna | `task_329` | 1 | passed | `max_enteral_formula_volume_bolus_amt_ml` | 5 | 8 / 9 | 0 (0) | 0/0 | 9,502 + 49,383 | 1,017 | 33.2 s | $0.004 |
| luna | `task_329` | 2 | passed | `daily_max_enteral_formula_volume_bolus_amt_ml` | 6 | 9 / 8 | 0 (0) | 0/0 | 4,415 + 51,687 | 834 | 32.5 s | $0.003 |
| luna | `task_329` | 3 | passed | `daily_max_amount_ml` | 8 | 12 / 13 | 1 (0) | 1/1 | 6,134 + 82,692 | 1,254 | 46.9 s | $0.004 |

What the refusals were, and what followed:

- sol is the one tier that reached for a join on `task_44`, in every run:
  `cost` filtered to the patient, joined to `treatment` on `eventid` to
  `treatmentid`, then `treatmentid` selected. Each time the join refused with
  `join_scope_names` (the right key is not copied after a join) and the very
  next call used the advised shape and succeeded; run 1 also met
  `join_on_as_mapping` first and took that. This is the detector written for
  qwen's four duplicate-field refusals of the third round, taken by a
  different model on the first try, three times out of three.
- luna's `task_329` run 3 wrote `uniquepid contains '033-22108'`, was refused
  with `function_as_infix`, and its next call was the rewrite verbatim,
  `contains(uniquepid, '033-22108')`.
- Two refusals carried no advice, both new shapes. terra's `task_329` run 2
  emitted stray non-Latin characters after a closing quote
  (`... amt (ml)'ીલ`), a generation glitch; the parser refused with
  "Unexpected character" and nothing else, and the model corrected itself on
  the next call. sol's `task_44` run 2 wrote `lower(eventtype) contains
  'treat'`: the infix detector matches a bare field on the left, not a call,
  so this one was refused bare; the model went another way and passed. Both
  are detector work for a follow-up: strip a trailing non-token run when the
  expression parses without it, and accept a call on the left of an infix
  function.
- No run of any tier met `field_not_in_scope`, `measure_needs_field`,
  `aggregate_body_misplaced`, `compare_literal_type` or `document_as_dataset`:
  the shapes qwen and opus produced did not occur here.

The `task_329` reading. Every failure at every tier is the same one the
earlier rounds recorded for qwen and for luna's fifth round: the day key named
under `columns` as well as under `one_per` and `order_by`, the both-lists
sentence returned, the declaration kept. The passing runs at every tier named
the value alone. luna went 0/3 on the fifth round and 3/3 here with the same
prompt, framing and tool descriptions; the backend changed only by the
operation lock and a detector that never fired for it. Nothing in the records
explains the swing, and three runs cannot: at a per-run rate near one half,
either extreme comes up one time in eight. The honest number for luna on
`task_329` is three passes in six runs across the two rounds, and for the
family this round six in nine. Which way a run goes is decided at the
declaration, in one step, before any refusal; it is the harness's question,
not the language's.

Cost. At OpenAI's prices a pass on `task_44` costs $0.096 with sol, $0.044
with terra and $0.007 with luna; on `task_329`, $0.288, $0.057 and $0.004.
Against the fifth round, opus at $0.201 and $0.132 per pass remains the only
model that passed `task_329` every time, and qwen at $0.092 per `task_44` pass
never passed `task_329`. Within the family sol and terra cost twenty and ten
times luna's price per token and took the same number of steps, so the
price shows up as a twentyfold and tenfold cost per run with no pass to show
for it on this sample. What a stronger tier bought here was a wider reach for
the language (sol's joins), not a better reading of the deliverable.

Raw runs are retained under `runs/<task>/opencode-informed-gpt-5.6-<tier>-20260910-1343/`
for `sol`, `terra` and `luna`, each with `agent_summary.json` and three run
directories. Earlier rounds' directories are untouched. Commands used, after
rebuilding the image:

```sh
docker build -f agent_harness/hosts/opencode.Dockerfile --build-arg OPENCODE_VERSION=1.18.26 -t intentum-opencode:1.18.26 .
for TIER in sol terra luna; do
  for TASK in task_44 task_329; do
    DEFAULT_MODEL_NAME=openai/gpt-5.6-$TIER PYTHONUNBUFFERED=1 .venv/bin/python -m agent_harness.scenarios.dataspace.agent --task $TASK --runs 3 \
      --out agent_harness/scenarios/dataspace/runs/$TASK/opencode-informed-gpt-5.6-$TIER-20260910-1343
  done
done
```

Three runs per cell and two tasks cannot rank three tiers of one family, and
a 1/3 against a 3/3 on `task_329` is within what a coin decides. What the
round establishes: the three tiers use the backend's language equally well
and equally cheaply in steps; the price difference between them is not
returned as passes on these two tasks; the new detectors are taken by models
that never saw the refusals they were written from; and the one failure that
matters, carrying the organizing key into the file, occurs at every tier and
at every price. The next question is the harness's: what in the framing or
the declaration exchange turns that reading, measured on more than three
runs per cell.

## 2026-09-10, fifth round · three models on the source that closed the MADR 0010 gap (`d7a7eb5`)

**Official verdicts: `qwen/qwen3.5-35b-a3b` 2/3 and 0/3, `anthropic/claude-opus-5`
3/3 and 3/3, `openai/gpt-5.6-luna` 2/3 and 0/3, on `task_44` and `task_329`.**
For qwen and opus the only change since their rounds on `eed47b7` is the
backend's refusal advice (the third and fourth rounds below); the prompt,
framing, tool descriptions and runner are the same. On `task_44` qwen's
refusals fell from thirteen to five and its unadvised refusals from eight to
one, with steps down from 26.3 to 19.0 and prompt tokens from 548k to 330k;
its one lost verdict is a declaration that carried `treatmentid` beside
`treatmentname`, a reading, not a refusal. On `task_329` qwen's refusals fell
from four to two and unadvised from four to none, with steps unchanged and the
verdict unchanged: every run again declared the day key as a carried column.
Opus is within noise of its fourth round on both tasks; its only refusals are
the three `import_dataset` calls on `doc/patient.md`, now advised. gpt-5.6-luna,
a first round, moves like opus and reads like qwen: about eleven and ten
steps, no language refusals at all, and the same two failure shapes as qwen,
`treatmentid` carried once on `task_44` and the day key carried on every
`task_329` run, at a thirtieth of opus's cost per run.

Conditions: source `d7a7eb5`, image `intentum-opencode:1.18.26` rebuilt before
the round, ID `sha256:ed49111a98657ac43d131acbcedcf00c25d09f6f59cd2bb1c9de794df72d7223`,
OpenCode 1.18.26, informed declaration framing, three sequential runs per task
per model with the runner's defaults and a 900 s timeout, all through the same
Kilo gateway and provider block with only `DEFAULT_MODEL_NAME` changed. Cost is
computed from the recorded tokens at the gateway's list prices on this day
(qwen $0.1625 / $1.30 per million input / output; opus $5 / $25 with cache reads
at $0.50; gpt-5.6-luna $0.20 / $1.20 with cache reads at $0.02), reasoning
billed as output; OpenCode's own estimate is 0 for an unpriced provider. The
third and fourth rounds are re-priced the same way here so the columns compare.
The whole round comes to about $1.32: qwen $0.29, opus $1.00, gpt-5.6-luna $0.03.

| round | task | official | mean steps | mean calls | mean prompt tokens (incl. cache) | refusals | unadvised | repair rate | mean elapsed | cost per run | cost per pass |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen, third round (`eed47b7`) | `task_44` | 3/3 | 26.3 | 25.3 | 548k | 13 | 8 | 0.46 | 106.7 s | $0.099 | $0.099 |
| qwen, this round (`d7a7eb5`) | `task_44` | 2/3 | 19.0 | 18.0 | 330k | 5 | 1 | 1.00 | 76.6 s | $0.061 | $0.092 |
| qwen, third round (`eed47b7`) | `task_329` | 0/3 | 14.3 | 13.7 | 205k | 4 | 4 | 0.75 | 51.1 s | $0.038 | no pass |
| qwen, this round (`d7a7eb5`) | `task_329` | 0/3 | 14.3 | 13.3 | 204k | 2 | 0 | 0.50 | 49.9 s | $0.037 | no pass |
| opus, fourth round (`eed47b7`) | `task_44` | 3/3 | 10.0 | 10.0 | 127k | 2 | 2 | 0.00 | 57.6 s | $0.184 | $0.184 |
| opus, this round (`d7a7eb5`) | `task_44` | 3/3 | 11.0 | 11.0 | 143k | 3 | 0 | 0.00 | 61.8 s | $0.201 | $0.201 |
| opus, fourth round (`eed47b7`) | `task_329` | 3/3 | 8.0 | 7.7 | 96k | 0 | 0 | n/a | 42.0 s | $0.136 | $0.136 |
| opus, this round (`d7a7eb5`) | `task_329` | 3/3 | 7.3 | 7.3 | 89k | 0 | 0 | n/a | 39.7 s | $0.132 | $0.132 |
| gpt-5.6-luna, this round (`d7a7eb5`) | `task_44` | 2/3 | 11.3 | 13.7 | 96k | 0 | 0 | n/a | 45.6 s | $0.006 | $0.008 |
| gpt-5.6-luna, this round (`d7a7eb5`) | `task_329` | 0/3 | 9.7 | 10.0 | 68k | 1 | 1 | 1.00 | 35.5 s | $0.004 | no pass |

Per run. Every declaration succeeded on its first successful call and stayed
at revision 1; no run amended.

| model | task | run | official | declared columns | declaration step | steps / calls | refusals (unadvised) | repaired | prompt tokens (uncached + cache read) | output tokens | elapsed | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen | `task_44` | 1 | passed | `treatmentname` | 13 | 20 / 19 | 2 (1) | 2/2 | 372,870 + 0 | 7,746 | 91.5 s | $0.071 |
| qwen | `task_44` | 2 | passed | `treatmentname` | 13 | 18 / 17 | 1 (0) | 1/1 | 325,236 + 0 | 5,022 | 73.3 s | $0.059 |
| qwen | `task_44` | 3 | failed: extra column | `treatmentid, treatmentname` | 16 | 19 / 18 | 2 (0) | 2/2 | 292,425 + 0 | 4,296 | 65.0 s | $0.053 |
| qwen | `task_329` | 1 | failed: extra column | `day, max_enteral_formula_ml` | 8 | 16 / 15 | 0 (0) | 0/0 | 258,947 + 0 | 3,119 | 51.2 s | $0.046 |
| qwen | `task_329` | 2 | failed: extra column | `date, daily_maximum_ml` | 7 | 15 / 14 | 2 (0) | 1/2 | 186,106 + 0 | 2,884 | 48.2 s | $0.034 |
| qwen | `task_329` | 3 | failed: extra column | `date, max_bolus_amt` | 9 | 12 / 11 | 0 (0) | 0/0 | 167,557 + 0 | 2,902 | 50.4 s | $0.031 |
| opus | `task_44` | 1 | passed | `treatmentname` | 9 | 12 / 11 | 1 (0) | 0/1 | 15,920 + 129,340 | 3,338 | 66.1 s | $0.228 |
| opus | `task_44` | 2 | passed | `treatmentname` | 7 | 10 / 11 | 1 (0) | 0/1 | 9,535 + 128,838 | 2,961 | 56.6 s | $0.186 |
| opus | `task_44` | 3 | passed | `treatmentname` | 8 | 11 / 11 | 1 (0) | 0/1 | 8,797 + 135,843 | 3,051 | 62.6 s | $0.188 |
| opus | `task_329` | 1 | passed | `max_volume_ml` | 4 | 7 / 7 | 0 (0) | 0/0 | 14,783 + 68,673 | 1,903 | 40.0 s | $0.156 |
| opus | `task_329` | 2 | passed | `max_volume_ml` | 4 | 7 / 7 | 0 (0) | 0/0 | 6,544 + 77,295 | 1,610 | 36.7 s | $0.112 |
| opus | `task_329` | 3 | passed | `max_volume_ml` | 5 | 8 / 8 | 0 (0) | 0/0 | 7,130 + 91,375 | 1,906 | 42.5 s | $0.129 |
| gpt-5.6-luna | `task_44` | 1 | failed: extra column | `treatmentid (integer), treatmentname (string)` | 8 | 11 / 12 | 0 (0) | 0/0 | 10,648 + 77,145 | 1,630 | 47.4 s | $0.006 |
| gpt-5.6-luna | `task_44` | 2 | passed | `treatmentname` | 8 | 12 / 14 | 0 (0) | 0/0 | 7,467 + 93,210 | 1,561 | 45.2 s | $0.005 |
| gpt-5.6-luna | `task_44` | 3 | passed | `treatmentname` | 8 | 11 / 15 | 0 (0) | 0/0 | 9,184 + 91,240 | 1,653 | 44.2 s | $0.006 |
| gpt-5.6-luna | `task_329` | 1 | failed: extra column | `date (date), daily_max_amount_ml (float)` | 6 | 10 / 10 | 0 (0) | 0/0 | 8,696 + 57,364 | 1,037 | 35.8 s | $0.004 |
| gpt-5.6-luna | `task_329` | 2 | failed: extra column | `day (date), daily_max_enteral_formula_volume_bolus_amt_ml (float)` | 6 | 9 / 8 | 0 (0) | 0/0 | 4,510 + 51,948 | 916 | 34.6 s | $0.003 |
| gpt-5.6-luna | `task_329` | 3 | failed: extra column | `day (date), daily_max_enteral_formula_volume_bolus_amt_ml (float)` | 5 | 10 / 12 | 1 (1) | 1/1 | 6,819 + 73,676 | 984 | 36.1 s | $0.004 |

What the refusals were, and what happened after them:

- qwen `task_44` met five refusals across three runs, all advised but one:
  `field_not_in_scope` twice and `declaration_rows` twice. The one unadvised
  refusal is a new shape: `{"derive": "max_time"}`, a derive with a name and
  no expression, in a list that then filtered `treatmenttime = max_time`. The
  model wanted a maximum across rows, which is an aggregate, not a derived
  value; the refusal shows the derive shape but nothing else. Recorded as the
  residual of this round.
- qwen `task_329`: two refusals in run 2, `declaration_rows` and then an
  export onto the existing prediction file, now advised as `file_exists`; the
  run took the rewrite and exported. All three declarations named the day key
  in `columns` and `order_by`, received the both-lists sentence, and stayed.
- opus `task_44`: each run sent `doc/patient.md` to `import_dataset`, was
  refused with `document_as_dataset` and the `attach_metadata` rewrite, and
  went on with transforms without calling it: the advice was heard, the
  document was not needed, and no step was lost to it. The harness's repair
  rate reads 0.0 here because it counts a later success of the *same* tool;
  a rewrite that names another tool is not a repair by that measure. That is
  a limit of the metric, not of the advice.
- gpt-5.6-luna sent no request the language refused in its six runs. Its one
  refusal is not a refusal: in `task_329`
  run 3 a `describe_dataset` returned `INTERNAL`,
  `InterfaceError('bad parameter or other API misuse')` from SQLite. The
  model issues several tool calls in one step (up to four; every one of its
  runs has such steps), the MCP server runs the handlers concurrently, and
  the backend shares one SQLite connection opened with `check_same_thread`
  off and no lock around it. This is the first model in the measurements
  that calls tools in parallel, and it exposes a concurrency defect in the
  server, not in the language; root README §7 item 7 names multi-writer
  deployments, and this is the single-process case. The run recovered on
  its own (the same call succeeded a step later).
- gpt-5.6-luna on `task_44` filtered `treatment` on a wrong stay id
  (`2079061`) in every run; the empty preview came back with
  `value_not_found` naming where that value occurs, and runs 2 and 3 then
  found the right stay and passed. Its `task_329` declarations carried the
  day key in every run, exactly as qwen's; the both-lists sentence was
  returned each time and none amended.

Cost. At the gateway's list prices a pass on `task_44` costs $0.092 with qwen,
$0.201 with opus and $0.008 with gpt-5.6-luna; on `task_329` only opus passes,
at $0.132. For qwen, closing the advice gap cut cost per run on `task_44` by
about 38% (fewer steps, and each step is a prefill); for opus it changed
nothing measurable, because opus met almost no refusals to begin with. The
cheap strong-family model is a tenth of qwen's cost per pass on the task it
can do and takes six tenths of qwen's steps, but it fails the same way qwen does
on the task it cannot: the organizing key carried into the file. The line
between the models that pass `task_329` and those that do not runs through
the reading of the deliverable, not through the transform language or the
price.

Raw runs are retained under `runs/<task>/opencode-informed-<model>-20260910-1245/`
for `qwen3.5-35b-a3b`, `claude-opus-5` and `gpt-5.6-luna`, each with
`agent_summary.json` and three run directories. Earlier rounds' directories
are untouched. Commands used, after rebuilding the image:

```sh
docker build -f agent_harness/hosts/opencode.Dockerfile --build-arg OPENCODE_VERSION=1.18.26 -t intentum-opencode:1.18.26 .
for MODEL in qwen/qwen3.5-35b-a3b anthropic/claude-opus-5 openai/gpt-5.6-luna; do
  for TASK in task_44 task_329; do
    DEFAULT_MODEL_NAME=$MODEL PYTHONUNBUFFERED=1 .venv/bin/python -m agent_harness.scenarios.dataspace.agent --task $TASK --runs 3 \
      --out agent_harness/scenarios/dataspace/runs/$TASK/opencode-informed-${MODEL#*/}-20260910-1245
  done
done
```

Three runs per cell are too few to read a 3/3 against a 2/3 as a change, and
two tasks cannot say how the three models rank elsewhere. What this round
establishes: with the same prompt, the advice change removed most of qwen's
refusals and about a third of its steps on the task where it had them, and
changed nothing for the model that had none; a cheap model of the strong
family behaves like the strong one on the language and like the weak one on
the reading; and parallel tool calls, which that model makes, break the
server's single shared SQLite connection. Two things follow for the backend
before the next round: serialize tool execution in the MCP server (or give
each call its own connection), and teach the derive-without-expression
shape. The reading of the deliverable, the one failure shared by qwen and
gpt-5.6-luna on `task_329`, remains the harness's question.

## 2026-09-10 · replay of the 21 unadvised refusals after the MADR 0010 gap was closed

A replay, not a measurement: no model ran. Every tool call of the 2026-09-09
run records (qwen second and third rounds, claude-opus-5 fourth round) was
re-sent in its recorded order through the MCP server to a fresh backend with
each task's workspace imported, container paths mapped to the benchmark copy
and a temporary run directory. For each call the record marked as a refusal,
the replay reports whether the refusal now carries `advice` and, for every
rewrite offered, whether the rewrite succeeds when sent back on a copy of the
workspace at that point.

**All 21 refusals that reached the model without advice now carry it, and
all 18 rewrites offered on them succeed as sent.** The three refusals that are explained without a
rewrite are the ones the design names as not mechanical: a text literal that
does not read as an integer (`patientunitstayid = '025-44842'`), a field that
the joined side does not have (`patientunitstayid` on `cost`, whose look-alike
`patienthealthsystemstayid` is a different identifier), and a transform sent
as malformed JSON.

| round | run | step | tool | refusal as recorded | advice now | rewrite |
|---|---|---|---|---|---|---|
| qwen r2 | `task_329` 2 | 7 | transform_dataset | `celllabel contains 'enteral'` (parse error) | `function_as_infix` | success |
| qwen r2 | `task_329` 2 | 11 | transform_dataset | aggregate body under `group_by` (invalid derive entry) | `aggregate_body_misplaced` | success |
| qwen r2 | `task_329` 2 | 12 | transform_dataset | `max(cellvaluenumeric) as max_amount` as a metric (needs_resolution) | `measure_as_object` | success |
| qwen r2 | `task_329` 3 | 5 | transform_dataset | `treatmentname contains 'enteral' or ...` (parse error) | `function_as_infix` | success |
| qwen r2 | `task_44` 1 | 3 | transform_dataset | integer compared with `'025-44842'` | `compare_literal_type` | explained: the literal does not read as an integer |
| qwen r2 | `task_44` 1 | 12 | transform_dataset | `patientunitstayid` absent after semi_join from `cost` | `field_not_in_scope` | explained: the scope, and the look-alike named as another field |
| qwen r3 | `task_329` 1 | 7 | materialize_result | `day` used by an aggregate listed before its derive | `field_created_later` | success (one self-ordering object) |
| qwen r3 | `task_329` 2 | 11 | transform_dataset | `date` used before its derive | `field_created_later` | success |
| qwen r3 | `task_329` 3 | 3 | attach_metadata | `knowledge.md` while `doc/microlab.md` existed | `document_not_found` | success |
| qwen r3 | `task_329` 3 | 16 | export_result | prediction file already exists | `file_exists` | success |
| qwen r3 | `task_44` 2 | 15 | transform_dataset | `max` of `*` | `measure_needs_field` | success (`count`) |
| qwen r3 | `task_44` 2 | 16 | transform_dataset | a join object as `source` (needs_resolution) | `source_as_dataset` | success (the join as the first step, the right key selected from the left one) |
| qwen r3 | `task_44` 2 | 17 | transform_dataset | transform as malformed JSON text (SDK pydantic text) | `transform_as_text` | explained: where the text stops being JSON |
| qwen r3 | `task_44` 2 | 19, 21, 23, 25 | transform_dataset | duplicate `treatmenttime` ×4 (`treatmentid` fuzzy-matched after the join) | `join_scope_names` | success ×4 (`eventid as treatmentid`) |
| qwen r3 | `task_44` 2 | 20 | transform_dataset | `treatment.treatmenttime as treatmenttime` | `join_scope_names` | success (unqualified) |
| qwen r3 | `task_44` 2 | 27 | transform_dataset | `max` of `*` | `measure_needs_field` | success |
| opus r4 | `task_44` 1, 3 | 2, 3 | import_dataset | `doc/patient.md` given to import_dataset | `document_as_dataset` | success ×2 (`attach_metadata`; the explanation says prose records are extracted outside the backend) |

The ten refusals that already carried advice in the records replayed with the
same kinds; their rewrites now show what the earlier rounds could not: three
of them lead to a second advised refusal (`expression_as_derive` then
`aggregate_body_misplaced`; `join_on_as_mapping` then `join_scope_names`;
`subquery_as_semi_join` then `field_not_in_scope`), and one materialization
name collides only because the replay had already materialized it, a
conflict that now carries `name_taken`. Two
refusals changed status: an aggregate call in a `metric` slot and a JSON
object as `source` are refused as `error` with the accepted shape instead of
`needs_resolution` with candidates, since neither is a name to disambiguate.

What the replay establishes: on the recorded requests, dispatch covers every
operation, each family has a detector, and the rewrites are executable. What
it cannot say is whether a model takes the advice, or how often these shapes
recur on other tasks; that is the next model-driven round's question, on the
same two tasks first, comparing steps and refusals per pass with the third
and fourth rounds.

## 2026-09-09, fourth round · `anthropic/claude-opus-5` through the same host on the same source (`eed47b7`)

**With the model changed from `qwen/qwen3.5-35b-a3b` to `anthropic/claude-opus-5`
and nothing else, the official verdicts were 3/3 on `task_44` and 3/3 on
`task_329`.** The `task_329` passes are the first by the model-driven layer on
that task. Every `task_329` run declared `day` under `one_per` and `order_by`
and only the value column under `columns`, the shape MADR 0012 was built for
and which no qwen round produced; each exported file carried one row and one
column. Every `task_44` run declared `treatmentname` carried and `treatmentid`
organizing, materialized both columns and exported one. All six declarations
succeeded on their first call and stayed at revision 1; no run amended. Steps
and tool calls fell to about a third of the qwen round on `task_44` and to
about half on `task_329`; refusals fell from thirteen to two and from four to
none.

Conditions: source `eed47b7` for everything the container runs (the two commits
since, `66f9a0f` and `4588056`, changed READMEs only), the same image
`intentum-opencode:1.18.26` with the ID recorded in the third round
(`sha256:191e4f56…`), OpenCode 1.18.26, informed declaration framing, three
sequential runs per task with the runner's defaults and a 900 s timeout. The
model reached the container through the same Kilo gateway and the same
OpenAI-compatible provider block; only `DEFAULT_MODEL_NAME` differed, set in the
environment of the two commands below. No backend, prompt, runner, host or
evaluator code was changed. The third round, qwen on the same source, is the
control arm.

| task | model | official | mean steps | mean calls | mean prompt tokens (uncached + cache reads) | refusals | unadvised | mean elapsed |
|---|---|---|---|---|---|---|---|---|
| `task_44` | `qwen/qwen3.5-35b-a3b` (third round) | 3/3 | 26.3 | 25.3 | 548k | 13 | 8 | 106.7 s |
| `task_44` | `anthropic/claude-opus-5` | 3/3 | 10.0 | 10.0 | 127k | 2 | 2 | 57.6 s |
| `task_329` | `qwen/qwen3.5-35b-a3b` (third round) | 0/3 | 14.3 | 13.7 | 205k | 4 | 4 | 51.1 s |
| `task_329` | `anthropic/claude-opus-5` | 3/3 | 8.0 | 7.7 | 96k | 0 | 0 | 42.0 s |

Per run. The declared columns are both initial and final.

| task | run | official | declared columns | `rows` | `order_by` | declaration step | steps / calls | refusals (unadvised) | prompt tokens (uncached + cache read) | elapsed |
|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | passed | `treatmentname` | `at_least_one` | `treatmentid` | 5 | 8 / 8 | 1 (1) | 15,044 + 81,777 | 43.5 s |
| `task_44` | 2 | passed | `treatmentname` | `at_least_one` | `treatmentid` | 6 | 9 / 10 | 0 (0) | 8,207 + 108,776 | 51.8 s |
| `task_44` | 3 | passed | `treatmentname` | `at_least_one` | `treatmentid` | 10 | 13 / 12 | 1 (1) | 9,283 + 158,876 | 77.6 s |
| `task_329` | 1 | passed | `max_volume_ml` | `one_per: [day]` | `day` | 4 | 7 / 7 | 0 (0) | 14,789 + 68,505 | 35.0 s |
| `task_329` | 2 | passed | `max_enteral_formula_volume_bolus_amt_ml (float)` | `one_per: [day]` | `day` | 5 | 8 / 7 | 0 (0) | 6,634 + 87,173 | 43.7 s |
| `task_329` | 3 | passed | `max_enteral_formula_volume_ml` | `one_per: [day]` | `day` | 6 | 9 / 9 | 0 (0) | 7,399 + 102,866 | 47.3 s |

What the runs did:

- `task_44`: every run imported the workspace, filtered `cost` to the patient,
  took the unit stay id from it, sorted `treatment` for that stay by time
  descending to find the latest timestamp, declared, materialized the rows at
  that timestamp with `treatmentid` and `treatmentname` selected and sorted by
  `treatmentid`, and exported. Runs 2 and 3 also read `doc/patient.md` through
  `attach_metadata`; run 2 additionally looked the treatment ids up from `cost`
  before switching to the stay id.
- The two refusals are the same call: runs 1 and 3 passed `doc/patient.md` to
  `import_dataset` and were refused with `INVALID_SCHEMA`, "Unsupported format
  'md'; supported formats are csv, parquet, json, sqlite", and no advice. Run 3
  then called `attach_metadata` on the artifact; run 1 went on without the
  document. Both recovered without help, but under MADR 0010 the refusal should
  have named the tool that accepts a markdown document. The qwen rounds never
  exposed this because qwen never sent a document to `import_dataset`. Stated
  generally: a refusal for an unsupported format should name the tool that
  accepts that format when one exists.
- `task_329`: every run filtered `patient` to the patient, took the unit stay of
  the current encounter, looked at the `intakeoutput` cell labels for that stay
  (two runs searched for `bolus`, one grouped all labels), then materialized one
  aggregate step: filter to the exact label, `max(cellvaluenumeric)` grouped by
  a day expression written inline in `group_by` (`date_trunc('day', …) as day`
  or `date(…) as day`), sorted by `day`. The contract left `day` out of the file.
  Two runs exported with `decimals: 1` and `strip_trailing_zeros`, one with
  `strip_trailing_zeros` alone; all three files read `60`. The qwen rounds
  derived the day key in a separate step and carried it into the file.

Token accounting differs from the earlier rounds. The gateway reports uncached
input separately from cache reads for this model, and the qwen rounds recorded
no cache reads, so the comparable figure is the sum, given above. The first
step of the first run on each task was 8,199 input tokens; the first step of
every later run read 8,197 of them from cache, so the runs of a round are not
independent in cost, though caching does not change what the model sees. The
first-step count was identical across the two tasks, which the qwen round's
were not (5,546 against 5,553); how the gateway counts for this model is not
established here and the counts are recorded as reported. OpenCode's cost
estimate is 0 for an unpriced custom provider; at Anthropic's list prices the
six runs come to about a dollar, and the gateway's own price is not recorded.

All six runs ended normally, passed the backend integrity check and had
official evaluator return code 0. The optional champion scorer reported 0.0 on
every run for the reason noted in the third round (a relative prediction path
resolved under its own working directory); it was not re-run.

Raw runs are retained under
[`task_44/opencode-informed-claude-opus-5-20260909-1752`](runs/task_44/opencode-informed-claude-opus-5-20260909-1752/)
and
[`task_329/opencode-informed-claude-opus-5-20260909-1752`](runs/task_329/opencode-informed-claude-opus-5-20260909-1752/),
each with `agent_summary.json` and three run directories holding the event
stream, prompt, prediction and evaluator result. Earlier rounds' directories
are untouched. Commands used:

```sh
DEFAULT_MODEL_NAME=anthropic/claude-opus-5 PYTHONUNBUFFERED=1 .venv/bin/python -m agent_harness.scenarios.dataspace.agent --task task_44 --runs 3 --out agent_harness/scenarios/dataspace/runs/task_44/opencode-informed-claude-opus-5-20260909-1752
DEFAULT_MODEL_NAME=anthropic/claude-opus-5 PYTHONUNBUFFERED=1 .venv/bin/python -m agent_harness.scenarios.dataspace.agent --task task_329 --runs 3 --out agent_harness/scenarios/dataspace/runs/task_329/opencode-informed-claude-opus-5-20260909-1752
```

A model change alters the tokenizer, the tool-calling style and the reading of
the question at once, so six runs cannot say which part of the backend the
stronger model used better, and two already studied tasks cannot say how it
would fare on the rest of the benchmark. What this round establishes is
narrower: the tool surface, prompt and contract that qwen ran against, with no
change, let a stronger model pass both tasks in a third of the steps, and the
organizing-key shape MADR 0012 provides was used as designed on every
`task_329` run. The qwen result stands as the control arm on the same source.
The with-and-without-backend comparison on one host, named as next in the
root README, is still the experiment that would attribute the passes to the
backend rather than to the model.

## 2026-09-09, third round · after declaration-response review (`eed47b7`)

**The official verdicts stayed at 3/3 for `task_44` and 0/3 for `task_329`;
none of the six runs amended a successful declaration.** The initial carried
columns already matched the reference shape in every `task_44` run. Every
`task_329` run declared and exported an extra date column, including two runs
whose declaration response explained the consequence of carrying the sort key.
The new review instructions did not produce an observable correction in this
sample. This does not establish whether the model internally reconsidered a
declaration before retaining it.

Conditions: source `eed47b7`, model `qwen/qwen3.5-35b-a3b`, OpenCode 1.18.26
in the container, informed declaration framing, three sequential runs per task
with the runner's defaults and a 900 s timeout. The image was rebuilt before
the runs; its recorded ID is
`sha256:191e4f563e6978f906e52fa8cc7071f3c83bc9683de13ae3d903e14fc6f05443`.
First-step input tokens were 5,546 on every `task_44` run and 5,553 on every
`task_329` run, against about 5.2k in the preceding round. The new instructions
are available before the first declaration as well as when its response is
read, so this comparison cannot isolate the effect of reviewing the response.
No backend, prompt, runner or evaluator code was changed during this round.

| task | round | official | amendments | mean steps | mean prompt tokens | refusals | unadvised | repair rate | mean elapsed |
|---|---|---|---|---|---|---|---|---|---|
| `task_44` | preceding round (`2ec5175`) | 3/3 | 0 | 19.7 | 326k | 7 | 2 | 1.0 | 72 s |
| `task_44` | declaration review (`eed47b7`) | 3/3 | 0 | 26.3 | 548k | 13 | 8 | 0.462 | 106.7 s |
| `task_329` | preceding round (`2ec5175`) | 0/3 | 0 | 15.3 | 230k | 5 | 3 | 0.80 | 51 s |
| `task_329` | declaration review (`eed47b7`) | 0/3 | 0 | 14.3 | 205k | 4 | 4 | 0.75 | 51.1 s |

Per run. All six successful declarations used `rows: at_least_one`; none used
`one_per`. Each stayed at revision 1, with no amendment or amendment reason.
The columns below are both the first successfully declared and the final
declared columns. A refused declaration followed by a corrected first
declaration is counted as refusal repair, not as an amendment.

| task | run | official | declared columns (initial = final) | `order_by` | declaration step | steps / calls | refusals (unadvised) | repaired | prompt tokens | elapsed |
|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | passed | `treatmentname` | `treatmentid asc` | 15 | 20 / 19 | 1 (0) | 1/1 | 381,277 | 83.6 s |
| `task_44` | 2 | passed | `treatmentname` | `eventid asc` | 30 | 38 / 37 | 11 (8) | 4/11 | 936,781 | 168.7 s |
| `task_44` | 3 | passed | `treatmentname` | `treatmentid asc` | 18 | 21 / 20 | 1 (0) | 1/1 | 324,896 | 67.9 s |
| `task_329` | 1 | failed: extra column | `day (string), max_amount_ml (float)` | `day asc` | 5 | 10 / 10 | 1 (1) | 1/1 | 125,675 | 39.0 s |
| `task_329` | 2 | failed: extra column | `date, max_ml` | `date asc` | 10 | 15 / 14 | 1 (1) | 1/1 | 226,045 | 57.8 s |
| `task_329` | 3 | failed: extra column | `date, max_amount_ml` | none | 12 | 18 / 17 | 2 (2) | 1/2 | 264,399 | 56.4 s |

The declaration response and the actions that followed it:

- `task_44` never named a key in both lists. Every response described one
  carried column and a separate sort key. Runs 1 and 3 first tried `rows: 4`,
  received `declaration_rows`, and then made their first successful declaration;
  that is failure-side recovery. After declaration, each run continued with
  transforms or materialization and exported the correct four-row, one-column
  file. Run 2 accounts for most of the extra work: all eleven of its refusals
  occurred before declaration, largely while expressing the join and projection.
- `task_329` runs 1 and 2 named `day` or `date` in both `columns` and `order_by`.
  Their responses contained `carried: true`, `organizing: [order_by]` and the
  sentence explaining that the column would be written, whereas naming it only
  in `order_by` would sort without writing it. Run 1 declared at step 5, then
  previewed at 6, attempted materialization at 7, materialized at 8 and exported
  at 9. Run 2 declared at 10, attempted a transform at 11, materialized through
  a transform at 12 and exported at 13. Neither amended the declaration.
- `task_329` run 3 used neither organizing field, so the both-lists sentence
  did not fire. Its declaration summary still said that the file would carry
  `date` and `max_amount_ml`. It declared at 12, previewed at 13, materialized
  at 14 and exported at 15. A second export at 16 was refused because the file
  existed; the agent inspected the operation at 17 and ended. All three files
  had one row and two columns against the reference's one row and one column.

Every first dependent call above occurred in a later model step than the
successful declaration response. The trace therefore contains a response
boundary at which reconsideration was possible. It contains no visible
explanation of retaining or revising a declaration: the only text events after
successful declarations are `DONE`. The records show unchanged declarations,
not whether the model read, ignored or agreed with the feedback. No run
attempted an amendment, so the revised amendment guidance was not exercised
through an amendment call.

All six runs ended normally and passed the backend integrity check; no run
reached the timeout, and all official evaluator processes returned 0. The
optional champion scorer initially reported `prediction missing`: the relative
`--out` path was interpreted under its own working directory. Those original
records are retained. Re-running only that local auxiliary scorer with absolute
prediction paths produced 1.0 on all `task_44` runs and 0.95 on all `task_329`
runs (one matching value column plus one extra column), saved separately as
`champion_absolute_path_check.json` in each run. Official results and model
runs were not replaced or repeated.

Raw runs and extracted declaration records are retained under
[`task_44/opencode-informed-20260909-0834`](runs/task_44/opencode-informed-20260909-0834/)
and [`task_329/opencode-informed-20260909-0834`](runs/task_329/opencode-informed-20260909-0834/).
Each directory contains `agent_summary.json`, `declaration_review.json` and
three run directories with the event stream, prompt, prediction and evaluator
result. These are new directories; the previous round's raw runs are preserved.
Commands used, after rebuilding the image:

```sh
PYTHONUNBUFFERED=1 .venv/bin/python -m agent_harness.scenarios.dataspace.agent --task task_44 --runs 3 --out agent_harness/scenarios/dataspace/runs/task_44/opencode-informed-20260909-0834
PYTHONUNBUFFERED=1 .venv/bin/python -m agent_harness.scenarios.dataspace.agent --task task_329 --runs 3 --out agent_harness/scenarios/dataspace/runs/task_329/opencode-informed-20260909-0834
```

Six runs on two already studied tasks are insufficient to establish either
generalization or a causal effect. The two both-lists exposures are also too
few to estimate a correction rate. What this round establishes is narrower:
the general review guidance was present, the existing declaration feedback was
returned, and none of these runs revised its successful declaration. The
correct initial declarations stayed correct; the incorrect ones stayed
incorrect. Broader validation still needs both retention and correction cases
with other deliverable shapes.

## 2026-09-09, second round · after `declare_output` answered a name given as both carried and organizing (`2ec5175`)

The round below found the model reading `columns` and `order_by`/`one_per` as
additive: four of five failing runs named the same key in both lists and
carried it anyway. `2ec5175` answers that declaration with what it means rather
than judging it — every declared name now reports which organizing field named
it, and a name given as both carried and organizing is answered with the
consequence: the file will carry it, and naming it only as organizing would
order or set the grain of the answer without writing it. Both readings stay
legal and nothing is refused or reshaped. `framing.py` and the MCP tool
descriptions were deliberately left alone, so this round moves one variable
against the round below.

Same host and framing, the runner's defaults, the image rebuilt from the
current backend. First-step prompt tokens are 5.2k in both rounds, which
confirms the prompt did not move. Six container runs, all ended by themselves;
none reached the 900 s timeout and none failed for an infrastructure reason.

| task | round | official | declarations with the gold shape | declaration steps | mean steps | mean prompt tokens | first-step tokens | refusals | unadvised | repair rate | mean elapsed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | baseline, before 0012 | 2/3 | 2/3 | 33, 16, 19 | 25.7 | 483k | 5.0k | 7 | 4 | 0.57 | 93 s |
| `task_44` | after 0012 | 1/3 | 1/3 | 13, 21, 37 | 29.0 | 635k | 5.2k | 15 | 11 | 0.60 | 120 s |
| `task_44` | after the both-lists fact | 3/3 | 3/3 | 17, 13, 13 | 19.7 | 326k | 5.2k | 7 | 2 | 1.0 | 72 s |
| `task_329` | baseline, before 0012 | 0/3 | 0/3 | 11, 9, 11 | 14.3 | 218k | 5.0k | 4 | 0 | 1.0 | 56 s |
| `task_329` | after 0012 | 0/3 | 0/3 | 15, 13, 10 | 16.7 | 254k | 5.2k | 6 | 3 | 1.0 | 58 s |
| `task_329` | after the both-lists fact | 0/3 | 0/3 | 8, 14, 13 | 15.3 | 230k | 5.2k | 5 | 3 | 0.80 | 51 s |

Per run. `key in both lists` is the condition the new fact fires on, and is
recorded beside the organizing keys because it decides whether this round's
change had anything to say at all.

| task | run | official | declared columns | `order_by` / `one_per` | key in both lists | declaration step | steps | tool calls | refusals (advised) | advice taken up | prompt tokens | stop |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | **passed** | `treatmentname` | `order_by: treatmentid asc`; `rows: at_least_one`, no `one_per` | no | 17 | 21 | 20 | 5 (3): `subquery_as_semi_join` ×2; `declaration_rows` (`rows` given as `4`); unadvised: a type mismatch, a missing name | 5/5 | 339k | done |
| `task_44` | 2 | **passed** | `treatmentname` | `order_by: treatmentid`; `rows: at_least_one`, no `one_per` | no | 13 | 18 | 17 | 1 (1): `declaration_rows` (`rows` given as `4`) | 1/1 | 354k | done |
| `task_44` | 3 | **passed** | `treatmentname` | `order_by: treatmentid asc`; `rows: at_least_one`, no `one_per` | no | 13 | 20 | 19 | 1 (1): `unknown_key` on a transform | 1/1 | 286k | done |
| `task_329` | 1 | failed (extra column) | `date, max_value` | neither; `rows: at_least_one` | no | 8 | 12 | 13 | 0 | — | 177k | done |
| `task_329` | 2 | failed (extra column) | `day, max_amount` | neither; `rows: at_least_one` | no | 14 | 17 | 16 | 4 (1): `expression_as_derive`; unadvised: two invalid transforms, an ambiguous reference | 3/4 | 248k | done |
| `task_329` | 3 | failed (extra column) | `day, max_volume_ml` | `order_by: day asc`; `rows: at_least_one`, no `one_per` | **yes** (`day`) | 13 | 17 | 16 | 1 (0): an invalid transform | 1/1 | 266k | done |

**Whether the both-lists fact changed the declared shape cannot be told from
this round.** The fact fires only when a name is given in both lists. Across
these six runs that happened once — `task_329` run 3, where `day` is declared
as a carried column and as `order_by` — and the raw event stream confirms the
sentence reached the model. It did not amend the declaration, and the run
failed the same way as every other `task_329` run on record. One occurrence is
not a measurement of anything.

`task_44` went 3/3, and all three runs declared `treatmentname` alone with
`order_by: treatmentid` — the shape 0012 predicted, on the first declaration
each time. **The new fact is not the cause.** No `task_44` declaration named a
key twice, so the fact had nothing to say in any of those runs; the
declarations arrived gold-shaped before anything new could be read. The other
half of `2ec5175`, the `organizing` field on every name, does fire on these
runs, but it arrives in the answer to a declaration already made, and no run
amended one. So the movement on `task_44` is run-to-run variation under an
unchanged prompt, not this change taking effect.

Read across the rounds, that is what the numbers look like. `task_44`
gold-shape declarations went 2/3 before 0012, 1/3 after it, 3/3 now, with the
framing text constant since `79f311f`. Three-run samples spread that way on
their own. The one honest summary is that `task_44` has produced six passes in
nine recorded runs and that the per-round order carries no signal.

`task_329` is the steady half and the more interesting one: 0/9 across the
three rounds, and the day key sits in `columns` in every one of those nine
declarations. This round it moved backwards on shape. Two of the three runs
named no organizing field at all — `rows: at_least_one` and both columns
carried, which is the pre-0012 shape — where the previous round had used
`one_per` once and `order_by` once. `one_per` was not used at all. Whatever is
keeping the grain key in the payload on this task, three rounds of backend
work have not touched it, and the field built for it is going unused.

Cost moved on `task_44` and not on `task_329`. `task_44` is cheaper and
shorter than the previous round on every count — 635k to 326k prompt tokens,
29.0 to 19.7 steps, 120 s to 72 s, refusals 15 to 7 — and essentially all of
that is the absence of the previous round's 41-step run rather than anything
systematic. The champion column-signature scorer gives 1.0 on all three
`task_44` runs against 0.95, 0.95, 1.0 before, and 0.95 three times on
`task_329` as in every round. `task_329` is flat: 254k to 230k, 16.7 to 15.3
steps, 58 s to 51 s.

Six runs, three per task. That is too few to separate a change from noise on
the verdict, which is why the declaration shape is tabled apart from it — and
on shape this round the change had one chance to speak and was not taken up.
The next round wanting evidence about the both-lists fact has to reach runs
where a key is named twice, and on the current numbers that condition appears
about once in six.

## 2026-09-09 · after the contract separated payload from organizing keys (MADR 0012)

`declare_output` now names two kinds of column (MADR 0012, landed in `79f311f`).
`columns` is what the file carries, in order; `order_by` and the `one_per` keys
of `rows` are what the answer is organized by — they may name columns the file
does not carry, the backend sorts and counts by them, and it leaves them out of
the file. `rows` became required. The prediction recorded in 0012 was that
early declarations should stop carrying the sort key. This is the first
measurement since that change.

Same conditions as the OpenCode container arm below: OpenCode 1.18.26 in a
freshly built image from `agent_harness/hosts/opencode.Dockerfile`,
`--declaration informed`, the same model and gateway, the runner's defaults
untouched. `framing.py`, the MCP tool descriptions and the backend are
unchanged by this round; the framing has carried the 0012 wording since
`79f311f` and was not strengthened for this measurement. Six container runs,
all ended by themselves; none reached the 900 s timeout, and none failed for an
infrastructure reason. First-step prompt tokens are 5.2k against the baseline's
5.0k, which is the 0012 sentence in the framing.

The baseline is the OpenCode container arm of 2026-09-02 below, which predates
`79f311f`.

| task | round | official | declarations with the gold shape | declaration steps | mean steps | mean prompt tokens | first-step tokens | refusals | unadvised | repair rate | mean elapsed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | baseline, before 0012 | 2/3 | 2/3 | 33, 16, 19 | 25.7 | 483k | 5.0k | 7 | 4 | 0.57 | 93 s |
| `task_44` | this round, after 0012 | 1/3 | 1/3 | 13, 21, 37 | 29.0 | 635k | 5.2k | 15 | 11 | 0.60 | 120 s |
| `task_329` | baseline, before 0012 | 0/3 | 0/3 | 11, 9, 11 | 14.3 | 218k | 5.0k | 4 | 0 | 1.0 | 56 s |
| `task_329` | this round, after 0012 | 0/3 | 0/3 | 15, 13, 10 | 16.7 | 254k | 5.2k | 6 | 3 | 1.0 | 58 s |

Per run. The `order_by` / `one_per` column is new and is the point of this
round: whether the run used the organizing fields at all, and which keys it
named there. It is recorded apart from the verdict on purpose, because a
verdict can stay put for reasons that have nothing to do with 0012, while the
shape of the declaration is the direct evidence.

| task | run | official | declared columns | `order_by` / `one_per` | declaration step | steps | tool calls | refusals (advised) | advice taken up | prompt tokens | stop |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | failed (extra column) | `treatmentid, treatmentname` | `order_by: treatmentid`; `rows: at_least_one`, no `one_per` | 13 | 18 | 17 | 2 (2): `declaration_rows` (`rows` given as `4`); `join_on_as_mapping` | 2/2 | 272k | done |
| `task_44` | 2 | failed (extra column) | `treatmentid, treatmentname` | `order_by: treatmentid asc`; `rows: at_least_one`, no `one_per` | 21 | 28 | 27 | 3 (2): `subquery_as_semi_join`; `contract_mismatch` at export; unadvised: an invalid transform | 2/3 | 601k | done |
| `task_44` | 3 | **passed** | `treatmentname` | `order_by: treatmentid asc`; `one_per: treatmentid` | 37 | 41 | 40 | 10 (0): nine transforms naming a column the workspace does not hold, one `NOT_FOUND` at export | 5/10 | 1.03M | done |
| `task_329` | 1 | failed (extra column) | `intake_date, daily_max` | `one_per: intake_date`; no `order_by` | 15 | 19 | 18 | 3 (2): `like_as_function`; `expression_as_derive`; unadvised: an invalid transform | 3/3 | 305k | done |
| `task_329` | 2 | failed (extra column) | `date (date), max_volume_ml (float)` | neither; `rows: at_least_one` | 13 | 18 | 17 | 2 (0): an invalid transform, a missing name | 2/2 | 296k | done |
| `task_329` | 3 | failed (extra column) | `date (date), max_bolus_amt_ml (float)` | `order_by: date asc`; `rows: at_least_one`, no `one_per` | 10 | 13 | 12 | 1 (1): `like_as_function` | 1/1 | 162k | done |

**The prediction did not hold.** 0012 expected early declarations to stop
carrying the sort key. Across six runs the key was moved out of `columns` once.

The grammar is being used: five of the six runs named at least one organizing
key, and before 0012 there was nowhere to name one. But naming a key in
`order_by` or `one_per` did not, for this model, mean taking it out of the
payload. In four of those five runs the same key sits in both lists at once —
`task_44` runs 1 and 2 declare `order_by: treatmentid` and still carry
`treatmentid`; `task_329` run 1 declares `one_per: intake_date` and still
carries `intake_date`. The two lists are read as additive rather than as a
relocation. `task_329` run 2 used neither field and reproduced the baseline
shape exactly.

The verdicts moved from 2/3 to 1/3 on `task_44` and stayed at 0/3 on
`task_329`. All five failures are the same official error as every round since
2026-08-31: `column_count_mismatch`, "Expected 1 columns; received 2". Not one
of the extra columns is an invented business column; every one is the sort key
or the grain key, which is the observation 0012 was built on. That observation
still stands; the remedy did not take.

What did work is the mechanism. `task_44` run 3 declared `treatmentname` alone
with `order_by: treatmentid` and `one_per: treatmentid`, and the export carried
one column in treatment-id order, which the official evaluator accepted on both
values and order — so the backend's final sort-then-project step delivers what
0012 says it delivers, once the model declares that way. The new
`declaration_rows` detector also fired and was repaired: `task_44` run 1 gave
`rows` as `4`, was refused with advice naming the three accepted forms, and got
it right on the next call. The one run that produced the predicted shape is
also the longest and most confused of the six — 41 steps, 1.03M prompt tokens,
ten refusals of which nine were transforms hunting for a column name that does
not exist — and it declared at step 37, after that exploration rather than from
reading the framing. So the single instance is not evidence that the wording
taught the behaviour.

The cost side moved on `task_44` and not on `task_329`: refusals 7 to 15,
prompt tokens 483k to 635k, mean steps 25.7 to 29.0, elapsed 93 s to 120 s,
almost all of it from run 3 alone. The champion column-signature scorer barely
separates the runs (0.95, 0.95, 1.0 on `task_44`; 0.95 three times on
`task_329`), as it did before.

Six runs is a small sample. Three runs per task means the 2/3 to 1/3 move on
`task_44` is one run and carries no weight by itself; that is why the
declaration shape was recorded separately from the verdict. On shape the count
is one of six against a prediction that expected most, and that is the number
this round reports.

What the failure now is, stated for the next round rather than acted on here:
it is no longer that the model has nowhere to put the sort key. It has
somewhere, it puts it there, and it keeps a copy in the payload. Whether that
wants different wording, or a fact reported back at declaration time — the
backend already answers a declaration with what it holds under each name,
including whether a column is unique per row — is a judgment for the next
outcome, not something this measurement settles.

## 2026-09-02 · after the recovery module (teach on refusal, MADR 0010)

The backend now answers every refusal with `advice` (what it accepts instead,
and the request rewritten as tool calls when mechanical) and marks two silent
failures on successful responses (an empty result whose literal lives
elsewhere; a result that already fits the declared contract). The harness
reports two convergence numbers beside the verdict: refusals without advice,
and the repair rate (a refusal followed by a successful call of the same tool
within two calls). It also nudges after four previews of one source with
nothing materialized. Same model and gateway, same framing.

### `task_329`, two rounds

Round 1 ran with the detectors written from the `task_44` refusals. Its three
unadvised refusals were two new shapes (an expression with an alias inside
`select`; a string function on a timestamp), which became the detectors
`expression_as_derive` and `temporal_as_text`; round 2 ran with those.

| round | official | mean turns | mean prompt tokens | refusals | unadvised | repair rate | advice taken up | nudges |
|---|---|---|---|---|---|---|---|---|
| before advice (08-31) | 0/3 | 15.0 | 195k | 7 | 7 | — | — | — |
| round 1 | 0/3 | 12.3 | 138k | 3 | 3 | 1.0 | `matches_contract` 3/3 runs | 1 |
| round 2 | 0/3 | 11.7 | 126k | 2 | 0 | 1.0 | `matches_contract` 3/3, `expression_as_derive` 1, `unknown_key` 1 | 1 |

Per run, round 2:

| run | official | declared columns | declaration turn | turns | tool calls | refusals (advice) | prompt tokens | stop |
|---|---|---|---|---|---|---|---|---|
| 1 | failed (extra column) | `date, max_bolus_amount` | 1 | 10 | 9 | 0 | 90k | said DONE |
| 2 | failed (extra column) | `date, max_amount` | 7 | 10 | 9 | 0 | 113k | said DONE |
| 3 | failed (extra column) | `date, max_bolus_amt` | 1 | 15 | 15 | 2 (`expression_as_derive`, `unknown_key`) | 176k | said DONE |

What changed and what did not. Every run reached the correct value
(2105-12-30, 60.0) and exported it in the shape it had declared, so the
verdict is the same reading failure as in every earlier measurement: the gold
table has one column, the model declares two. That is the boundary MADRs 0007
and 0008 draw, and advice does not cross it; the backend never sees the
question. Inside the boundary the loop converged: unadvised refusals went from
seven to three to zero in two rounds, each refusal was repaired within two
calls, and `matches_contract` was taken up in every run, twice under the
proposed name `answer_oc_1`, so the preview-to-export tail shrank to two or
three calls. Turns fell from 15.0 to 11.7 and prompt tokens from 195k to 126k
across the three measurements. The one nudge (run 3, four previews of
`intakeoutput`) was followed by the materialization.

### `task_44`, three runs with the round-1 detectors

| official | mean turns | mean prompt tokens | refusals | unadvised | repair rate | exports | correct values exported | `semi_join` use | nudges |
|---|---|---|---|---|---|---|---|---|---|
| 0/3 (unchanged) | 26.0 (was 30.0) | 336k (was 425k) | 10 (was 12) | 5 (was 12) | 0.8 | 3/3 (was 0/3) | 3/3 | 1/3 | 2 |

Per run:

| run | official | declared columns | declaration turn | turns | tool calls | refusals (advised) | advice taken up | prompt tokens | stop |
|---|---|---|---|---|---|---|---|---|---|
| 1 | failed (two extra columns) | `treatmentid, treatmentname, treatmenttime` | 2 | 30 | 28 | 4 (3) | `subquery_as_semi_join` ×2 (materialized `cost_filtered`, ran `semi_join`), `distinct_as_group_by`, `value_not_found` ×2, `matches_contract` ×6 | 369k | said DONE |
| 2 | failed (extra column) | `treatment_id, procedure` | 1 | 28 | 28 | 4 (1) | `distinct_as_group_by`, `value_not_found`, `matches_contract` ×2 | 379k | said DONE |
| 3 | failed (extra column) | `procedure, treatment_id` | 1 | 20 | 19 | 2 (1) | `contract_mismatch` at export, then `matches_contract` | 261k | said DONE |

Every run exported the four gold procedure names in gold order, next to the
identifier or timestamp columns it had declared. Before advice, no run
exported anything: all three hit the turn limit, one of them with the exact
answer sitting in a preview. The change is inside the boundary: the model
still reads the question as asking for an identifier column, and the contract
holds the export to that reading; the "knew but did not do" failures
(unexported answer, previews of one identifier filtered by another's value)
went away. Run 1 shows the advice chain end to end: the subquery refusal
returned a `semi_join` rewrite with the filtered right dataset, the model
materialized `cost_filtered` and ran the `semi_join` as proposed; the join
kept the model's own mismatched identifiers, so the result was empty, and
`value_not_found` on the next preview said that the value lives in
`cost.patienthealthsystemstayid`, not `treatment.patientunitstayid`; from
there the run found the `eventid` path, materialized `answer_oc_1` on the
`matches_contract` advice and exported it. Run 3 was refused once at export
(`CONTRACT_MISMATCH`, the dataset lacked the declared `procedure` column),
renamed and exported three calls later.

The five unadvised refusals were: a document named as a dataset
(`describe_dataset("doc/patient.md")`), a document path with a leading slash
(`attach_metadata("/doc/patient.md")`), a bare field as a filter condition, a
sort by a field that a compact object's `select` had already dropped, and
`describe_dataset("patient")` for a table that is not in the workspace. The
first four became, in this order, the `document_as_dataset` advice (with the
`attach_metadata` rewrite), leniency in the document resolver, the
`predicate_not_boolean` advice, and a resolver rule that runs a compact
object's `select` after its `sort` when the sort key would be dropped. The
fifth already answers itself: the response lists the datasets that exist.

### `task_44`, three runs with the four fixes

| official | mean turns | mean prompt tokens | refusals | unadvised | repair rate | exports | correct values exported | nudges |
|---|---|---|---|---|---|---|---|---|
| **1/3** (was 0/3) | 26.7 (was 26.0) | 368k (was 336k) | 6 (was 10) | 3 (was 5) | 1.0 (was 0.8) | 2/3 (was 3/3) | 2/2 | 4 (was 2) |

Per run:

| run | official | declared columns | declaration turn | turns | tool calls | refusals (advised) | advice taken up | prompt tokens | stop |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **passed** | `treatmentname` | 21 | 25 | 24 | 2 (1) | `subquery_as_semi_join`, `no_matching_rows`, `near_contract` (materialized `answer_oc_1` with the projection it proposed), `matches_contract` | 331k | said DONE |
| 2 | failed (no file) | `procedure, treatment_id` | 1 | 30 | 30 | 1 (1) | `value_not_found`, `join_on_as_mapping` (repaired, then out of turns) | 439k | max turns |
| 3 | failed (extra column) | `treatment_id, procedure` | 1 | 25 | 25 | 3 (1) | `distinct_as_group_by`, `value_not_found`, `matches_contract` | 335k | said DONE |

Run 1 is the first official pass of the model-driven layer on `task_44`. It
declared late, at turn 21, after it had the four procedure rows in front of
it, and declared the gold shape exactly; the next preview carried an
identifier column beside the names, `near_contract` proposed the projection,
the model materialized it under the proposed name and exported. The two runs
that declared at turn 1 declared an identifier column again, as every early
declaration has: run 3 exported the four correct names next to it, run 2
took the `join_on_as_mapping` rewrite at turn 26 and ran out of turns. A
declaration made with the data in view read the question better than the
fresh ones; one run is not evidence, but it is the first counter-example to
the declaration-first framing (MADR 0007, 0008) and belongs in the next
experiment.

The four fixes themselves did not fire: no run named a document as a dataset,
passed a bare field as a filter, or sorted by a field its projection dropped.
Their measured effect on this sample is that the shapes did not recur. The
three unadvised refusals were a field no longer in scope after an earlier
step (the response lists the fields that are) and, twice, a SQL `LIMIT` tail
written inside a filter expression (`... IS NOT NULL LIMIT 5`), which is the
next detector: strip the tail into a limit step.

### Declaration timing: fresh versus informed (`task_44`, `task_329`)

The framing has two modes now (`--declaration`, see `framing.py`). `fresh` asks
for `declare_output` before anything else, the framing measured since
2026-08-31; `informed` asks for it once a preview shows the rows that answer
the question. Everything else in the framing is the same text. Three runs per
task and mode, same day, same model and gateway, the fresh arm being the runs
recorded just above (task_44 with the four fixes; task_329 round 2).

| task | framing | official | declarations with the gold shape | declaration turns | mean turns | mean prompt tokens | refusals | unadvised | repair rate | nudges | exports |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | fresh | 1/3 | 1/3 | 21, 1, 1 | 26.7 | 368k | 6 | 3 | 1.0 | 4 | 2/3 |
| `task_44` | informed | **2/3** | 2/3 | 26, 17, 24 | 26.3 | 346k | 5 | 1 | 1.0 | 3 | 3/3 |
| `task_329` | fresh | 0/3 | 0/3 | 1, 7, 1 | 11.7 | 126k | 2 | 0 | 1.0 | 1 | 3/3 |
| `task_329` | informed | **1/3** | 1/3 | 8, 8, 8 | 11.0 | 116k | 0 | 0 | — | 0 | 3/3 |

Per run, informed:

| task | run | official | declared columns | declaration turn | turns | tool calls | refusals (advised) | prompt tokens | stop |
|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | failed (extra column) | `treatmentid, treatmentname` | 26 | 30 | 29 | 2 (1) | 432k | said DONE |
| `task_44` | 2 | **passed** | `treatmentname` | 17 | 21 | 20 | 3 (3), one of them `CONTRACT_MISMATCH` at export, reshaped in two calls | 232k | said DONE |
| `task_44` | 3 | **passed** | `treatmentname` | 24 | 28 | 27 | 0 | 373k | said DONE |
| `task_329` | 1 | failed (extra column) | `date, max_volume` | 8 | 11 | 10 | 0 | 123k | said DONE |
| `task_329` | 2 | failed (extra column) | `day, daily_max_volume` | 8 | 11 | 10 | 0 | 107k | said DONE |
| `task_329` | 3 | **passed** | `max_enteral_formula_volume_ml` | 8 | 11 | 10 | 0 | 118k | said DONE |

What the declaration timing comparison shows. Across both tasks, declarations made with the
answer rows in view named the gold shape three times out of six against one
out of six for declarations made from the question alone (and that one was
the fresh-arm run that had ignored its framing and declared at turn 21).
Passes went from 1/6 to 3/6, `task_329`'s first pass among them, with turns,
prompt tokens, refusals and nudges no worse in either task. The traces show
the mechanism: a model declaring from the question alone adds the identifier
or date column it expects to need; with the rows in front of it, it more
often drops that column. The contract does the same work in both arms: an
informed declaration is held at export like a fresh one (`task_44` run 2 was
refused once at export and reshaped). Twelve runs is a small sample; the
effect points the same way on both tasks. For the trust model (MADR 0008)
nothing changes: a declaration is the agent's fresh output when it is made,
whatever the turn. What changes is the reading of "while the requirement is
in front of you" (MADR 0007): the requirement is the question and the shape of
the data together. The runner's default framing is `informed` from here on;
each measurement records which framing it used.

### OpenCode as the host, in a container (`task_44`, `task_329`, informed framing)

The harness no longer owns the model loop (MADR 0011). `--host opencode` runs
OpenCode 1.18.26 in a container built from `agent_harness/hosts/opencode.Dockerfile`
(OpenCode pinned, the backend's MCP entry point baked in), the run directory
mounted at `/run`, the benchmark read-only at `/data`, an empty HOME and working
directory; every builtin tool is disabled and only the `backend_*` tools are
allowed; the agent prompt is the backend's instructions plus the same informed
framing; the event stream is normalized into the harness's tool-event record,
so the metrics below are computed the same way as for the in-process loop. No
turn cap and no nudge apply under OpenCode; a run is bounded by a 900 s
timeout, which no run reached. Same model and gateway, same day; the loop rows
are the informed runs recorded above.

A first attempt ran OpenCode on the machine, from the run directory inside
this repository. Its system prompt carried the repository's `AGENTS.md`:
OpenCode folds any `AGENTS.md` or `CLAUDE.md` above its working directory or
above the config file it loads into the prompt. First-step prompt tokens were
7.6k on the machine against 5.2k for the same prompt from a directory outside
the repository and 5.0k in the container, about 2.4k tokens per step of
DriftSeal protocol the model was never meant to see. Those six runs (task_44
2/3 at 23.3 steps and 447k tokens, task_329 0/3 at 16.3 steps and 288k) are
discarded; the container is the only OpenCode host from here on.

| task | host | official | declarations with the gold shape | declaration steps | mean steps | mean prompt tokens | first-step tokens | refusals | unadvised | repair rate | mean elapsed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | loop | 2/3 | 2/3 | 26, 17, 24 | 26.3 | 346k | — | 5 | 1 | 1.0 | — |
| `task_44` | OpenCode, container | 2/3 | 2/3 | 33, 16, 19 | 25.7 | 483k | 5.0k | 7 | 4 | 0.57 | 93 s |
| `task_329` | loop | 1/3 | 1/3 | 8, 8, 8 | 11.0 | 116k | — | 0 | 0 | — | — |
| `task_329` | OpenCode, container | 0/3 | 0/3 | 11, 9, 11 | 14.3 | 218k | 5.0k | 4 | 0 | 1.0 | 56 s |

Per run, OpenCode in the container:

| task | run | official | declared columns | declaration step | steps | tool calls | refusals (advised) | prompt tokens | stop |
|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | **passed** | `treatmentname` | 33 | 36 | 35 | 5 (2): `join_on_as_mapping` ×2; unadvised: a duplicate output field, a field out of scope, an export to `/tmp` refused by the sandbox | 838k | done |
| `task_44` | 2 | **passed** | `treatmentname` | 16 | 19 | 18 | 1 (1): `distinct_as_group_by` | 263k | done |
| `task_44` | 3 | failed (extra column) | `treatmentid, treatmentname` | 19 | 22 | 21 | 1 (0): an integer compared with a string | 348k | done |
| `task_329` | 1 | failed (extra column) | `date (string), max_bolus_amt_ml (float)`, amended to `date (date)` at step 14 | 11 | 17 | 16 | 2 (2): `like_as_function`; `contract_mismatch` at export (the declared type), then the amendment | 280k | done |
| `task_329` | 2 | failed (extra column) | `date (string), max_enteral_formula_ml (float)` | 9 | 12 | 11 | 1 (1): `expression_as_derive` | 171k | done |
| `task_329` | 3 | failed (extra column) | `date, daily_max_enteral_formula_ml` | 11 | 14 | 13 | 1 (1): `expression_as_derive` | 203k | done |

What the comparison shows. On `task_44` the host made no difference to the
verdict: 2/3 under both, the same two gold-shape declarations, at about the
same point (steps 16 to 33). On `task_329` every OpenCode declaration carried
a date column, 0/3 against the loop's 1/3; the same held in the discarded
machine runs, so across six OpenCode runs the task is 0/6 against the loop's
1/3, within noise at these counts but consistent in direction. The advice
chain works under a foreign host as under the loop: `expression_as_derive`,
`like_as_function`, `distinct_as_group_by` and `join_on_as_mapping` were each
repaired on the next call, `matches_contract` preceded the exports, and the
one `CONTRACT_MISMATCH` (a type declared as string that the data holds as a
date) was answered by amending the declaration. The export sandbox held: run 1
of `task_44` tried to write `/tmp/debug_export.csv` and was refused with the
allowed root. Prompt tokens are higher under OpenCode (483k against 346k on
`task_44`, 218k against 116k on `task_329`): the host does not truncate tool
results as the loop does (8000 characters) and one `task_44` run explored for
36 steps. Every run ended by itself, none by the timeout; the host's own
pacing replaced the loop's nudge without loss on these two tasks. The
unadvised refusals of this round, a duplicate output field, a field out of
scope after a join, an integer compared with a string and an export outside
the sandbox, each already carry the fact the model needs in `message` or
`hint`; whether they deserve `advice` entries is a judgment for the next
round.

## 2026-09-02 · task_44 after the harness split, before advice

The harness moved to `agent_harness/` with framing, scripts and scoring
unchanged. The model is `qwen/qwen3.5-35b-a3b` through the Kilo gateway, which
reports no cost, so the cost column is absent from here on. Three runs on
`task_44`:

| official | mean turns | mean prompt tokens | refusals | refusals naming the accepted shape | exports | `semi_join` use |
|---|---|---|---|---|---|---|
| 0/3 (unchanged) | 30.0 (was 26.7) | 425k (was 336k) | 12 (11 errors, 1 needs_resolution) | 0 | 0 (was 1) | 0/3 (was 1/3) |

Per run:

| run | official | declared columns | turns | tool calls | refusals | prompt tokens | stop |
|---|---|---|---|---|---|---|---|
| 1 | failed (no file) | `treatment_id, procedure_name` | 30 | 31 | 3 | 428k | max turns |
| 2 | failed (no file) | `treatment_id, procedure` | 30 | 31 | 5 | 436k | max turns |
| 3 | failed (no file) | `procedure` | 30 | 31 | 3 | 412k | max turns |

All three declared in turn 1; run 3 declared the gold shape exactly, the
first of twelve declarations across all measurements to do so. None exported.
The refusals: three `IN (SELECT …)` subqueries answered with a parser error
("Expected ')'"), `distinct` as a key and as a select prefix (three), `join.on`
as an equality string (two), `LIKE` (one), an aggregate written inside
`select` (one), an inline relation as `source` (one), a document named as a
dataset (one). Five carried no hint, six a generic one ("Use one of the listed
field names"), one an example of another shape; none named the shape the
backend accepts for what the model wrote, and `semi_join` was never
discovered.

Run 2 had the answer. At turn 27 its preview of `treatment` filtered by the
four event ids at the latest `cost` timestamp returned the four gold procedure
names in gold order; it spent the last three turns trying to fold that into one
pipeline (an inline relation as source, `on` as an equality string) and never
materialized or exported it. Runs 1 and 3 filtered `treatment.patientunitstayid`
by a `patienthealthsystemstayid` value taken from `cost`, got empty previews
marked success, and kept previewing `cost`. The `cost.eventid →
treatment.treatmentid` relationship is declared nowhere in the workspace: the
SQLite foreign keys point at a `patient` table that is not there, and neither
document mentions it. No backend fact can supply it; run 2 shows the model can
find it.

This measurement is the ground for MADR 0010: a refusal must name the accepted
shape, and a silent failure must be named too.

## 2026-08-31 · post-semi-join task_44 experiment

After `semi_join`, post-aggregate projection and measureless grouping were on
the MCP transform surface, the same configured model and gateway were run three
more times on `task_44`. The declaration-first framing was unchanged.

| official | mean turns | mean prompt tokens | mean cost | tool errors | successful declaration turns | `semi_join` use |
|---|---|---|---|---|---|---|
| 0/3 (unchanged) | 26.7 (was 30.0) | 336k (was 437k) | $0.0032 (was $0.0041) | 14 (was 11) | 1, 2, 2 (was 3, 1, 1) | 1/3 runs; 4 calls, 2 successful |

Per run:

| run | official | declared columns | turns | tool calls | tool errors | prompt tokens | cost | `semi_join` | stop |
|---|---|---|---|---|---|---|---|---|---|
| 1 | failed (no file) | `treatment_id, procedure_name, treatment_timestamp` | 30 | 31 | 9 | 370k | $0.0035 | 4 calls, 2 successful | max turns |
| 2 | failed (extra column) | `treatment_id, procedure` | 20 | 19 | 2 | 217k | $0.0021 | not used | said DONE |
| 3 | failed (no file) | `treatmentid, treatmentname, treatmenttime` | 30 | 30 | 3 | 421k | $0.0039 | not used | max turns |

The 14 tool errors were 6 `NOT_FOUND`, 5 `INVALID_TRANSFORM` and 3
`INVALID_INTENT`. Run 1 discovered `semi_join`, repaired two invalid key
shapes, and invoked it successfully twice. It nevertheless related a
health-system stay id to a unit-stay id, obtained no rows, and exhausted the
turn budget. The same run also used measureless grouping successfully to
materialize a distinct key set. Runs 2 and 3 never invoked `semi_join`.

Run 2 followed the direct `cost.eventid = treatment.treatmentid` relationship
instead. It exported the four correct procedure values in the required order,
but carried the helper treatment id because that id was in its fresh output
contract. The isolated experiment directory was supplied as a relative path,
so the in-loop scorer did not find the resulting nested export. Running the
vendored official evaluator offline against that CSV produced the same semantic
verdict: `column_count_mismatch`, with 4/4 rows and two predicted columns versus
one gold column. This rescore made no model call.

The new operation therefore closes the backend capability gap and is visible
enough for the model to attempt, but it does not change the 0/3 task result.
The remaining failures are in reading the requested output shape and choosing
the correct relationship between source identifiers. The lower mean turns and
tokens are descriptive only: with three runs and no accuracy change they are
not evidence of a model-level efficiency improvement.

## 2026-08-31 · declaration-first experiment before the remaining vocabulary

The scenario framing now tells the model, before the workspace instruction,
to read the question once and make `declare_output` its first tool call. It
also says not to include helper, grouping or identifier columns unless the
question explicitly asks for them. The same model and gateway were run three
times on `task_44` and `task_329` before any core vocabulary changed.

| task | official | mean turns | mean prompt tokens | mean cost | tool errors | declaration turns |
|---|---|---|---|---|---|---|
| `task_44` | 0/3 (was 0/3) | 30.0 (was 26.3) | 437k (was 351k) | $0.0041 (was $0.0034) | 11 (was 8) | 3, 1, 1 (was never, 28, 16) |
| `task_329` | 0/3 (was 0/3) | 15.0 (unchanged) | 195k (was 164k) | $0.0018 (was $0.0015) | 7 (was 9) | 1, 1, 1 (was 13, 8, 7) |

Per run:

| task | run | official | declared columns | turns | tool calls | tool errors | prompt tokens | cost | stop |
|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | failed (no file) | `treatmentid, treatmentname` | 30 | 29 | 1 | 419k | $0.0039 | max turns |
| `task_44` | 2 | failed (no file) | `treatment_id, procedure_name` | 30 | 32 | 4 | 440k | $0.0043 | max turns |
| `task_44` | 3 | failed (no file) | `treatment_id, procedure` | 30 | 31 | 6 | 453k | $0.0042 | max turns |
| `task_329` | 1 | failed (extra column) | `date, max_amount` | 13 | 15 | 2 | 144k | $0.0014 | said DONE |
| `task_329` | 2 | failed (extra column) | `date, max_volume` | 12 | 12 | 3 | 134k | $0.0012 | said DONE |
| `task_329` | 3 | failed (two extra columns) | `patient_id, date, max_enteral_volume` | 20 | 21 | 2 | 307k | $0.0028 | said DONE |

The experiment fixes the timing defect without changing the reading defect.
Five runs made the declaration in the same first turn as `import_workspace`;
the remaining `task_44` run declared at turn 3 rather than never or at turn
28. All six fresh declarations nevertheless added an identifier or grouping
column that the gold answer does not carry. `task_329` still reached the
correct value in every run and exported it with the declared extra columns;
`task_44` still spent all three runs searching for a path between the patient
identifier and treatment rows. A fresh declaration is useful as a durable
reference, but it does not make a first reading correct, the boundary stated
by MADRs 0007 and 0008.

## 2026-08-30 · after the output contract and the vocabulary from the second measurement

What changed since the second measurement, in the order that measurement
asked for it: `declare_output` and the contract check in `export_result`
(MADR 0007), `strftime` accepted with the pattern first, a type error that
names the signature, `date`/`date_trunc`, `group_by` over a named expression,
`in [a, b]`, and `{"aggregate": {...}}` with the body nested under its own
key.

### Scripted layer

Every scripted task now declares its output contract as its first call, so
the export at the end is held to it.

| task | tool calls | elapsed | official evaluator | champion scorer | contract |
|---|---|---|---|---|---|
| `task_10` | 7 | 2.8 s | **passed** | 1.0 | `[EndDate, TotalAssets]`, at least one row — satisfied |
| `task_44` | 6 | 5.2 s | **passed** | 1.0 | `[treatmentname]`, at least one row — satisfied |
| `task_127` | 6 | 52.3 s | **passed** | 1.0 | `[maximum_respiration]`, one row — satisfied |
| `task_329` | 6 | 8.7 s | **passed** | 1.0 | `[daily_maximum]`, one row — satisfied |

`task_329` groups by `"date(intakeoutputtime) as day"` directly, the shape
the model reached for in the second measurement and was refused.

### Model-driven layer (qwen3.5-35b-a3b through the MCP tools, 3 runs per task)

Same model and gateway as the second measurement; the prompt is still only
the MCP server's instructions (which now say to declare the output first),
the question, the workspace path and the tool schemas. Only the two tasks
that failed last time were re-run.

| task | official | mean turns | mean prompt tokens | mean cost | tool errors (3 runs) |
|---|---|---|---|---|---|
| `task_44` | 0/3 (was 0/3) | **26.3** (was 29.3) | **351k** (was 390k) | $0.0034 (was $0.0038) | **8** (was 11) |
| `task_329` | 0/3 (was 0/3) | **15.0** (was 22.0) | **164k** (was 262k) | $0.0015 (was $0.0024) | **9** (was 26) |

Per run:

| task | run | official | champion | turns | tool calls | tool errors | prompt tokens | cost | stop | contract declared at turn |
|---|---|---|---|---|---|---|---|---|---|---|
| `task_44` | 1 | failed (no file) | — | 30 | 29 | 3 | 412k | $0.0042 | max turns | never |
| `task_44` | 2 | failed (extra columns) | 0.93 | 30 | 30 | 4 | 432k | $0.0041 | max turns | 28 |
| `task_44` | 3 | failed (extra columns) | 0.93 | 19 | 18 | 1 | 210k | $0.0020 | said DONE | 16 |
| `task_329` | 1 | failed (extra column) | 0.95 | 24 | 22 | 8 | 302k | $0.0027 | said DONE | 13 |
| `task_329` | 2 | failed (extra column) | 0.95 | 11 | 10 | 1 | 97k | $0.0009 | said DONE | 8 |
| `task_329` | 3 | failed (extra column) | 0.95 | 10 | 9 | 0 | 94k | $0.0009 | said DONE | 7 |

### What the vocabulary changes did

They removed the refusals they were built for. The thirteen
`strftime`-pattern-first refusals of the second measurement are gone (zero
`TYPE_MISMATCH` in six runs); every `task_329` run wrote
`date_trunc('day', intakeoutputtime)` and `{"aggregate": {"group_by": …,
"measures": …}}` and both were accepted; two runs of `task_329` reached the
right daily maximum in nine and ten tool calls with zero or one error. Turns,
tokens and cost fell on both tasks.

### Where the six runs actually failed

Five of six exported the **correct values with extra columns** again
(`treatmentid, treatmenttime` next to `treatmentname`; `date` next to the
maximum), and the sixth never exported. This time, though, every run that
exported had **declared exactly those extra columns in its contract** — the
contract check passed because the deliverable matched what the agent had
written down. That is the boundary of MADR 0007 and 0008 observed, not a
defect in it: the backend held the export to the declaration, and the
declaration encoded a misreading of the question ("by treatment id" read as
"include the id", "daily maximum … during the encounter" read as "one row per
day"). A wrong first reading is enforced as faithfully as a right one, and it
belongs to the model, not to the backend.

Two things in that are worth acting on:

1. **The contract was never declared fresh.** The server instructions say to
   declare the output while the requirement is in front of you; the model
   declared at turns 7–28, after exploration and in `task_44` immediately
   before materializing — the degenerate "contract declared at the last
   moment" that 0007 names. Whether an early declaration would have read the
   question better is untested; it is the next experiment, and it belongs in
   the scenario framing under `agent_harness/scenarios/dataspace/`, not in core.
2. **The one `CONTRACT_MISMATCH` was repaired in one turn.** `task_329` run 1
   tried to export the un-aggregated table against a contract naming
   `max_ml`; the error named the missing and the extra column, and the next
   two calls materialized the aggregate and exported it.

The tool errors, by kind:

| refusal | count | where |
|---|---|---|
| `select` naming an aggregate output inside a compound object (`{"aggregate": …, "select": ["date", "max_ml"], "sort": "-date"}`): the compact form applies `select` before `aggregate`, so `max_ml` does not exist yet | 6 | `task_329` run 1 |
| a SQL subquery inside a filter (`patientunitstayid IN (SELECT … FROM cost WHERE …)`) — the agent wants to filter by another dataset's values | 5 | `task_44`, all runs |
| `group_by` with no measure, meaning "distinct values" | 2 | `task_44` run 2 |
| `"distinct": true` as a step key | 1 | `task_44` run 2 |
| an expression with an alias inside `select` (`"date_trunc('day', t) as date"`) | 1 | `task_329` run 1 |
| a rename of a guessed auto-alias (`max_cellvaluenumeric`) | 1 | `task_329` run 1 |
| `attach_metadata` with a path that does not exist | 1 | `task_329` run 2 |

### The champion pipeline on the same two tasks

For scale, the KDD Cup 2026 champion pipeline (`zhezh/kddcup2026_champion`,
`upstream/main` at `bdc874f`, full SQL through a `solver.py` the model edits,
same model and gateway) was run once on the same two tasks on 2026-08-30 and
scored with the same evaluator:

| task | official | output columns | turns | input tokens | wall clock |
|---|---|---|---|---|---|
| `task_44` | **passed** | `treatmentname` (4 rows) | 14 | 211k | 781 s, of which 693 s document extraction |
| `task_329` | failed (extra column) | `intake_date, daily_max_amount` → `2105-12-30, 60.0` | 12 | 183k | 145 s |

Against Intentum's third measurement (`task_44` 0/3 at 26.3 turns / 351k,
`task_329` 0/3 at 15.0 turns / 164k), three things stand out:

1. **`task_329` fails the same way with full SQL.** The champion's solver
   template has a `# 目标输出列:` line the model fills in before writing the
   query — a declaration step of its own — and the model filled it with
   `intake_date, daily_max_amount`, the same two columns three Intentum runs
   declared as their contract. The extra column is the model's reading of the
   question, not a property of either backend.
2. **`task_44` passed on a reading, not on a capability.** The champion's
   reasoning trace says "只需要输出 procedure（treatmentname）" and the SQL
   selects one column; on the same question Intentum's runs declared and
   exported `treatmentid, treatmentname, treatmenttime`. One champion run
   against three Intentum runs (one of which never exported) does not show
   the champion reads more accurately, only that it read correctly this time.
   The path it took — `treatment` joined to `patient`, then
   `treatmenttime = (SELECT MAX(...))` — is the subquery shape Intentum
   refused five times, i.e. the semi-join listed below.
3. **Turns and tokens are in the same range.** `task_329`: 15.0 turns / 164k
   for Intentum against 12 / 183k; `task_44`: 26.3 / 351k against 14 / 211k,
   the gap there being the subquery detour.

So the vocabulary and the contract are not what separates the two on these
tasks; the semi-join is, and the reading of the question is a shared limit of
the model.

### Next, from this measurement

Two of these are general and cheap: read a compound object's `select` after
`aggregate` when it names aggregate outputs (or honour the written key order
outright), and accept `group_by` without measures as a distinct-values step.
The subquery-in-filter shape is the deferred consequence of MADR 0008 made
concrete — the agent wants to *reference* another dataset's values rather
than copy them — and calls for a semi-join (`filter … in (dataset.column)`),
not for `raw_query` (MADR 0002). The column-count failure itself is a reading
of the question, which the backend cannot judge; the experiment that follows
is to make the scenario prompt ask for the declaration first and see whether
a fresh declaration reads the question differently.

## 2026-08-28 · after the export specification, temporal refinement and the loose shapes

### Scripted layer

| task | tool calls | elapsed | official evaluator | champion scorer |
|---|---|---|---|---|
| `task_10` | 6 | 2.8 s | **passed** | **1.0** (was 0.45) |
| `task_44` | 5 | 5.5 s | **passed** | 1.0 |
| `task_127` | 5 | 56.7 s | **passed** | 1.0 |
| `task_329` | 5 | 7.9 s | **passed** | 1.0 |

`task_10`'s champion score moved from 0.45 to 1.0 without the dataset
changing: the only difference is that `export_result` now applies the
question's rendering rules (`{"decimals": 4, "strip_trailing_zeros": true,
"integer_min_decimals": 1}`) instead of leaving number-to-text to the engine.
That is the whole of MADR 0005 in one number.

`task_127` takes 57 s because its workspace holds 854k vitalperiodic rows;
the temporal probe added at import is ~7 % of that (53 s vs 49 s for the
whole workspace import, measured with the probe disabled).

### Model-driven layer (qwen3.5-35b-a3b through the MCP tools, 3 runs per task)

Same model and gateway as the baseline; the prompt is still only the MCP
server's instructions, the question, the workspace path and the tool schemas.

| task | official | mean turns | mean prompt tokens | mean cost | tool errors (3 runs) |
|---|---|---|---|---|---|
| `task_10` | **3/3** (baseline 2/3) | **17.0** (baseline 27.0) | **339k** (baseline 489k) | $0.0031 | 6 |
| `task_127` | **3/3** | 11.3 | 107k | $0.0010 | 3 |
| `task_44` | 0/3 | 29.3 | 390k | $0.0038 | 11 |
| `task_329` | 0/3 | 22.0 | 262k | $0.0024 | 26 |

Per run:

| task | run | official | turns | tool calls | tool errors | prompt tokens | cost | stop |
|---|---|---|---|---|---|---|---|---|
| `task_10` | 1 | passed | 11 | 10 | 1 | 135k | $0.0012 | said DONE |
| `task_10` | 2 | passed | 10 | 9 | 0 | 132k | $0.0014 | said DONE |
| `task_10` | 3 | passed | 30 | 30 | 5 | 749k | $0.0067 | max turns |
| `task_127` | 1 | passed | 9 | 8 | 0 | 80k | $0.0008 | said DONE |
| `task_127` | 2 | passed | 12 | 12 | 1 | 110k | $0.0011 | said DONE |
| `task_127` | 3 | passed | 13 | 12 | 2 | 132k | $0.0013 | said DONE |
| `task_44` | 1 | failed (no file) | 30 | 30 | 4 | 415k | $0.0041 | max turns |
| `task_44` | 2 | failed (extra column) | 28 | 27 | 4 | 367k | $0.0035 | said DONE |
| `task_44` | 3 | failed (extra column) | 30 | 28 | 3 | 387k | $0.0039 | max turns |
| `task_329` | 1 | failed (extra column) | 23 | 22 | 8 | 276k | $0.0025 | said DONE |
| `task_329` | 2 | failed (no file) | 30 | 30 | 14 | 385k | $0.0035 | max turns |
| `task_329` | 3 | failed (extra column) | 13 | 12 | 4 | 125k | $0.0012 | said DONE |

### What changed against the baseline

- **The formatting loop is gone.** In the baseline both passing `task_10` runs
  had the correct file on disk by turn 8-12 and then burned every remaining
  turn trying to render values inside the transform (`strftime`, `format`,
  `substr`, `||`), each refused, until the 30-turn cap. Now two of three runs
  export once and say DONE at turn 10-11; mean turns 27 → 17, mean prompt
  tokens 489k → 339k, and the run that still hits the cap passes anyway.
  In run 3 the model even reached for `format_spec` *in a transform* first
  (`Unknown key(s) ['format_spec'] in transform`) — it had read the
  capability off the export tool and mislocated it, not looked for a
  workaround.
- **`task_10` is 3/3.** The baseline's failing run divided by 100 after
  misreading the unit `亿元 (100M CNY)`; that did not recur.
- **Temporal refinement is invisible when it works.** No run needed a cast to
  compare or sort a date, and `year()`/`strftime()` on a SQLite-sourced date
  column no longer raise `TYPE_MISMATCH` (`task_10`'s `EndDate` is now
  `timestamp`, `task_329`'s `intakeoutputtime` likewise, both from text).
- **The accepted loose shapes stopped appearing as errors.** Not one refusal
  in the twelve runs was a single-key step, an `x as y` alias, a `derive`
  list or `||`.

### Where the four failing runs actually failed

None of them failed on something the backend cannot express: the scripted
layer answers all four tasks in five or six calls.

1. **An extra column, four times.** `task_44` runs 2-3 and `task_329` runs 1
   and 3 exported the *correct values* and were rejected by the evaluator for
   shipping one column too many (`treatmentid` next to `treatmentname`;
   `event_date` next to the maximum). The prompt does say "exactly the columns
   the question asks for". The answer table is a contract the tool surface
   does not currently make explicit anywhere.
2. **`strftime` argument order, thirteen times.** Every `task_329`
   `TYPE_MISMATCH` is `strftime('%Y-%m-%d', intakeoutputtime)` — pattern
   first, as in Python and as DuckDB also accepts. The allowlist takes
   `(temporal, pattern)` only, and the error message names the type without
   naming the order.
3. **Date-part vocabulary.** Around that, `task_329` reached for `date(x)`,
   `to_char`, `strptime`, `cast(x as string)` and `x::string`, and tried to
   `group_by` the expression text itself
   (`"strftime('%Y-%m-%d', intakeoutputtime)"` as a field name). Deriving a
   day key and grouping by it works; discovering that took the model a dozen
   turns.
4. **Path finding in `task_44`.** The patient id lives only in `cost`, and the
   link is `cost.eventid = treatment.treatmentid` with
   `eventtype = 'treatment'`. Run 1 never found it (and never exported);
   runs 2-3 did. Side observations: `IN [1, 2, 3]` with brackets instead of
   parentheses (twice), a SQL subquery inside a filter expression (once), and
   `attach_metadata`/`import_workspace` called with a path prefix that does
   not exist (three times).

### Next, from this measurement

In frequency order, all general and none specific to DataSpace: accept
`strftime` with either argument order (13 refusals); make the answer-table
contract visible at the export boundary (4 lost runs with a correct answer);
a small date-part vocabulary (`date`/`day` truncation) and `group_by` over a
derived expression (9 refusals); `IN` with bracket lists; and an aggregate
step whose body is nested under its own key
(`{"aggregate": {"group_by": …, "measures": …}}`, 2 refusals). A raw_query
fallback (MADR 0002) is still not what any of these need.

## Baseline · before the export specification and the loose shapes (2026-08-28)

Kept because it is the evidence behind MADR 0005 (export formatting) and
MADR 0006 (temporal refinement).

### `task_10`, scripted agent

Six tool calls, ~4 s, no LLM: `import_workspace` (16 datasets from 8 csv,
8 sqlite tables, 1 wrapper json) → `attach_metadata("knowledge.md")` →
`search_datasets("monetary authority balance sheet total assets")` →
`describe_dataset` → `materialize_result` with sloppy field names
(`total assets`, `enddate`) → `export_result`.

- **Official evaluator: passed** (242/242 rows, order-sensitive, both columns
  aligned).
- **Champion repo scorer: 0.45** (1 of 2 column signatures matched). Cause:
  DuckDB prints the double `31783696.815` as `31783696.814999998`; that scorer
  rounds to 2 decimals half-up and gets `.81` vs gold `.82`, while the official
  evaluator compares at the config's 4 decimal places and accepts. Same table,
  different text.
- SQLite date columns arrived as `VARCHAR` (DuckDB's SQLite type mapping);
  ISO strings sort correctly, but the schema showed `string` where the agent
  would expect a date.
- The Chinese table name in the question (货币当局资产负债表) does not resolve
  directly — the knowledge document is English — so the scripted agent
  searched in English.

### `task_10`, qwen3.5-35b-a3b through MCP, 3 runs

| run | official | turns | tool calls | tool errors | prompt tokens | cost | stop |
|---|---|---|---|---|---|---|---|
| 1 | **failed** (`relation_mismatch`) | 21 | 20 | 7 | 374k | $0.0033 | said DONE |
| 2 | passed | 30 | 30 | 3 | 611k | $0.0054 | max turns |
| 3 | passed | 30 | 30 | 12 | 502k | $0.0045 | max turns |

- **Finding the data was never the problem.** Every run went
  `import_workspace` → `describe_dataset("ed_moneyauthoritybs")` in two turns,
  straight from the import listing, despite the Chinese question and English
  knowledge document.
- **Run 1 failed on semantics**: the model divided `TotalAssets` by 100 to
  "convert to 亿元" although the column already is in 亿元.
- **Runs 2 and 3 never stopped because of the formatting clause**, retrying
  `strftime`, `format`, `substr`, `||` inside transforms until the cap.
- **Loose-intent shapes the model used that the backend rejected**: a list of
  single-key steps without `type` (5 times); `sort` with `order` as the key
  list; `"EndDate as ReportPeriod"` in `select` and in a derive expression;
  `derive` as a list of `{name, expression}`; string/date functions outside
  the allowlist.
- **The export sandbox held**: run 3 tried `/tmp/...`, received
  `PERMISSION_DENIED` with the allowed root in the hint, and continued inside
  it.
