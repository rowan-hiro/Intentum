"""OpenCode as the agent host.

``opencode run`` drives the model; the backend is reached only through its
MCP server, launched by OpenCode from the per-run ``opencode.json`` this
module writes. Every builtin tool (bash, edit, read, ...) is disabled for the
agent and only the ``backend_*`` MCP tools are allowed, so the model cannot
step around the backend. The run's ``--format json`` event stream is parsed
into the harness's tool-event records: one per tool call with the backend's
own status, code, summary and advice kinds, and the step it happened in.

    opencode run --agent backend --model harness/<model> --format json --auto --pure "<question>"

Settings come from the harness configuration (DEFAULT_MODEL_*): the provider
is declared as an OpenAI-compatible endpoint with the same base URL the
in-process loop uses; the key reaches OpenCode through the environment.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from agent_harness.config import ROOT

SERVER = "backend"                 # MCP server name; tools appear to the model as backend_<tool>
PROVIDER = "harness"               # provider id in opencode.json; the model is referenced as harness/<model>
AGENT = "backend"                  # the agent whose only tools are the backend's
KEY_ENV = "HARNESS_MODEL_API_KEY"  # how the API key reaches OpenCode
BUILTIN_TOOLS = ("bash", "edit", "write", "read", "glob", "grep", "list", "patch", "webfetch", "websearch",
                 "task", "todowrite", "todoread", "question", "skill", "lsp")
PERMISSION_KEYS = ("bash", "edit", "read", "glob", "grep", "list", "webfetch", "websearch", "task", "todowrite",
                   "question", "skill", "lsp")


@dataclass
class HostRun:
    tool_events: list[dict[str, Any]]
    turns: int                       # model steps (one per request to the model)
    usage: dict[str, int]            # prompt_tokens, completion_tokens, reasoning_tokens, total_tokens
    cost_usd: float                  # OpenCode's own estimate from the model's price list; 0 when unpriced
    stop_reason: str                 # done | timeout | error | unknown
    elapsed_s: float
    final_text: str
    session_id: str | None
    exit_code: int | None


def opencode_version() -> str | None:
    if shutil.which("opencode") is None:
        return None
    try:
        proc = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None


def backend_command(workspace: Path, export_root: Path) -> list[str]:
    """The MCP server as OpenCode launches it: the backend's own entry point on this run's workspace."""
    return ["uv", "run", "--directory", str(ROOT), "agent-backend-mcp",
            "--workspace", str(workspace), "--export-root", str(export_root)]


def write_config(run_dir: Path, *, model: str, base_url: str, system_prompt: str, workspace: Path, export_root: Path,
                 context_limit: int = 262144, output_limit: int = 16384, mcp_timeout_ms: int = 60000) -> Path:
    """Write the per-run opencode.json and the agent prompt beside it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(system_prompt, encoding="utf-8")
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"{PROVIDER}/{model}",
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Harness model gateway",
                "options": {"baseURL": base_url, "apiKey": "{env:" + KEY_ENV + "}"},
                "models": {model: {"name": model, "limit": {"context": context_limit, "output": output_limit}}},
            }
        },
        "mcp": {
            SERVER: {"type": "local", "command": backend_command(workspace, export_root), "enabled": True,
                     "timeout": mcp_timeout_ms},
        },
        "agent": {
            AGENT: {
                "mode": "primary",
                "description": "Solve one analytics task only through the backend's MCP tools",
                "model": f"{PROVIDER}/{model}",
                "prompt": "{file:./prompt.txt}",
                "tools": {**{tool: False for tool in BUILTIN_TOOLS}, f"{SERVER}_*": True},
                "permission": {**{key: "deny" for key in PERMISSION_KEYS}, f"{SERVER}_*": "allow"},
            }
        },
    }
    path = run_dir / "opencode.json"
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _body(output: Any) -> dict[str, Any]:
    """The backend's structured response inside a tool result, or an empty dict."""
    if isinstance(output, dict):
        return output
    if isinstance(output, str) and output.lstrip().startswith("{"):
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_events(lines: Iterable[str]) -> dict[str, Any]:
    """Turn an ``opencode run --format json`` stream into tool events, steps, tokens, cost and the final text."""
    tool_events: list[dict[str, Any]] = []
    steps = 0
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0, "cache_read_tokens": 0}
    cost = 0.0
    texts: list[str] = []
    session_id: str | None = None
    last_reason: str | None = None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        session_id = event.get("sessionID") or session_id
        kind = event.get("type")
        part = event.get("part") or {}
        if kind == "step_start":
            steps += 1
        elif kind == "step_finish":
            last_reason = part.get("reason")
            t = part.get("tokens") or {}
            tokens["prompt_tokens"] += int(t.get("input") or 0)
            tokens["completion_tokens"] += int(t.get("output") or 0)
            tokens["reasoning_tokens"] += int(t.get("reasoning") or 0)
            tokens["total_tokens"] += int(t.get("total") or 0)
            tokens["cache_read_tokens"] += int((t.get("cache") or {}).get("read") or 0)
            cost += float(part.get("cost") or 0.0)
        elif kind == "tool_use":
            state = part.get("state") or {}
            name = str(part.get("tool") or "")
            tool = name[len(SERVER) + 1:] if name.startswith(f"{SERVER}_") else name
            body = _body(state.get("output"))
            status = body.get("status") or ("error" if state.get("status") == "error" else str(state.get("status")))
            # A call the host or the MCP SDK refused before the backend saw it: the text is in state.error.
            host_error = str(state.get("error") or "") if not body else ""
            if body:
                code = body.get("code")
            elif "validation error" in host_error:
                code = "INVALID_INTENT"  # the SDK rejected the argument shape, as the in-process loop records it
            else:
                code = "HOST" if status == "error" else None
            when = state.get("time") or {}
            elapsed = round((float(when.get("end", 0)) - float(when.get("start", 0))) / 1000, 3) if when.get("end") else None
            arguments = state.get("input") if isinstance(state.get("input"), dict) else {}
            tool_events.append({
                "turn": max(steps, 1), "tool": tool, "arguments": arguments, "status": status,
                "code": code,
                "summary": body.get("summary") or body.get("message") or (host_error[:300] or str(state.get("output"))[:200] if not body else None),
                "resolution": body.get("resolution"),
                "advice": [a.get("kind") for a in (body.get("advice") or []) if isinstance(a, dict)],
                "elapsed_s": elapsed,
            })
        elif kind == "text":
            texts.append(str(part.get("text") or ""))
    return {"tool_events": tool_events, "turns": steps, "usage": tokens, "cost_usd": round(cost, 6),
            "final_text": "\n".join(texts), "session_id": session_id, "last_reason": last_reason}


