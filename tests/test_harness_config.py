"""Local media defaults can be overridden without quietly losing ASR or exposing keys."""

import json
import sys

import pytest

from agent_harness import config
from agent_harness.perception.prepare_asr import MODEL_FILES, copy_local
from agent_harness.scenarios.dataspace import agent


@pytest.fixture
def configured_runner(tmp_path, monkeypatch):
    context = tmp_path / "benchmark" / "input" / "task_312" / "context"
    context.mkdir(parents=True)
    (context.parent / "task.json").write_text(json.dumps({"question": "Read the video."}))
    source = tmp_path / "source"
    source.mkdir()
    for name in MODEL_FILES:
        (source / name).write_bytes(name.encode())
    model = copy_local(source, tmp_path / "weights")
    settings = {"DEFAULT_MODEL_API_URL": "https://gateway.example", "DEFAULT_MODEL_API_KEY": "private-test-key",
                "DEFAULT_MODEL_NAME": "vision-model", "DATASPACE_BENCHMARK": str(tmp_path / "benchmark"),
                "HARNESS_VIDEO": "1", "HARNESS_ASR_MODEL": "weights", "HARNESS_OPENCODE_IMAGE": "test:media"}
    for name in settings:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "load_env_file", lambda: settings)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return model, settings


@pytest.mark.parametrize("flags,video,audio", [([], True, True), (["--no-audio"], True, False),
                                              (["--no-video"], False, False),
                                              (["--no-video", "--host", "loop"], False, False)])
def test_media_check_uses_repo_paths_and_explicit_opt_outs(configured_runner, monkeypatch, capsys,
                                                         tmp_path, flags, video, audio):
    model, _ = configured_runner
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "argv", ["agent", "--task", "task_312", "--check-config", *flags])
    assert agent.main() == 0
    output = capsys.readouterr().out
    assert f"video {video}" in output
    assert f"ASR {model if audio else 'off'}" in output
    assert "image test:media" in output
    assert "private-test-key" not in output
    assert not list(elsewhere.iterdir())


def test_environment_and_cli_override_saved_settings(configured_runner, monkeypatch, capsys):
    model, _ = configured_runner
    monkeypatch.setenv("HARNESS_VIDEO", "false")
    monkeypatch.setenv("HARNESS_ASR_MODEL", "missing")
    monkeypatch.setenv("HARNESS_OPENCODE_IMAGE", "env:media")
    assert not config.boolean_setting("HARNESS_VIDEO")
    monkeypatch.setattr(sys, "argv", ["agent", "--task", "task_312", "--check-config", "--video",
                                     "--asr-model", str(model), "--image", "cli:media"])
    assert agent.main() == 0
    output = capsys.readouterr().out
    assert "video True" in output and f"ASR {model}" in output and "image cli:media" in output


def test_bad_weights_fail_before_starting_or_replacing_a_run(configured_runner, monkeypatch, capsys, tmp_path):
    model, _ = configured_runner
    (model / "model.bin").write_bytes(b"corrupt")
    existing = tmp_path / "saved" / "run_1"
    existing.mkdir(parents=True)
    evidence = existing / "evidence.txt"
    evidence.write_text("keep this run")
    monkeypatch.setattr(sys, "argv", ["agent", "--task", "task_312", "--out", str(existing.parent)])
    with pytest.raises(SystemExit) as error:
        agent.main()
    assert error.value.code == 2
    assert "does not match its manifest" in capsys.readouterr().err
    assert evidence.read_text() == "keep this run"


def test_invalid_media_toggle_is_reported(configured_runner, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_VIDEO", "treu")
    monkeypatch.setattr(sys, "argv", ["agent", "--check-config"])
    with pytest.raises(SystemExit) as error:
        agent.main()
    assert error.value.code == 2
    assert "HARNESS_VIDEO must be" in capsys.readouterr().err
