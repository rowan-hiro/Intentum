"""OpenAI-compatible chat completions with tool calling, standard library only."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


def chat(config: dict[str, str], messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int,
         timeout: int = 180, attempts: int = 3) -> dict[str, Any]:
    """One chat completion; retries transient gateway errors with a linear back-off."""
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
    """MCP tool definitions rendered as OpenAI function tools, schemas untouched."""
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}}
        for t in mcp_tools
    ]
