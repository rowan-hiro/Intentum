# DataSpace smoke tests

Scripts for running Intentum against the [DataSpace](https://dataspace-bench.github.io)
benchmark (KDD Cup 2026 Data Agent track).

- `evaluate.py` — the official evaluator, vendored unmodified from
  `HKUSTDial/DataSpace` (commit `6491caa`, MIT). Task Accuracy is binary per
  task: one one-to-one column mapping must make the whole predicted table equal
  to the gold table under the task's config (types, precision, row order).
  A prediction with more columns than the gold table fails outright.
- `../dataspace_smoke.py` — drives a task end to end through the MCP tool
  surface with a scripted agent (no LLM), writes
  `predictions/<task>/prediction.csv`, scores it with the official evaluator
  and (when the champion repository is available) with its local
  column-signature scorer, and records the full tool-call trace in
  `smoke_result.json`.
- `../dataspace_agent.py` — the same tool surface driven by a real model: a
  thin OpenAI-compatible agent loop with no SQL and no dialect rules in the
  prompt, scored the same way, with per-run traces and an aggregated summary.

The benchmark package itself (task inputs, 60 public `gold.csv` files and
their configs) is expected at `$DATASPACE_BENCHMARK` or
`~/dev/kddcup2026_champion/DataSpace-Benchmark`.

```sh
uv run python examples/dataspace_smoke.py --task all --check
uv run python examples/dataspace_agent.py --task task_127 --runs 3
```

## Tasks under measurement

Four public-reference tasks whose answers come from structured sources alone:

| task | question (abridged) | what it exercises |
|---|---|---|
| `task_10` | 货币当局资产负债表: reporting period and total assets where total assets is not null, oldest first | Chinese question against an English knowledge document; the question prescribes how numbers are rendered |
| `task_44` | procedures a patient received at the latest treatment timestamp, in treatment id order | the patient is only named in `cost`, so the answer needs a join and a second round on the maximum |
| `task_127` | maximum respiration for a patient on one day | json sources; a day filter on a timestamp that the source stores as text |
| `task_329` | daily maximum enteral formula volume for a patient | derive a day key, then aggregate twice through two managed datasets |

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
