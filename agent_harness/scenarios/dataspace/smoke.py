"""DataSpace smoke test, layer 1: scripted agents drive benchmark tasks end to end through the MCP tool surface.

    uv run python -m agent_harness.scenarios.dataspace.smoke --task task_10 [--check]
    uv run python -m agent_harness.scenarios.dataspace.smoke --task all --check

A scripted agent (no model) issues the intents an agent would issue: declare
the shape of the answer, import the workspace, attach the knowledge document,
find the right dataset, materialize the answer, export it. The resulting
prediction.csv is scored with the official evaluator (evaluate.py) and, when
the champion repository is available, with its local column-signature scorer.
The full tool-call trace and both scores are written to smoke_result.json
under runs/<task>/scripted/, a directory this layer owns; the model-driven
layer writes under runs/<task>/agent/ and neither touches the other's.

Settings (environment or the repository .env):
    DATASPACE_BENCHMARK   benchmark package root (default $DATASPACE_BENCHMARK)
    KDDCUP_CHAMPION       champion repo root, for the comparison scorer (default $KDDCUP_CHAMPION)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from agent_backend import Backend
from agent_backend.mcp.server import create_server

from agent_harness.scenarios.dataspace import RUNS, benchmark_root, champion_root, task_context, task_question
from agent_harness.scenarios.dataspace.scoring import official_verdict, run_champion_scorer, run_official_evaluator
from agent_harness.scenarios.dataspace.scripted import SCRIPTS, run_script


def run_task(task: str, benchmark: Path, champion: Path, out_root: Path | None) -> dict[str, Any]:
    context_dir = task_context(benchmark, task)
    if not context_dir.is_dir():
        raise SystemExit(f"benchmark task not found: {context_dir}")
    question = task_question(benchmark, task)

    out_dir = out_root if out_root is not None else RUNS / task / "scripted"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    pred_root = out_dir / "predictions"
    prediction_path = pred_root / task / "prediction.csv"

    print(f"\n=== {task}: {question}")
    backend = Backend(out_dir / "workspace", export_root=out_dir)
    started = time.perf_counter()
    try:
        server = create_server(backend)
        trace = asyncio.run(run_script(server, SCRIPTS[task](context_dir, prediction_path)))
        integrity = backend.integrity_report()
    finally:
        backend.close()
    elapsed = round(time.perf_counter() - started, 2)

    result: dict[str, Any] = {
        "task": task,
        "question": question,
        "mode": "scripted",
        "tool_calls": len(trace),
        "elapsed_s": elapsed,
        "prediction": str(prediction_path) if prediction_path.exists() else None,
        "integrity": integrity,
        "trace": trace,
    }
    if prediction_path.exists():
        official = run_official_evaluator(pred_root, benchmark, task, out_dir)
        entry = official_verdict(official["summary"], task) or {}
        result["official"] = {"correct": bool(entry.get("passed")), "task_accuracy": official["summary"].get("task_accuracy"),
                              "task": entry, "returncode": official["returncode"], "summary": official["summary"]}
        print(f"official evaluator: passed={entry.get('passed')} rows={entry.get('prediction_row_count')}/{entry.get('gold_row_count')} "
              f"error={entry.get('error')} task_accuracy={official['summary'].get('task_accuracy')}")
        if official["returncode"] != 0:
            print(official["stderr"] or official["stdout"])
        result["champion_scorer"] = run_champion_scorer(pred_root, benchmark, task, champion)
        scored = (result["champion_scorer"] or {}).get("result")
        if scored:
            print(f"champion scorer: score={scored.get('score')} recall={scored.get('recall')} "
                  f"matched={scored.get('matched_cols')}/{scored.get('gold_cols')} pred_cols={scored.get('pred_cols')}")
        elif result["champion_scorer"]:
            print("champion scorer unavailable:", result["champion_scorer"].get("stderr", "")[-300:])
    else:
        print("no prediction produced")
        result["official"] = {"correct": False}

    (out_dir / "smoke_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out_dir / 'smoke_result.json'}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="DataSpace smoke test through the MCP tool surface")
    parser.add_argument("--task", default="task_10", help="task id, comma-separated ids, or 'all'")
    parser.add_argument("--benchmark", default=None, help="benchmark package root (default: $DATASPACE_BENCHMARK)")
    parser.add_argument("--champion", default=None, help="champion repository root (default: $KDDCUP_CHAMPION)")
    parser.add_argument("--out", default=None, help="output directory (default runs/<task>/scripted beside this module)")
    parser.add_argument("--check", action="store_true", help="exit non-zero unless the official evaluator marks every task correct")
    args = parser.parse_args()

    tasks = sorted(SCRIPTS, key=lambda t: int(t.split("_")[1])) if args.task == "all" else [t.strip() for t in args.task.split(",")]
    unknown = [t for t in tasks if t not in SCRIPTS]
    if unknown:
        print(f"no scripted agent for {unknown}; available: {sorted(SCRIPTS)}")
        return 2

    benchmark, champion = benchmark_root(args.benchmark), champion_root(args.champion)
    results = [run_task(t, benchmark, champion, Path(args.out) / t if args.out else None) for t in tasks]
    if len(results) > 1:
        print("\n=== summary ===")
        for result in results:
            print(f"  {result['task']}: passed={result['official'].get('correct')} "
                  f"tool_calls={result['tool_calls']} elapsed={result['elapsed_s']}s")
    if args.check:
        return 0 if all(r["official"].get("correct") for r in results) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
