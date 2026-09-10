"""OpenCode as the agent host, in a container.

``opencode run`` drives the model inside a container built from
``opencode.Dockerfile`` beside this module: OpenCode at a pinned version and
the backend's MCP entry point installed from the lockfile. The backend is
reached only through that MCP server, launched by OpenCode from the per-run
``opencode.json`` this module writes. Every builtin tool (bash, edit, read,
...) is disabled for the agent and only the ``backend_*`` MCP tools are
allowed by default, so the model cannot step around the backend. An optional
``perception_*`` server supplies video frames and saves the host model's
observations; it performs no structured data queries. The run's
``--format json`` event stream is parsed into the harness's tool-event
records: one per tool call with the backend's own status, code, summary and
advice kinds, and the step it happened in.

    docker run --rm -v <run dir>:/run -v <benchmark>:/data:ro -v <empty dir>:/work ... <image> \\
        run --agent backend --model harness/<model> --format json --auto --pure "<question>"

Why a container and nothing else: OpenCode adds to the system prompt every
AGENTS.md or CLAUDE.md it finds upwards from its working directory and from
the directory of the config file it loads, plus global config, plugins and
session state. Measured on 2026-09-02, runs whose opencode.json sat inside
this repository carried the repository's AGENTS.md, about 2.4k tokens more per
step. Inside the container HOME and the working directory are empty and the
run directory is mounted at /run, so the model sees the harness's prompt and
nothing else of ours (MADR 0011).

Settings come from the harness configuration (DEFAULT_MODEL_*): the provider
is declared as an OpenAI-compatible endpoint with the same base URL the
in-process loop uses; the key reaches the container through the environment.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SERVER = "backend"                 # MCP server name; tools appear to the model as backend_<tool>
PROVIDER = "harness"               # provider id in opencode.json; the model is referenced as harness/<model>
AGENT = "backend"                  # backend tools, plus explicitly enabled perception
KEY_ENV = "HARNESS_MODEL_API_KEY"  # how the API key reaches OpenCode
BUILTIN_TOOLS = ("bash", "edit", "write", "read", "glob", "grep", "list", "patch", "webfetch", "websearch",
                 "task", "todowrite", "todoread", "question", "skill", "lsp")
PERMISSION_KEYS = ("bash", "edit", "read", "glob", "grep", "list", "webfetch", "websearch", "task", "todowrite",
                   "question", "skill", "lsp")
DEFAULT_IMAGE = "intentum-opencode:1.18.26"       # built from opencode.Dockerfile beside this module
DOCKER_RUN_DIR = "/run"                          # the run directory inside the container
DOCKER_DATA_DIR = "/data"                        # the benchmark, read-only, inside the container
DOCKER_HOME = "/work"                            # an empty HOME and cwd inside the container
DOCKER_BACKEND = "/opt/venv/bin/agent-backend-mcp"  # where the image installs the backend's MCP entry point


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
    image: str                       # the image the container ran from


def write_config(run_dir: Path, *, model: str, base_url: str, system_prompt: str,
                 context_limit: int = 262144, output_limit: int = 16384, mcp_timeout_ms: int = 60000,
                 video_context: str | None = None) -> Path:
    """Write the per-run opencode.json and the agent prompt beside it, with the paths the container sees."""
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
            SERVER: {"type": "local",
                     "command": [DOCKER_BACKEND, "--workspace", f"{DOCKER_RUN_DIR}/workspace", "--export-root", DOCKER_RUN_DIR],
                     "enabled": True, "timeout": mcp_timeout_ms},
        },
        "agent": {
            AGENT: {
                "mode": "primary",
                "description": "Solve one analytics task only through the backend's MCP tools",
                "model": f"{PROVIDER}/{model}",
                "prompt": "{file:" + f"{DOCKER_RUN_DIR}/prompt.txt" + "}",
                "tools": {**{tool: False for tool in BUILTIN_TOOLS}, f"{SERVER}_*": True},
                "permission": {**{key: "deny" for key in PERMISSION_KEYS}, f"{SERVER}_*": "allow"},
            }
        },
    }
    if video_context is not None:
        config["provider"][PROVIDER]["models"][model].update(
            modalities={"input": ["text", "image"], "output": ["text"]}, attachment=True)
        config["mcp"]["perception"] = {
            "type": "local", "enabled": True, "timeout": 120000,
            "command": ["/opt/venv/bin/python", "-m", "agent_harness.perception.server",
                        "--context-root", video_context, "--evidence-root", f"{DOCKER_RUN_DIR}/perception"],
        }
        config["agent"][AGENT]["tools"]["perception_*"] = True
        config["agent"][AGENT]["permission"]["perception_*"] = "allow"
    path = run_dir / "opencode.json"
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def container_path(host_path: Path, mounts: dict[Path, str]) -> str:
    """Where a host path appears inside the container, given the mounts (host directory -> container directory)."""
    resolved = host_path.resolve()
    for host_root, target in sorted(mounts.items(), key=lambda item: -len(str(item[0]))):
        root = host_root.resolve()
        if resolved == root or root in resolved.parents:
            return target + ("/" + resolved.relative_to(root).as_posix() if resolved != root else "")
    raise ValueError(f"{host_path} is not under a mounted directory {sorted(map(str, mounts))}")


def docker_command(*, image: str, name: str, run_dir: Path, home_dir: Path, mounts: dict[Path, str],
                   model: str, prompt: str, title: str | None) -> list[str]:
    """``docker run`` for one measurement: the run directory at /run, the data read-only, an empty HOME, the caller's uid."""
    command = ["docker", "run", "--rm", "--name", name, "--user", f"{os.getuid()}:{os.getgid()}",
               "-e", KEY_ENV, "-e", f"OPENCODE_CONFIG={DOCKER_RUN_DIR}/opencode.json", "-e", f"HOME={DOCKER_HOME}",
               "-v", f"{run_dir.resolve()}:{DOCKER_RUN_DIR}", "-v", f"{home_dir.resolve()}:{DOCKER_HOME}"]
    for host_root, target in mounts.items():
        command += ["-v", f"{host_root.resolve()}:{target}:ro"]
    command += ["-w", DOCKER_HOME, image, "run", "--agent", AGENT, "--model", f"{PROVIDER}/{model}",
                "--format", "json", "--auto", "--pure"]
    if title:
        command += ["--title", title]
    command.append(prompt)
    return command


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
            if name.startswith("perception_"):
                # Keep provenance in normalized traces; binary attachments remain
                # in the raw event stream and in the run's evidence directory.
                tool_events[-1]["evidence"] = {key: body[key] for key in (
                    "source_path", "source_sha256", "duration_s", "has_audio", "frames", "coverage",
                    "observation_id", "path", "evidence") if key in body}
        elif kind == "text":
            texts.append(str(part.get("text") or ""))
    return {"tool_events": tool_events, "turns": steps, "usage": tokens, "cost_usd": round(cost, 6),
            "final_text": "\n".join(texts), "session_id": session_id, "last_reason": last_reason}


