# DataSpace smoke tests

Scripts for running Intentum against the [DataSpace](https://dataspace-bench.github.io)
benchmark (KDD Cup 2026 Data Agent track).

- `evaluate.py` — the official evaluator, vendored unmodified from
  `HKUSTDial/DataSpace` (commit `6491caa`, MIT). Task Accuracy is binary per
  task: one one-to-one column mapping must make the whole predicted table equal
  to the gold table under the task's config (types, precision, row order).
- `../dataspace_smoke.py` — drives one task end to end through the MCP tool
  surface with a scripted agent, writes `predictions/<task>/prediction.csv`,
  scores it with the official evaluator and (when the champion repository is
  available) with its local column-signature scorer, and records the full
  tool-call trace in `smoke_result.json`.

The benchmark package itself (task inputs, 60 public `gold.csv` files and
their configs) is expected at `$DATASPACE_BENCHMARK` or
`$DATASPACE_BENCHMARK`.

```sh
uv run python examples/dataspace_smoke.py --task task_10 --check
```

## Findings

### task_10 (scripted agent, 2026-08-28)

Question: list monetary-authority balance-sheet records with non-null total
assets, by reporting period, returning period and total assets (亿元).

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
  different text. This is the first concrete requirement for `export_result`
  formatting rules (MADR 0003, step 5): the task text itself says "round to at
  most 4 decimals, strip trailing zeros, integers keep one decimal".
- SQLite date columns arrive as `VARCHAR` (DuckDB's SQLite type mapping);
  ISO strings sort correctly here, but the schema shows `string` where the
  agent would expect a date. Candidate for import-time type refinement.
- The Chinese table name in the question (货币当局资产负债表) does not resolve
  directly — the knowledge document is English — so the scripted agent
  searched in English. Whether a model does this in one round is the point of
  the LLM-driven layer.

### task_10 (qwen3.5-35b-a3b through MCP, 3 runs, 2026-08-28)

```sh
uv run python examples/dataspace_agent.py --task task_10 --runs 3
```

Same model as the champion pipeline (via the same gateway), a thin agent
loop, no SQL, no dialect or column-naming rules in the prompt — only the MCP
server's instructions, the question, the workspace path and the tool schemas.

| run | official | turns | tool calls | tool errors | prompt tokens | reasoning tokens | cost | stop |
|---|---|---|---|---|---|---|---|---|
| 1 | **failed** (`relation_mismatch`) | 21 | 20 | 7 | 374k | ~4k | $0.0033 | said DONE |
| 2 | passed | 30 | 30 | 3 | 611k | ~5k | $0.0054 | max turns |
| 3 | passed | 30 | 30 | 12 | 502k | ~5k | $0.0045 | max turns |

- **Finding the data was never the problem.** Every run went
  `import_workspace` → `describe_dataset("ed_moneyauthoritybs")` in two turns,
  straight from the import listing, despite the Chinese question and English
  knowledge document. Runs 2 and 3 had a correct `prediction.csv` on disk by
  turn 12 and turn 8.
- **Run 1 failed on semantics, not mechanics**: the model divided
  `TotalAssets` by 100 ("convert to 亿元") although the column already is in
  亿元 — the attached unit `亿元 (100M CNY)` was read as "needs conversion".
  The backend did exactly what it was asked; the agent's reading of a unit
  was wrong. No backend change follows from this.
- **Runs 2 and 3 never stopped because of the formatting clause** ("至多 4
  位小数、去尾 0、整数保留 1 位"; "报告期" read as date-without-time). After the
  correct export they kept trying to render the columns as text with
  `strftime(...)`, `format(x, '.4f')`, `substr`, `substring`, `||`, and every
  attempt was refused (`INVALID_TRANSFORM`). Same conclusion as the scripted
  layer, now with a model demonstrating it: value formatting belongs to an
  export-format specification (MADR 0003, step 5), not to the transform.
  The 30-turn cap, not the model, ended those runs.
- **Loose-intent shapes the model used that the backend rejected** (all
  general, all cheap to accept; MADR 0003 step 4 evidence):
  1. a list of single-key steps without `type`:
     `[{"filter": "..."}, {"select": [...]}, {"sort": "EndDate"}]` — 5 times
     across the runs;
  2. `{"type": "sort", "order": [{"field": "EndDate", "direction": "asc"}]}` —
     `order` as the key list (fixed in this commit);
  3. `"EndDate as ReportPeriod"` inside `select`, and `... as ReportPeriod`
     inside a derive expression — SQL-style aliasing;
  4. `derive` given as a list of `{name, expression}` objects;
  5. string/date functions outside the allowlist (`substr`, `strftime`,
     `format`, `||`) and `year()` on the `VARCHAR` date column from SQLite.
- **The export sandbox held**: run 3 tried `/tmp/ed_moneyauthoritybs.csv`,
  received `PERMISSION_DENIED` with the allowed root in the hint, and
  continued inside it. The first exploratory run (before the sandbox
  existed) had written a stray file into the benchmark's input directory —
  the reason the guard was added.
- Cost is negligible (≈ $0.004 per run at this gateway's rates); prompt
  tokens grow to ~0.5M per run because every tool response stays in
  context — a trace-compaction concern for the driver, not the backend.

Next: accept the shapes in 1–4 in the resolver, extend the expression
vocabulary (string slicing, temporal formatting) and give `export_result`
a format specification; then re-run this measurement.