def run_opencode(run_dir: Path, *, prompt: str, system_prompt: str, settings: dict[str, str],
                 timeout_s: int = 900, title: str | None = None) -> HostRun:
    """One ``opencode run`` in ``run_dir``: the backend's workspace and export root live there too."""
    workspace = run_dir / "workspace"
    config_path = write_config(run_dir, model=settings["model"], base_url=settings["url"], system_prompt=system_prompt,
                               workspace=workspace, export_root=run_dir)
    env = {**os.environ, KEY_ENV: settings["key"], "OPENCODE_CONFIG": str(config_path)}
    command = ["opencode", "run", "--agent", AGENT, "--model", f"{PROVIDER}/{settings['model']}",
               "--format", "json", "--auto", "--pure"]
    if title:
        command += ["--title", title]
    command.append(prompt)
    events_path = run_dir / "events.jsonl"
    started = time.perf_counter()
    timed_out = False
    with events_path.open("w", encoding="utf-8") as events, (run_dir / "host.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=run_dir, env=env, stdout=events, stderr=log, start_new_session=True)
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    elapsed = round(time.perf_counter() - started, 1)
    parsed = parse_events(events_path.read_text(encoding="utf-8").splitlines())
    if timed_out:
        stop = "timeout"
    elif process.returncode != 0:
        stop = "error"
    elif parsed["last_reason"] == "stop":
        stop = "done"
    else:
        stop = "unknown"
    return HostRun(tool_events=parsed["tool_events"], turns=parsed["turns"], usage=parsed["usage"], cost_usd=parsed["cost_usd"],
                   stop_reason=stop, elapsed_s=elapsed, final_text=parsed["final_text"], session_id=parsed["session_id"],
                   exit_code=process.returncode)
