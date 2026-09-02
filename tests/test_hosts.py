"""Host adapters (MADR 0011): an external agent's record becomes the harness's tool events."""

import json
from pathlib import Path

from agent_harness.hosts.opencode import (
    AGENT, BUILTIN_TOOLS, KEY_ENV, PROVIDER, SERVER, backend_command, parse_events, write_config,
)
from agent_harness.scenarios.dataspace.agent import convergence

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "opencode_events.jsonl"


def test_opencode_events_become_tool_events_with_the_backend_status_and_advice():
    parsed = parse_events(FIXTURE.read_text(encoding="utf-8").splitlines())
    events = parsed["tool_events"]
    assert [e["tool"] for e in events][:3] == ["import_workspace", "describe_dataset", "transform_dataset"]
    assert len(events) == 11 and parsed["turns"] == 12 and parsed["session_id"] == "ses_fixture"
    refused = events[3]
    assert refused["status"] == "error" and refused["code"] == "NOT_FOUND" and refused["advice"] == ["expression_as_derive"]
    assert refused["turn"] == 4 and "round(TotalAssets, 4) as TotalAssets" in refused["summary"]
    assert events[4]["status"] == "success" and events[4]["advice"] == []
    assert events[6]["tool"] == "declare_output" and events[6]["status"] == "success"
    assert parsed["usage"]["prompt_tokens"] == 229836 and parsed["usage"]["reasoning_tokens"] == 2866
    assert parsed["cost_usd"] > 0 and parsed["last_reason"] == "stop"
    assert parsed["final_text"].startswith("任务已完成")


def test_convergence_metrics_apply_to_host_events_unchanged():
    events = parse_events(FIXTURE.read_text(encoding="utf-8").splitlines())["tool_events"]
    assert convergence(events) == {"refusals": 1, "unadvised_refusals": 0, "repaired_refusals": 1, "repair_rate": 1.0,
                                   "advice_kinds": {"expression_as_derive": 1}}


def test_config_allows_only_the_backend_tools(tmp_path):
    path = write_config(tmp_path, model="qwen/qwen3.5-35b-a3b", base_url="https://gateway.example/api", system_prompt="Work only through the tools.",
                        workspace=tmp_path / "workspace", export_root=tmp_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    agent = config["agent"][AGENT]
    assert agent["mode"] == "primary" and agent["prompt"] == "{file:./prompt.txt}"
    assert all(agent["tools"][tool] is False for tool in BUILTIN_TOOLS) and agent["tools"][f"{SERVER}_*"] is True
    assert agent["permission"]["bash"] == "deny" and agent["permission"][f"{SERVER}_*"] == "allow"
    provider = config["provider"][PROVIDER]
    assert provider["npm"] == "@ai-sdk/openai-compatible" and provider["options"]["baseURL"] == "https://gateway.example/api"
    assert provider["options"]["apiKey"] == "{env:" + KEY_ENV + "}" and "qwen/qwen3.5-35b-a3b" in provider["models"]
    assert config["model"] == f"{PROVIDER}/qwen/qwen3.5-35b-a3b"
    mcp = config["mcp"][SERVER]
    assert mcp["type"] == "local" and mcp["command"] == backend_command(tmp_path / "workspace", tmp_path)
    assert "agent-backend-mcp" in mcp["command"] and str(tmp_path / "workspace") in mcp["command"]
    assert (tmp_path / "prompt.txt").read_text(encoding="utf-8") == "Work only through the tools."


def test_host_output_that_is_not_backend_json_is_a_host_error():
    line = json.dumps({"type": "tool_use", "sessionID": "s", "part": {"tool": "backend_transform_dataset",
                       "state": {"status": "error", "input": {"source": "x"}, "output": "Tool execution failed: timeout"}}})
    (event,) = parse_events([json.dumps({"type": "step_start", "part": {}}), line])["tool_events"]
    assert event["status"] == "error" and event["code"] == "HOST" and event["summary"].startswith("Tool execution failed")


def test_an_argument_the_sdk_refused_is_recorded_as_invalid_intent():
    line = json.dumps({"type": "tool_use", "sessionID": "s", "part": {"tool": "backend_transform_dataset", "state": {
        "status": "error", "input": {"source": "cost", "transform": "[{\"filter\": \"x = 1\""},
        "error": "Error executing tool transform_dataset: 2 validation errors for transform_datasetArguments\ntransform.dict[str,any]\n  Input should be a valid dictionary"}}})
    (event,) = parse_events([json.dumps({"type": "step_start", "part": {}}), line])["tool_events"]
    assert event["status"] == "error" and event["code"] == "INVALID_INTENT"
    assert event["summary"].startswith("Error executing tool transform_dataset") and event["advice"] == []
