"""DataSpace smoke test, layer 2: a real model drives one task through the MCP tools.

    uv run python -m agent_harness.scenarios.dataspace.agent --task task_10 --runs 3

A deliberately thin agent loop (no framework, no SQL, no dialect rules): the
model sees the MCP server's own instructions, the task framing, the question
and the tool schemas; every tool call is forwarded to the MCP server and the
structured response is handed back verbatim (truncated only for size). The
prediction it exports is scored with the official evaluator; per-run traces
(messages, tool calls, usage, cost) and an aggregated summary are written
under runs/<task>/agent/.

Settings (environment or the repository .env):
    DEFAULT_MODEL_API_URL, DEFAULT_MODEL_API_KEY, DEFAULT_MODEL_NAME
    DATASPACE_BENCHMARK, KDDCUP_CHAMPION
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from agent_backend import Backend
from agent_backend.mcp.server import INSTRUCTIONS, create_server

from agent_harness.config import model_config
from agent_harness.loop import run_tool_loop
from agent_harness.scenarios.dataspace import RUNS, benchmark_root, champion_root, task_context, task_question
from agent_harness.scenarios.dataspace.framing import DELIVERED_PROMPT, NUDGE_PROMPT, TASK_FRAMING
from agent_harness.scenarios.dataspace.scoring import official_verdict, run_champion_scorer, run_official_evaluator


def convergence(tool_events: list[dict[str, Any]], window: int = 2) -> dict[str, Any]:
    """How the backend's refusals were taken up (MADR 0010).

    A refusal is an error or needs_resolution response; it is unadvised when an
    error carries no advice; it is repaired when a call of the same tool succeeds
    within ``window`` later calls.
    """
    refusals = [i for i, e in enumerate(tool_events) if e["status"] in ("error", "needs_resolution")]
    unadvised = [i for i in refusals if tool_events[i]["status"] == "error" and not tool_events[i].get("advice")]
    repaired = [i for i in refusals if any(
        later["tool"] == tool_events[i]["tool"] and later["status"] == "success"
        for later in tool_events[i + 1:i + 1 + window])]
    advice_kinds: Counter = Counter(kind for e in tool_events for kind in (e.get("advice") or []))
    return {
        "refusals": len(refusals),
        "unadvised_refusals": len(unadvised),
        "repaired_refusals": len(repaired),
        "repair_rate": round(len(repaired) / len(refusals), 3) if refusals else None,
        "advice_kinds": dict(advice_kinds),
    }


async def run_once(task: str, question: str, benchmark: Path, run_dir: Path, config: dict[str, str], *,
                   max_turns: int, max_tool_chars: int, max_tokens: int) -> dict[str, Any]:
    context_dir = task_context(benchmark, task)
    pred_root = run_dir / "predictions"
    prediction_path = pred_root / task / "prediction.csv"
    backend = Backend(run_dir / "workspace", export_root=run_dir)  # exports are confined to the run directory
    try:
        server = create_server(backend)
        loop = await run_tool_loop(
            server, config,
            system_prompt=INSTRUCTIONS + "\n" + TASK_FRAMING.format(context_dir=context_dir, prediction_path=prediction_path),
            user_prompt=question, deliverable=prediction_path,
            delivered_prompt=DELIVERED_PROMPT, nudge_prompt=NUDGE_PROMPT,
            max_turns=max_turns, max_tool_chars=max_tool_chars, max_tokens=max_tokens,
        )
        integrity = backend.integrity_report()
    finally:
        backend.close()

    result: dict[str, Any] = {
        "task": task, "model": config["model"], "stop_reason": loop.stop_reason, "turns": len(loop.turns),
        "tool_calls": len(loop.tool_events), "elapsed_s": loop.elapsed_s, "usage": loop.usage, "cost_usd": loop.cost_usd,
        "prediction": str(prediction_path) if prediction_path.exists() else None,
        "tool_histogram": dict(Counter(e["tool"] for e in loop.tool_events)),
        "status_histogram": dict(Counter(f"{e['status']}:{e['code']}" if e["code"] else str(e["status"]) for e in loop.tool_events)),
        "integrity_ok": integrity["ok"], "stall_nudges": loop.stall_nudges, "convergence": convergence(loop.tool_events),
        "tool_events": loop.tool_events, "turn_log": loop.turns,
    }
    if prediction_path.exists():
        official = run_official_evaluator(pred_root, benchmark, task, run_dir)
        entry = official_verdict(official["summary"], task) or {}
        result["official"] = {"passed": bool(entry.get("passed")), "task": entry, "returncode": official["returncode"]}
        champion = run_champion_scorer(pred_root, benchmark, task, champion_root())
        result["champion_scorer"] = (champion or {}).get("result")
    else:
        result["official"] = {"passed": False, "task": None}
    (run_dir / "messages.json").write_text(json.dumps(loop.messages, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_dir / "run_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def mean(values: list[float], digits: int = 2) -> float | None:
        return round(statistics.fmean(values), digits) if values else None

    codes: Counter = Counter()
    tools: Counter = Counter()
    advice_kinds: Counter = Counter()
    for r in results:
        for key, count in r["status_histogram"].items():
            codes[key] += count
        for key, count in r["tool_histogram"].items():
            tools[key] += count
        for key, count in r["convergence"]["advice_kinds"].items():
            advice_kinds[key] += count
    refusals = sum(r["convergence"]["refusals"] for r in results)
    repaired = sum(r["convergence"]["repaired_refusals"] for r in results)
    return {
        "runs": len(results),
        "passed": sum(1 for r in results if r["official"]["passed"]),
        "pass_rate": round(sum(1 for r in results if r["official"]["passed"]) / len(results), 3) if results else None,
        "turns": {"mean": mean([r["turns"] for r in results]), "values": [r["turns"] for r in results]},
        "tool_calls": {"mean": mean([r["tool_calls"] for r in results]), "values": [r["tool_calls"] for r in results]},
        "prompt_tokens_mean": mean([r["usage"].get("prompt_tokens", 0) for r in results]),
        "completion_tokens_mean": mean([r["usage"].get("completion_tokens", 0) for r in results]),
        "reasoning_tokens_mean": mean([r["usage"].get("reasoning_tokens", 0) for r in results]),
        "cost_usd_mean": mean([r["cost_usd"] for r in results], 6),
        "elapsed_s_mean": mean([r["elapsed_s"] for r in results]),
        "stop_reasons": dict(Counter(r["stop_reason"] for r in results)),
        "tool_status_codes": dict(codes),
        "tool_usage": dict(tools),
        "refusals": refusals,
        "unadvised_refusals": sum(r["convergence"]["unadvised_refusals"] for r in results),
        "repair_rate": round(repaired / refusals, 3) if refusals else None,
        "advice_kinds": dict(advice_kinds),
        "stall_nudges": sum(r["stall_nudges"] for r in results),
        "champion_scores": [(r.get("champion_scorer") or {}).get("score") for r in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DataSpace smoke test with a real model through the MCP tools")
    parser.add_argument("--task", default="task_10")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-tool-chars", type=int, default=8000)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--benchmark", default=None, help="benchmark package root (default: $DATASPACE_BENCHMARK)")
    parser.add_argument("--out", default=None, help="output directory (default runs/<task>/agent beside this module)")
    parser.add_argument("--check-config", action="store_true", help="only validate model configuration and benchmark paths")
    args = parser.parse_args()

    benchmark = benchmark_root(args.benchmark)
    context_dir = task_context(benchmark, args.task)
    if not context_dir.is_dir():
        print(f"benchmark task not found: {context_dir}")
        return 2
    config = model_config()
    question = task_question(benchmark, args.task)
    print(f"task {args.task} · model {config['model']} · {question}")
    if args.check_config or args.runs <= 0:
        print("configuration ok")
        return 0

    out_dir = Path(args.out) if args.out else RUNS / args.task / "agent"
    results: list[dict[str, Any]] = []
    for index in range(1, args.runs + 1):
        run_dir = out_dir / f"run_{index}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        print(f"\n=== run {index}/{args.runs} ===")
        result = asyncio.run(run_once(args.task, question, benchmark, run_dir, config, max_turns=args.max_turns,
                                      max_tool_chars=args.max_tool_chars, max_tokens=args.max_tokens))
        results.append(result)
        c = result["convergence"]
        print(f"  → passed={result['official']['passed']} turns={result['turns']} tool_calls={result['tool_calls']} "
              f"tokens={result['usage'].get('total_tokens')} cost=${result['cost_usd']:.4f} stop={result['stop_reason']} "
              f"refusals={c['refusals']} unadvised={c['unadvised_refusals']} repair_rate={c['repair_rate']} nudges={result['stall_nudges']}")
        summary = summarize(results)
        (out_dir / "agent_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== summary ===")
    print(json.dumps(summarize(results), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
