"""DataSpace smoke test, layer 2: a real model drives one task through the MCP tools.

    uv run python -m agent_harness.scenarios.dataspace.agent --task task_10 --runs 3
    uv run python -m agent_harness.scenarios.dataspace.agent --task task_44 --runs 3 --declaration informed
    uv run python -m agent_harness.scenarios.dataspace.agent --task task_44 --runs 3 --host opencode

Two hosts can run the model: OpenCode in a container with only the backend's MCP
tools enabled, with opt-in video-frame tools via --video (--host opencode,
the default since MADR 0011; the image from
agent_harness/hosts/opencode.Dockerfile), or the in-process reference loop below
(--host loop, the control arm); both produce the same run record.

A deliberately thin agent loop (no framework, no SQL, no dialect rules): the
model sees the MCP server's own instructions, the task framing, the question
and the tool schemas; every tool call is forwarded to the MCP server and the
structured response is handed back verbatim (truncated only for size). The
prediction it exports is scored with the official evaluator; per-run traces
(messages, tool calls, usage, cost) and an aggregated summary are written
under runs/<task>/<host>-<declaration>/ (the loop keeps its historical names agent/
and agent-informed/); --declaration informed is the default since the 2026-09-02
timing experiment (see framing.py).

Settings (environment or the repository .env):
    DEFAULT_MODEL_API_URL, DEFAULT_MODEL_API_KEY, DEFAULT_MODEL_NAME
    DATASPACE_BENCHMARK, KDDCUP_CHAMPION
    HARNESS_VIDEO, HARNESS_ASR_MODEL, HARNESS_OPENCODE_IMAGE
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

from agent_harness.config import boolean_setting, configured_asr_model, model_config, setting
from agent_harness.hosts.opencode import DEFAULT_IMAGE, DOCKER_DATA_DIR, DOCKER_RUN_DIR, container_path, run_opencode
from agent_harness.loop import run_tool_loop
from agent_harness.perception.server import AUDIO_INSTRUCTIONS, INSTRUCTIONS as VIDEO_INSTRUCTIONS
from agent_harness.perception.prepare_asr import verify_model
from agent_harness.scenarios.dataspace import RUNS, benchmark_root, champion_root, task_context, task_question
from agent_harness.scenarios.dataspace.framing import DELIVERED_PROMPT, FRAMINGS, NUDGE_PROMPT
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
                   max_turns: int, max_tool_chars: int, max_tokens: int, declaration: str = "fresh",
                   host: str = "opencode", host_timeout: int = 900, image: str = DEFAULT_IMAGE,
                   video: bool = False, asr_model: Path | None = None) -> dict[str, Any]:
    if asr_model is not None and not video:
        raise ValueError("Audio transcription requires --video.")
    if video and host != "opencode":
        raise ValueError("Video attachments currently require --host opencode.")
    context_dir = task_context(benchmark, task)
    pred_root = run_dir / "predictions"
    prediction_path = pred_root / task / "prediction.csv"
    messages: list[dict[str, Any]] | None = None
    turn_log: list[dict[str, Any]] = []
    host_version: str | None = None
    if host == "opencode":
        # OpenCode runs in a container and launches the backend's MCP server there, on this run's workspace;
        # the framing names the paths as the container sees them, exports land in run_dir on this side.
        # Perception can see this task's input only; no public answers or other
        # tasks are mounted into its container.
        mounts = {context_dir: f"{DOCKER_DATA_DIR}/context"} if video else {benchmark: DOCKER_DATA_DIR}
        video_context = container_path(context_dir, mounts) if video else None
        system_prompt = INSTRUCTIONS + "\n" + FRAMINGS[declaration].format(
            context_dir=container_path(context_dir, mounts),
            prediction_path=container_path(prediction_path, {run_dir: DOCKER_RUN_DIR}))
        if video:
            system_prompt += "\n" + VIDEO_INSTRUCTIONS
        if asr_model is not None:
            system_prompt += "\n" + AUDIO_INSTRUCTIONS
        ran = run_opencode(run_dir, prompt=question, system_prompt=system_prompt, settings=config, mounts=mounts,
                           image=image, timeout_s=host_timeout, title=task, video_context=video_context, asr_model=asr_model)
        tool_events, turns, usage, cost = ran.tool_events, ran.turns, ran.usage, ran.cost_usd
        stop_reason, elapsed, nudges = ran.stop_reason, ran.elapsed_s, 0
        host_version = ran.image
        backend = Backend(run_dir / "workspace", export_root=run_dir)
        try:
            integrity = backend.integrity_report()
        finally:
            backend.close()
    else:
        system_prompt = INSTRUCTIONS + "\n" + FRAMINGS[declaration].format(context_dir=context_dir, prediction_path=prediction_path)
        backend = Backend(run_dir / "workspace", export_root=run_dir)  # exports are confined to the run directory
        try:
            server = create_server(backend)
            loop = await run_tool_loop(
                server, config, system_prompt=system_prompt, user_prompt=question, deliverable=prediction_path,
                delivered_prompt=DELIVERED_PROMPT, nudge_prompt=NUDGE_PROMPT,
                max_turns=max_turns, max_tool_chars=max_tool_chars, max_tokens=max_tokens,
            )
            integrity = backend.integrity_report()
        finally:
            backend.close()
        tool_events, turns, usage, cost = loop.tool_events, len(loop.turns), loop.usage, loop.cost_usd
        stop_reason, elapsed, nudges = loop.stop_reason, loop.elapsed_s, loop.stall_nudges
        messages, turn_log = loop.messages, loop.turns

    declared = [e["turn"] for e in tool_events if e["tool"] == "declare_output" and e["status"] == "success"]
    result: dict[str, Any] = {
        "task": task, "model": config["model"], "host": host, "host_version": host_version, "declaration": declaration,
        "model_gateway": config["url"], "asr_model": str(asr_model.resolve()) if asr_model else None,
        "perception": "video_frames_and_audio" if asr_model is not None else "video_frames" if video else None,
        "declaration_turn": declared[0] if declared else None,
        "stop_reason": stop_reason, "turns": turns,
        "tool_calls": len(tool_events), "elapsed_s": elapsed, "usage": usage, "cost_usd": cost,
        "prediction": str(prediction_path) if prediction_path.exists() else None,
        "tool_histogram": dict(Counter(e["tool"] for e in tool_events)),
        "status_histogram": dict(Counter(f"{e['status']}:{e['code']}" if e["code"] else str(e["status"]) for e in tool_events)),
        "integrity_ok": integrity["ok"], "stall_nudges": nudges, "convergence": convergence(tool_events),
        "tool_events": tool_events, "turn_log": turn_log,
    }
    if prediction_path.exists():
        official = run_official_evaluator(pred_root, benchmark, task, run_dir)
        entry = official_verdict(official["summary"], task) or {}
        result["official"] = {"passed": bool(entry.get("passed")), "task": entry, "returncode": official["returncode"]}
        champion = run_champion_scorer(pred_root, benchmark, task, champion_root())
        result["champion_scorer"] = (champion or {}).get("result")
    else:
        result["official"] = {"passed": False, "task": None}
    if messages is not None:
        (run_dir / "messages.json").write_text(json.dumps(messages, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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
        "host": results[0].get("host") if results else None,
        "host_version": results[0].get("host_version") if results else None,
        "declaration": results[0]["declaration"] if results else None,
        "perception": results[0].get("perception") if results else None,
        "declaration_turns": [r.get("declaration_turn") for r in results],
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
    parser.add_argument("--host", choices=["loop", "opencode"], default="opencode",
                        help="who runs the model loop: OpenCode in a container from --image (default), or the in-process reference loop")
    parser.add_argument("--image", default=None, help="Docker image (default: $HARNESS_OPENCODE_IMAGE or the pinned host image)")
    parser.add_argument("--host-timeout", type=int, default=900, help="seconds a container run may take before it is killed")
    parser.add_argument("--video", action=argparse.BooleanOptionalAction, default=None,
                        help="enable video-frame tools and model image inputs (default: $HARNESS_VIDEO, otherwise off; OpenCode only)")
    parser.add_argument("--asr-model", type=Path, help="also transcribe audio using this prepared offline model directory (requires --video)")
    parser.add_argument("--no-audio", action="store_true", help="disable configured ASR for a frame-only run")
    parser.add_argument("--declaration", choices=sorted(FRAMINGS), default="informed",
                        help="when the framing asks for declare_output: fresh (first call) or informed (once a preview shows the answer)")
    parser.add_argument("--out", default=None, help="output directory (default runs/<task>/agent or agent-informed beside this module)")
    parser.add_argument("--check-config", action="store_true", help="validate settings, benchmark paths and ASR weight hashes without starting a model run")
    args = parser.parse_args()
    try:
        if args.video is None:
            args.video = boolean_setting("HARNESS_VIDEO")
        args.image = args.image or setting("HARNESS_OPENCODE_IMAGE", DEFAULT_IMAGE)
        if args.no_audio and args.asr_model is not None:
            parser.error("--no-audio cannot be combined with --asr-model")
        if args.asr_model is not None:
            args.asr_model = args.asr_model.expanduser()
        elif args.video and not args.no_audio:
            args.asr_model = configured_asr_model()
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.video and args.host != "opencode":
        parser.error("--video requires --host opencode")
    if args.asr_model is not None and not args.video:
        parser.error("--asr-model requires --video")
    if args.asr_model is not None:
        try:
            verify_model(args.asr_model)
        except (ValueError, OSError) as error:
            parser.error(str(error))

    benchmark = benchmark_root(args.benchmark)
    context_dir = task_context(benchmark, args.task)
    if not context_dir.is_dir():
        print(f"benchmark task not found: {context_dir}")
        return 2
    config = model_config()
    question = task_question(benchmark, args.task)
    print(f"task {args.task} · host {args.host} · model {config['model']} · declaration {args.declaration} · {question}")
    print(f"gateway {config['url']} · image {args.image} · video {args.video} · ASR {args.asr_model or 'off'}")
    if args.check_config or args.runs <= 0:
        print("configuration ok")
        return 0

    if args.out:
        out_dir = Path(args.out)
    elif args.host == "loop":  # the reference loop keeps its historical directory names
        out_dir = RUNS / args.task / ("agent" if args.declaration == "fresh" else f"agent-{args.declaration}")
    else:
        out_dir = RUNS / args.task / f"{args.host}-{args.declaration}"
    results: list[dict[str, Any]] = []
    for index in range(1, args.runs + 1):
        run_dir = out_dir / f"run_{index}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        print(f"\n=== run {index}/{args.runs} ===")
        result = asyncio.run(run_once(args.task, question, benchmark, run_dir, config, max_turns=args.max_turns,
                                      max_tool_chars=args.max_tool_chars, max_tokens=args.max_tokens,
                                      declaration=args.declaration, host=args.host, host_timeout=args.host_timeout,
                                      image=args.image, video=args.video, asr_model=args.asr_model))
        results.append(result)
        c = result["convergence"]
        print(f"  → passed={result['official']['passed']} turns={result['turns']} tool_calls={result['tool_calls']} "
              f"tokens={result['usage'].get('total_tokens')} cost=${result['cost_usd']:.4f} stop={result['stop_reason']} "
              f"refusals={c['refusals']} unadvised={c['unadvised_refusals']} repair_rate={c['repair_rate']} nudges={result['stall_nudges']} "
              f"declared_at={result['declaration_turn']}")
        summary = summarize(results)
        (out_dir / "agent_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== summary ===")
    print(json.dumps(summarize(results), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
