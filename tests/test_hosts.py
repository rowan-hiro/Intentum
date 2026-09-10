"""Host adapters (MADR 0011): an external agent's record becomes the harness's tool events."""

import json
from pathlib import Path

from agent_harness.hosts.opencode import (
    AGENT, BUILTIN_TOOLS, DOCKER_BACKEND, DOCKER_DATA_DIR, DOCKER_HOME, DOCKER_RUN_DIR, KEY_ENV, PROVIDER, SERVER,
    container_path, docker_command, parse_events, write_config,
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


def test_config_allows_only_the_backend_tools_and_points_at_the_container_paths(tmp_path):
    path = write_config(tmp_path, model="qwen/qwen3.5-35b-a3b", base_url="https://gateway.example/api", system_prompt="Work only through the tools.")
    config = json.loads(path.read_text(encoding="utf-8"))
    agent = config["agent"][AGENT]
    assert agent["mode"] == "primary" and agent["prompt"] == "{file:/run/prompt.txt}"
    assert all(agent["tools"][tool] is False for tool in BUILTIN_TOOLS) and agent["tools"][f"{SERVER}_*"] is True
    assert agent["permission"]["bash"] == "deny" and agent["permission"][f"{SERVER}_*"] == "allow"
    provider = config["provider"][PROVIDER]
    assert provider["npm"] == "@ai-sdk/openai-compatible" and provider["options"]["baseURL"] == "https://gateway.example/api"
    assert provider["options"]["apiKey"] == "{env:" + KEY_ENV + "}" and "qwen/qwen3.5-35b-a3b" in provider["models"]
    assert config["model"] == f"{PROVIDER}/qwen/qwen3.5-35b-a3b"
    assert config["mcp"][SERVER]["command"] == [DOCKER_BACKEND, "--workspace", "/run/workspace", "--export-root", "/run"]
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


def test_host_paths_are_translated_to_the_container_mounts(tmp_path):
    benchmark, run_dir = tmp_path / "bench", tmp_path / "runs" / "task_44" / "run_1"
    (benchmark / "input" / "task_44" / "context").mkdir(parents=True)
    run_dir.mkdir(parents=True)
    mounts = {benchmark: DOCKER_DATA_DIR, run_dir: DOCKER_RUN_DIR}
    assert container_path(benchmark / "input" / "task_44" / "context", mounts) == "/data/input/task_44/context"
    assert container_path(run_dir / "predictions" / "task_44" / "prediction.csv", mounts) == "/run/predictions/task_44/prediction.csv"
    assert container_path(run_dir, mounts) == "/run"
    try:
        container_path(tmp_path / "elsewhere", mounts)
    except ValueError as err:
        assert "not under a mounted directory" in str(err)
    else:
        raise AssertionError("a path outside the mounts must be refused")


def test_the_container_gets_the_run_dir_the_data_read_only_and_an_empty_home(tmp_path):
    run_dir, home, data = tmp_path / "run_1", tmp_path / "home", tmp_path / "bench"
    for d in (run_dir, home, data):
        d.mkdir()
    command = docker_command(image="intentum-opencode:1.18.26", name="intentum-test", run_dir=run_dir, home_dir=home,
                             mounts={data: DOCKER_DATA_DIR}, model="qwen/qwen3.5-35b-a3b", prompt="the question", title="task_44")
    text = " ".join(command)
    assert command[:3] == ["docker", "run", "--rm"] and "--name intentum-test" in text
    assert f"-v {run_dir.resolve()}:{DOCKER_RUN_DIR} " in text and f"-v {data.resolve()}:{DOCKER_DATA_DIR}:ro" in text
    assert f"-v {home.resolve()}:{DOCKER_HOME}" in text and f"-e HOME={DOCKER_HOME}" in text and f"-w {DOCKER_HOME}" in text
    assert "-e HARNESS_MODEL_API_KEY " in text and f"-e OPENCODE_CONFIG={DOCKER_RUN_DIR}/opencode.json" in text
    assert "HARNESS_MODEL_API_KEY=" not in text  # the key travels in the environment, never on the command line
    assert command[-1] == "the question" and "--format json --auto --pure --title task_44" in text
    assert f"intentum-opencode:1.18.26 run --agent {AGENT} --model {PROVIDER}/qwen/qwen3.5-35b-a3b" in text


def test_video_is_opt_in_and_enables_images_and_only_the_scoped_reader(tmp_path):
    options = dict(model="vision-model", base_url="https://gateway.example", system_prompt="Read the evidence.")
    default = json.loads(write_config(tmp_path, **options).read_text())
    assert "perception" not in default["mcp"]
    assert "modalities" not in default["provider"][PROVIDER]["models"]["vision-model"]
    config = json.loads(write_config(tmp_path, **options, video_context="/data/context").read_text())
    assert config["provider"][PROVIDER]["models"]["vision-model"]["modalities"]["input"] == ["text", "image"]
    assert config["mcp"]["perception"]["command"][-4:] == ["--context-root", "/data/context", "--evidence-root", "/run/perception"]
    agent = config["agent"][AGENT]
    assert agent["tools"]["perception_*"] and agent["permission"]["perception_*"] == "allow"
    assert all(agent["tools"][tool] is False for tool in BUILTIN_TOOLS)


def test_video_evidence_is_retained_without_binary_in_normalized_events():
    body = {"status": "success", "summary": "Decoded a frame", "frames": [{"frame_id": "f1", "timestamp_s": 0.5}]}
    line = json.dumps({"type": "tool_use", "part": {"tool": "perception_read_video_frames", "state": {
        "status": "completed", "input": {"path": "/data/context/clip.mp4", "timestamps_s": [0.5]},
        "output": json.dumps(body), "attachments": [{"url": "data:image/png;base64,AAAA"}]}}})
    event = parse_events([line])["tool_events"][0]
    assert event["tool"] == "perception_read_video_frames" and event["status"] == "success"
    assert event["evidence"]["frames"] == body["frames"]
    assert "base64" not in json.dumps(event)


def test_audio_config_uses_local_asr_without_requiring_an_audio_capable_chat_model(tmp_path):
    import pytest
    options = dict(model="vision-model", base_url="https://gateway.example", system_prompt="Read evidence.")
    with pytest.raises(ValueError, match="scoped video context"):
        write_config(tmp_path, **options, audio=True)
    config = json.loads(write_config(tmp_path, **options, video_context="/data/context", audio=True).read_text())
    assert config["mcp"]["perception"]["command"][-2:] == ["--asr-model", "/opt/asr-model"]
    assert config["mcp"]["perception"]["timeout"] == 360000
    assert config["provider"][PROVIDER]["models"]["vision-model"]["modalities"]["input"] == ["text", "image"]
    assert config["agent"][AGENT]["permission"]["bash"] == "deny"


def test_audio_trace_preserves_estimated_bounds_and_actual_decoded_interval():
    body = {"status": "success", "clip_start_s": 2, "clip_end_s": 3, "decoded_duration_s": 1,
            "audio_bounds": {"start_s": 1.5, "end_s": 9, "end_source": "format.duration", "end_is_estimate": True}}
    line = json.dumps({"type": "tool_use", "part": {"tool": "perception_transcribe_audio", "state": {
        "status": "completed", "input": {"start_s": 2, "duration_s": 10}, "output": json.dumps(body)}}})
    event = parse_events([line])["tool_events"][0]
    assert event["evidence"] == {key: value for key, value in body.items() if key != "status"}
