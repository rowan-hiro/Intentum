"""A tool-calling loop over an MCP server.

The model sees a system prompt, a user prompt and the server's tool schemas.
Every tool call is forwarded to the server and the structured response is
handed back verbatim, truncated only for size. The loop knows nothing about
the task: the caller names the deliverable and the prompts that steer the
model once it has been written or when it stops calling tools.

Pacing is the loop's business, not the backend's (MADR 0009, 0010): when the
model keeps previewing one source without materializing anything, the loop
says so, with the turns that remain.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.model import chat, openai_tools

STALL_PROMPT = (
    "You have previewed {source} {count} times in a row without materializing anything; {remaining} turns remain. "
    "If a preview already shows the answer, materialize it with materialize_result and export it; "
    "if it does not, change the approach rather than previewing again."
)


@dataclass
class LoopResult:
    stop_reason: str                     # done | max_turns | no_tool_calls
    turns: list[dict[str, Any]]          # one record per model turn
    tool_events: list[dict[str, Any]]    # one record per forwarded tool call
    messages: list[dict[str, Any]]       # the full conversation as sent to the model
    usage: dict[str, int]
    cost_usd: float
    elapsed_s: float
    stall_nudges: int = 0            # times the loop pointed out a preview streak


def _preview_streak(tool_events: list[dict[str, Any]]) -> tuple[str | None, int]:
    """How many trailing tool calls previewed the same source without materializing or exporting."""
    source: str | None = None
    count = 0
    for event in reversed(tool_events):
        if event["tool"] != "transform_dataset" or (event.get("arguments") or {}).get("output_name"):
            break
        this = str((event.get("arguments") or {}).get("source"))
        if source is None:
            source = this
        elif this != source:
            break
        count += 1
    return source, count


def _delivered(event: dict[str, Any], deliverable: Path) -> bool:
    """A successful tool call whose ``path`` argument names the deliverable."""
    path = event["arguments"].get("path") if isinstance(event.get("arguments"), dict) else None
    return bool(event["status"] == "success" and path and Path(str(path)).expanduser().resolve() == deliverable.resolve())


async def run_tool_loop(server, config: dict[str, str], *, system_prompt: str, user_prompt: str,
                        deliverable: Path | None, delivered_prompt: str, nudge_prompt: str,
                        max_turns: int, max_tool_chars: int, max_tokens: int, max_nudges: int = 2,
                        preview_streak: int = 4, stall_prompt: str = STALL_PROMPT) -> LoopResult:
    tools = openai_tools(await server.list_tools())
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    turns: list[dict[str, Any]] = []
    usage_total: Counter = Counter()
    cost_total = 0.0
    tool_events: list[dict[str, Any]] = []
    stop_reason = "max_turns"
    nudges = 0
    stall_nudges = 0
    stall_nudged_at = 0
    delivered_once = False
    started = time.perf_counter()

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
                                    "resolution": response.get("resolution"),
                                    "advice": [a.get("kind") for a in (response.get("advice") or []) if isinstance(a, dict)],
                                    "elapsed_s": round(time.perf_counter() - c0, 3)})
            text = json.dumps(response, ensure_ascii=False, default=str)
            if len(text) > max_tool_chars:
                text = text[:max_tool_chars] + f"... [truncated {len(text) - max_tool_chars} chars]"
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": text})
            record["tool_calls"].append({"tool": name, "status": response.get("status"), "code": response.get("code")})
        turns.append(record)
        print(f"  turn {turn}: " + (", ".join(f"{c['tool']}→{c['status']}" + (f"({c['code']})" if c["code"] else "") for c in record["tool_calls"])
                                 or f"text: {(message.get('content') or '')[:120]!r}"))

        source, streak = _preview_streak(tool_events)
        if streak >= preview_streak and streak - stall_nudged_at >= preview_streak:
            stall_nudged_at = streak
            stall_nudges += 1
            messages.append({"role": "user", "content": stall_prompt.format(source=source, count=streak, remaining=max_turns - turn)})
            print(f"  nudge: {streak} previews of {source} in a row")

        if deliverable is not None and not delivered_once and any(
            e["turn"] == turn and _delivered(e, deliverable) for e in tool_events
        ):
            delivered_once = True
            messages.append({"role": "user", "content": delivered_prompt})
            continue

        if not calls:
            content = (message.get("content") or "").strip()
            if "DONE" in content.upper() or (deliverable is not None and deliverable.exists() and content):
                stop_reason = "done"
                break
            if nudges >= max_nudges:
                stop_reason = "no_tool_calls"
                break
            nudges += 1
            messages.append({"role": "user", "content": nudge_prompt})

    return LoopResult(stop_reason=stop_reason, turns=turns, tool_events=tool_events, messages=messages,
                      usage=dict(usage_total), cost_usd=round(cost_total, 6), elapsed_s=round(time.perf_counter() - started, 1),
                      stall_nudges=stall_nudges)
