"""DataSpace smoke test, layer 2: a real model drives one task through the MCP tools.

    uv run python examples/dataspace_agent.py --task task_10 --runs 3

A deliberately thin agent loop (no framework, no SQL, no dialect rules): the
model sees the MCP server's own instructions, the task question, the workspace
path and the tool schemas; every tool call is forwarded to the MCP server and
the structured response is handed back verbatim (truncated only for size). The
prediction it exports is scored with the official evaluator; per-run traces
(messages, tool calls, usage, cost) and an aggregated summary are written under
examples/dataspace/runs/<task>/agent/.

Model configuration (OpenAI-compatible chat completions with tool calling):
    DEFAULT_MODEL_API_URL, DEFAULT_MODEL_API_KEY, DEFAULT_MODEL_NAME
read from the environment or from a .env file in the repository root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

from agent_backend import Backend  # noqa: E402
from agent_backend.mcp.server import INSTRUCTIONS, create_server  # noqa: E402
from dataspace_smoke import official_verdict, run_champion_scorer, run_official_evaluator  # noqa: E402

TASK_FRAMING = """\
You are a data agent solving one analytics task. Work only through the tools.

- The task workspace directory is: {context_dir}
  It contains csv/json/sqlite files and a knowledge.md that describes them. Import it first.
- Produce the answer as a table with exactly the columns the question asks for, then write it with
  export_result to this path (overwrite allowed): {prediction_path}
