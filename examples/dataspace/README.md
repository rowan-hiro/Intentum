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
`~/dev/kddcup2026_champion/DataSpace-Benchmark`.

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
