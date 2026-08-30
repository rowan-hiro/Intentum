"""DataSpace smoke test: drive benchmark tasks end to end through the MCP tool surface.

    uv run python examples/dataspace_smoke.py --task task_10 [--check]
    uv run python examples/dataspace_smoke.py --task all --check

Layer 1 of the smoke plan: a *scripted* agent (no LLM) issues the intents an
agent would issue — import the workspace, attach the knowledge document,
declare the shape of the answer, find the right dataset, materialize the
answer, export it — purely as MCP tool calls. The declaration is made first,
as the contract protocol asks (MADR 0007), so every export below is held to it. The resulting prediction.csv is scored with the official DataSpace
evaluator (examples/dataspace/evaluate.py) and, when the champion repository
is available, with its local column-signature scorer. The full tool-call
trace and both scores are written to smoke_result.json.

Environment:
    DATASPACE_BENCHMARK   benchmark package root (default ~/dev/kddcup2026_champion/DataSpace-Benchmark)
    KDDCUP_CHAMPION       champion repo root, for the comparison scorer (default ~/dev/kddcup2026_champion)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_backend import Backend  # noqa: E402
from agent_backend.mcp.server import create_server  # noqa: E402

EVALUATOR = ROOT / "examples" / "dataspace" / "evaluate.py"

Step = Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]]


# ----------------------------------------------------------------------------
# Scripted agents, one per task. Each step receives the trace so far and returns
# (tool, arguments); arguments may be derived from earlier responses exactly as a
# real agent would read them.
# ----------------------------------------------------------------------------

def _last(trace: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    return next(t["response"] for t in reversed(trace) if t["tool"] == tool)


def _cell(trace: list[dict[str, Any]], tool: str, column: str, row: int = 0) -> Any:
    """One value out of a preview, the way an agent reads its own result."""
    result = _last(trace, tool)["result"]
    index = [c["name"] for c in result["columns"]].index(column)
    return result["rows"][row][index]


def task_10_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """按报告期从早到晚列出货币当局资产负债表中总资产金额非空的记录，返回报告期和总资产金额（单位：亿元）。"""
    return [
        lambda trace: ("declare_output", {"columns": ["EndDate", "TotalAssets"], "rows": "at_least_one",
                                          "description": "reporting period and total assets, oldest first"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("attach_metadata", {"source": "knowledge.md"}),
        lambda trace: ("search_datasets", {"query": "monetary authority balance sheet total assets"}),
        lambda trace: ("describe_dataset", {"dataset": _last(trace, "search_datasets")["results"][0]["name"], "sample_rows": 2}),
        lambda trace: ("materialize_result", {
            "source": _last(trace, "search_datasets")["results"][0]["name"],
            "transform": {
                "filter": {"field": "total assets", "op": "not_null"},
                "select": ["enddate", "total assets"],
                "sort": "enddate",
            },
            "name": "task_10_answer",
            "description": "Monetary authority balance sheet records with non-null total assets, by reporting period",
        }),
        # The question prescribes the rendering: at most 4 decimals, no trailing
        # zeros, whole amounts keep one decimal. That is an export rule (MADR 0005).
        lambda trace: ("export_result", {
            "dataset": "task_10_answer", "path": str(prediction_path), "overwrite": True,
            "format_spec": {"decimals": 4, "strip_trailing_zeros": True, "integer_min_decimals": 1},
        }),
    ]


def task_44_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """What procedures did patient 025-44842 receive at the latest treatment timestamp, by treatment id?"""
    # The patient is only named in the cost table; treatments are linked through
    # cost.eventid, so every step below runs on the joined relation.
    linked = [
        {"join": {"right": "cost", "on": {"treatmentid": "eventid"}}},
        {"filter": "uniquepid = '025-44842' and eventtype = 'treatment'"},
    ]
    return [
        lambda trace: ("declare_output", {"columns": ["treatmentname"], "rows": "at_least_one",
                                          "description": "procedures at the latest treatment timestamp, in treatment id order"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("describe_dataset", {"dataset": "treatment", "sample_rows": 2}),
        lambda trace: ("transform_dataset", {
            "source": "treatment",
            "transform": linked + [{"sort": "-treatmenttime"}, {"limit": 1}],
        }),
        lambda trace: ("materialize_result", {
            "source": "treatment",
            "transform": linked + [
                {"filter": {"field": "treatmenttime", "op": "=", "value": _cell(trace, "transform_dataset", "treatmenttime")}},
                {"sort": "treatmentid"},
                {"select": ["treatmentname"]},
            ],
            "name": "task_44_answer",
            "description": "Procedures at the patient's latest treatment timestamp, in treatment id order",
        }),
        lambda trace: ("export_result", {"dataset": "task_44_answer", "path": str(prediction_path), "overwrite": True}),
    ]


def task_127_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """What was the maximum recorded respiration value for patient 027-146876 on 2103-07-12?"""
    return [
        lambda trace: ("declare_output", {"columns": ["maximum_respiration"], "rows": "one"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("describe_dataset", {"dataset": "vitalperiodic", "sample_rows": 2}),
        lambda trace: ("transform_dataset", {
            "source": "patient",
            "transform": {"filter": "uniquepid = '027-146876'", "select": ["patientunitstayid"]},
        }),
        lambda trace: ("materialize_result", {
            "source": "vitalperiodic",
            "transform": [
                {"filter": f"patientunitstayid = {_cell(trace, 'transform_dataset', 'patientunitstayid')} "
                           f"and strftime(observationtime, '%Y-%m-%d') = '2103-07-12'"},
                {"aggregate": [{"function": "max", "field": "respiration", "alias": "maximum_respiration"}]},
            ],
            "name": "task_127_answer",
            "description": "Maximum respiration recorded for the patient on 2103-07-12",
        }),
        lambda trace: ("export_result", {"dataset": "task_127_answer", "path": str(prediction_path), "overwrite": True}),
    ]


def task_329_script(context_dir: Path, prediction_path: Path) -> list[Step]:
    """Daily maximum enteral formula volume/bolus amt (ml) for patient 033-22108 in the current encounter."""
    label = "enteral formula volume/bolus amt (ml)"
    return [
        lambda trace: ("declare_output", {"columns": ["daily_maximum"], "rows": "one"}),
        lambda trace: ("import_workspace", {"path": str(context_dir)}),
        lambda trace: ("transform_dataset", {
            "source": "patient",
            "transform": {"filter": "uniquepid = '033-22108'", "select": ["patientunitstayid"]},
        }),
        # Two managed datasets: the daily totals, then the maximum over them.
        lambda trace: ("materialize_result", {
            "source": "intakeoutput",
            "transform": [
                {"filter": f"patientunitstayid = {_cell(trace, 'transform_dataset', 'patientunitstayid')} "
                           f"and celllabel = '{label}'"},
                {"aggregate": {"group_by": ["date(intakeoutputtime) as day"],
                               "measures": [{"function": "sum", "field": "cellvaluenumeric", "alias": "daily_total"}]}},
            ],
            "name": "task_329_daily_totals",
            "description": "Enteral formula volume per calendar day for the patient",
        }),
        lambda trace: ("materialize_result", {
            "source": "task_329_daily_totals",
            "transform": {"aggregate": [{"function": "max", "field": "daily_total", "alias": "daily_maximum"}]},
            "name": "task_329_answer",
            "description": "Largest daily enteral formula volume for the patient",
        }),
        lambda trace: ("export_result", {"dataset": "task_329_answer", "path": str(prediction_path), "overwrite": True}),
    ]


SCRIPTS: dict[str, Callable[[Path, Path], list[Step]]] = {
    "task_10": task_10_script,
    "task_44": task_44_script,
    "task_127": task_127_script,
    "task_329": task_329_script,
}


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------

async def run_script(server, steps: list[Step]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        tool, arguments = step(trace)
        started = time.perf_counter()
        result = await server.call_tool(tool, arguments)
        response = result.structured_content or {}
        entry = {
            "step": index,
            "tool": tool,
            "arguments": arguments,
            "status": response.get("status"),
            "code": response.get("code"),
            "summary": response.get("summary") or response.get("message"),
            "resolution": response.get("resolution"),
            "elapsed_s": round(time.perf_counter() - started, 3),
            "response": response,
        }
        trace.append(entry)
        print(f"[{index}] {tool} → {entry['status']}" + (f" ({entry['code']})" if entry["code"] else "") + f": {entry['summary']}")
        if entry["status"] not in ("success", "partial"):
            print("    stopping: the scripted agent has no repair strategy for this response")
            break
    return trace


def run_official_evaluator(pred_root: Path, benchmark: Path, task: str, out_dir: Path) -> dict[str, Any]:
    config_root = out_dir / "configs"
    config_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(benchmark / "evaluation" / "configs" / f"{task}.json", config_root / f"{task}.json")
    summary_path = out_dir / "evaluation_summary.json"
    cmd = [sys.executable, str(EVALUATOR), "--prediction-root", str(pred_root), "--gold-root", str(benchmark / "output"),
           "--config-root", str(config_root), "--output", str(summary_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    return {"command": " ".join(cmd), "returncode": proc.returncode, "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-2000:], "summary": summary}


def run_champion_scorer(pred_root: Path, benchmark: Path, task: str, champion: Path) -> dict[str, Any] | None:
    scorer = champion / "src" / "data_agent_baseline" / "run_compare_to_gt.py"
    if not scorer.exists():
        return None
    code = (
        "import json, sys; sys.path.insert(0, %r); from run_compare_to_gt import score_task; "
        "from pathlib import Path; print(json.dumps(score_task(%r, Path(%r), Path(%r), write_score=False), default=str))"
        % (str(scorer.parent), task, str(benchmark / "output"), str(pred_root))
    )
    proc = subprocess.run(["uv", "run", "--directory", str(champion), "python", "-c", code], capture_output=True, text=True)
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        result = None
    return {"returncode": proc.returncode, "result": result, "stderr": proc.stderr[-1000:] if result is None else ""}


def official_verdict(summary: dict[str, Any], task: str) -> dict[str, Any] | None:
    """The task's entry in the evaluator summary (``passed``, row/column counts, error)."""
    for entry in summary.get("tasks", []):
        if entry.get("task_id") == task:
            return entry
    return None