- When the file has been written, reply with the single word DONE.
"""


# ----------------------------------------------------------------------------
# configuration
# ----------------------------------------------------------------------------

def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def model_config() -> dict[str, str]:
    env = {**load_env_file(ROOT / ".env"), **{k: v for k, v in os.environ.items() if k.startswith("DEFAULT_MODEL_")}}
    missing = [k for k in ("DEFAULT_MODEL_API_URL", "DEFAULT_MODEL_API_KEY", "DEFAULT_MODEL_NAME") if not env.get(k)]
    if missing:
        raise SystemExit(f"missing model configuration: {missing} (set them in the environment or in {ROOT / '.env'})")
    return {"url": env["DEFAULT_MODEL_API_URL"].rstrip("/"), "key": env["DEFAULT_MODEL_API_KEY"], "model": env["DEFAULT_MODEL_NAME"]}


# ----------------------------------------------------------------------------
# model client (stdlib only)
# ----------------------------------------------------------------------------

def chat(config: dict[str, str], messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int,
         timeout: int = 180, attempts: int = 3) -> dict[str, Any]:
    payload = {"model": config["model"], "messages": messages, "tools": tools, "tool_choice": "auto", "max_tokens": max_tokens}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            f"{config['url']}/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {config['key']}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")[:500]
            last_error = RuntimeError(f"HTTP {err.code}: {detail}")
            if err.code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(2 * attempt)
                continue
            raise last_error from err
        except (urllib.error.URLError, TimeoutError) as err:
            last_error = err
            if attempt < attempts:
                time.sleep(2 * attempt)
                continue
            raise
    raise last_error or RuntimeError("chat failed")


def openai_tools(mcp_tools) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}}
        for t in mcp_tools
    ]


# ----------------------------------------------------------------------------
# one run
# ----------------------------------------------------------------------------

async def run_once(task: str, question: str, context_dir: Path, run_dir: Path, config: dict[str, str], *,
                   max_turns: int, max_tool_chars: int, max_tokens: int) -> dict[str, Any]:
    pred_root = run_dir / "predictions"
    prediction_path = pred_root / task / "prediction.csv"
    backend = Backend(run_dir / "workspace", export_root=run_dir)  # exports are confined to the run directory
    server = create_server(backend)
    tools = openai_tools(await server.list_tools())

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": INSTRUCTIONS + "\n" + TASK_FRAMING.format(context_dir=context_dir, prediction_path=prediction_path)},
        {"role": "user", "content": question},
    ]
    turns: list[dict[str, Any]] = []
    usage_total = Counter()
    cost_total = 0.0
    tool_events: list[dict[str, Any]] = []
    stop_reason = "max_turns"
    nudges = 0
    exported_once = False
    started = time.perf_counter()

    try:
        for turn in range(1, max_turns + 1):
            t0 = time.perf_counter()
            reply = chat(config, messages, tools, max_tokens=max_tokens)
            usage = reply.get("usage") or {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage_total[key] += int(usage.get(key) or 0)
            usage_total["reasoning_tokens"] += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            cost_total += float(usage.get("cost") or 0.0)
            message = (reply.get("choices") or [{}])[0].get("message") or {}
            assistant: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            if message.get("tool_calls"):
                assistant["tool_calls"] = message["tool_calls"]
            if message.get("reasoning_content"):
                assistant["reasoning_content"] = message["reasoning_content"]
            messages.append(assistant)
            record: dict[str, Any] = {"turn": turn, "content": (message.get("content") or "")[:2000], "tool_calls": [],
                                      "usage": usage, "elapsed_s": round(time.perf_counter() - t0, 2)}

            calls = message.get("tool_calls") or []
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                raw_args = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError as err:
                    response: dict[str, Any] = {"status": "error", "code": "INVALID_INTENT",
                                                "message": f"tool arguments were not valid JSON: {err}", "recoverable": True}
                else:
                    c0 = time.perf_counter()
                    try:
                        result = await server.call_tool(name, arguments)
                        response = result.structured_content or {"status": "error", "code": "INTERNAL", "message": "empty tool result"}
                    except Exception as err:  # protocol-level validation errors from the MCP SDK
                        response = {"status": "error", "code": "INVALID_INTENT", "message": str(err)[:800], "recoverable": True}
                    tool_events.append({"turn": turn, "tool": name, "arguments": arguments, "status": response.get("status"),
                                        "code": response.get("code"), "summary": response.get("summary") or response.get("message"),
                                        "resolution": response.get("resolution"), "elapsed_s": round(time.perf_counter() - c0, 3)})
                text = json.dumps(response, ensure_ascii=False, default=str)
                if len(text) > max_tool_chars:
                    text = text[:max_tool_chars] + f'... [truncated {len(text) - max_tool_chars} chars]'
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": text})
                record["tool_calls"].append({"tool": name, "status": response.get("status"), "code": response.get("code")})
            turns.append(record)
            print(f"  turn {turn}: " + (", ".join(f"{c['tool']}→{c['status']}" + (f"({c['code']})" if c["code"] else "") for c in record["tool_calls"])
                                     or f"text: {(message.get('content') or '')[:120]!r}"))

            exported_now = any(
                e["turn"] == turn and e["tool"] == "export_result" and e["status"] == "success"
                and Path(str(e["arguments"].get("path", ""))).expanduser().resolve() == prediction_path.resolve()
                for e in tool_events
            )
            if exported_now and not exported_once:
                exported_once = True
                messages.append({"role": "user", "content": "The prediction file has been written. Reply DONE if you are finished, or continue if you still want to change it."})
                continue

            if not calls:
                content = (message.get("content") or "").strip()
                if "DONE" in content.upper() or (prediction_path.exists() and content):
                    stop_reason = "done"
                    break
                if nudges >= 2:
                    stop_reason = "no_tool_calls"
                    break
                nudges += 1
                messages.append({"role": "user", "content": "Continue with the tools. Reply DONE only after export_result has written the prediction file."})
        integrity = backend.integrity_report()
    finally:
        backend.close()

    elapsed = round(time.perf_counter() - started, 1)
    result: dict[str, Any] = {
        "task": task, "model": config["model"], "stop_reason": stop_reason, "turns": len(turns),
        "tool_calls": len(tool_events), "elapsed_s": elapsed, "usage": dict(usage_total), "cost_usd": round(cost_total, 6),
        "prediction": str(prediction_path) if prediction_path.exists() else None,
        "tool_histogram": dict(Counter(e["tool"] for e in tool_events)),
        "status_histogram": dict(Counter(f"{e['status']}:{e['code']}" if e["code"] else str(e["status"]) for e in tool_events)),
        "integrity_ok": integrity["ok"], "tool_events": tool_events, "turn_log": turns,
    }
    if prediction_path.exists():
        official = run_official_evaluator(pred_root, context_dir.parent.parent.parent, task, run_dir)
        entry = official_verdict(official["summary"], task) or {}
        result["official"] = {"passed": bool(entry.get("passed")), "task": entry, "returncode": official["returncode"]}
        champion = run_champion_scorer(pred_root, context_dir.parent.parent.parent, task, Path(os.environ.get("KDDCUP_CHAMPION", str(Path("$KDDCUP_CHAMPION").expanduser()))))
        result["champion_scorer"] = (champion or {}).get("result")
    else:
        result["official"] = {"passed": False, "task": None}
    (run_dir / "messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run_dir / "run_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def mean(values: list[float], digits: int = 2) -> float | None:
        return round(statistics.fmean(values), digits) if values else None

    codes: Counter = Counter()
    tools: Counter = Counter()
    for r in results:
        for key, count in r["status_histogram"].items():
            codes[key] += count
        for key, count in r["tool_histogram"].items():
            tools[key] += count
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
        "champion_scores": [(r.get("champion_scorer") or {}).get("score") for r in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DataSpace smoke test with a real model through the MCP tools")
    parser.add_argument("--task", default="task_10")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-tool-chars", type=int, default=8000)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--benchmark", default=os.environ.get("DATASPACE_BENCHMARK",
                        str(Path("$DATASPACE_BENCHMARK").expanduser())))
    parser.add_argument("--out", default=None)
    parser.add_argument("--check-config", action="store_true", help="only validate model configuration and benchmark paths")
    args = parser.parse_args()

    benchmark = Path(args.benchmark)
    context_dir = benchmark / "input" / args.task / "context"
    if not context_dir.is_dir():
        print(f"benchmark task not found: {context_dir}")
        return 2
    config = model_config()
    question = json.loads((benchmark / "input" / args.task / "task.json").read_text(encoding="utf-8"))["question"]
    print(f"task {args.task} · model {config['model']} · {question}")
    if args.check_config or args.runs <= 0:
        print("configuration ok")
        return 0

    out_dir = Path(args.out) if args.out else ROOT / "examples" / "dataspace" / "runs" / args.task / "agent"
    results: list[dict[str, Any]] = []
    for index in range(1, args.runs + 1):
        run_dir = out_dir / f"run_{index}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        print(f"\n=== run {index}/{args.runs} ===")
        result = asyncio.run(run_once(args.task, question, context_dir, run_dir, config, max_turns=args.max_turns,
                                      max_tool_chars=args.max_tool_chars, max_tokens=args.max_tokens))
        results.append(result)
        print(f"  → passed={result['official']['passed']} turns={result['turns']} tool_calls={result['tool_calls']} "
              f"tokens={result['usage'].get('total_tokens')} cost=${result['cost_usd']:.4f} stop={result['stop_reason']}")
        summary = summarize(results)
        (out_dir / "agent_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== summary ===")
    print(json.dumps(summarize(results), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