def run_opencode(run_dir: Path, *, prompt: str, system_prompt: str, settings: dict[str, str], mounts: dict[Path, str],
                 image: str = DEFAULT_IMAGE, timeout_s: int = 900, title: str | None = None,
                 video_context: str | None = None) -> HostRun:
    """One ``opencode run`` in a fresh container: ``run_dir`` at /run, each of ``mounts`` read-only, an empty HOME.

    The caller renders the framing with container paths (see ``container_path``);
    the backend's workspace and exports land in ``run_dir`` on this side.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    write_config(run_dir, model=settings["model"], base_url=settings["url"], system_prompt=system_prompt,
                 video_context=video_context)
    home = Path(tempfile.mkdtemp(prefix="intentum-opencode-home-"))  # mounted as the container's empty HOME and cwd
    container = f"intentum-{run_dir.parent.name}-{run_dir.name}-{int(time.time())}"
    command = docker_command(image=image, name=container, run_dir=run_dir, home_dir=home, mounts=mounts,
                             model=settings["model"], prompt=prompt, title=title)
    env = {**os.environ, KEY_ENV: settings["key"]}
    events_path = run_dir / "events.jsonl"
    started = time.perf_counter()
    timed_out = False
    with events_path.open("w", encoding="utf-8") as events, (run_dir / "host.log").open("w", encoding="utf-8") as log:
        # stdin closed: an inherited pipe that never closes has stalled opencode before it started the MCP server.
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=events, stderr=log,
                                   start_new_session=True)
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["docker", "kill", container], capture_output=True)
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    elapsed = round(time.perf_counter() - started, 1)
    shutil.rmtree(home, ignore_errors=True)
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
                   exit_code=process.returncode, image=image)