def run_task(task: str, benchmark: Path, champion: Path, out_root: Path | None) -> dict[str, Any]:
    context_dir = benchmark / "input" / task / "context"
    if not context_dir.is_dir():
        raise SystemExit(f"benchmark task not found: {context_dir}")
    question = json.loads((benchmark / "input" / task / "task.json").read_text(encoding="utf-8"))["question"]

    out_dir = out_root if out_root is not None else ROOT / "examples" / "dataspace" / "runs" / task
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
    parser.add_argument("--benchmark", default=os.environ.get("DATASPACE_BENCHMARK",
                        str(Path("~/dev/kddcup2026_champion/DataSpace-Benchmark").expanduser())))
    parser.add_argument("--champion", default=os.environ.get("KDDCUP_CHAMPION", str(Path("~/dev/kddcup2026_champion").expanduser())))
    parser.add_argument("--out", default=None, help="output directory (default examples/dataspace/runs/<task>)")
    parser.add_argument("--check", action="store_true", help="exit non-zero unless the official evaluator marks every task correct")
    args = parser.parse_args()

    tasks = sorted(SCRIPTS, key=lambda t: int(t.split("_")[1])) if args.task == "all" else [t.strip() for t in args.task.split(",")]
    unknown = [t for t in tasks if t not in SCRIPTS]
    if unknown:
        print(f"no scripted agent for {unknown}; available: {sorted(SCRIPTS)}")
        return 2

    benchmark, champion = Path(args.benchmark), Path(args.champion)
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
