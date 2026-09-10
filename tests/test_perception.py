"""Video perception stays outside the backend and preserves revisable, attributable evidence."""

import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_backend import Backend
from agent_harness.perception import video as video_module
from agent_harness.perception.server import create_server
from agent_harness.perception.video import VideoReader, digest


@pytest.fixture
def reader(tmp_path):
    context = tmp_path / "context"
    context.mkdir()
    return VideoReader(context, tmp_path / "evidence")


def evidence(reader):
    frame_id = "a" * 64
    image = reader.evidence_root / "frames" / f"{frame_id}.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="))
    fact = {"frame_id": frame_id, "source_path": str(reader.context_root / "clip.mp4"),
            "source_sha256": "b" * 64, "frame_sha256": digest(image),
            "timestamp_s": 3.5, "image_path": str(image)}
    (reader.evidence_root / "frames" / f"{frame_id}.json").write_text(json.dumps(fact))
    return fact


def test_reader_confines_sources_including_symlinks(reader, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    inside = reader.context_root / "clip.mp4"
    inside.write_bytes(b"video")
    assert reader.source("clip.mp4") == inside
    for path in (str(outside), "../outside.mp4"):
        with pytest.raises(ValueError, match="inside"):
            reader.source(path)
    (reader.context_root / "link.mp4").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        reader.source("link.mp4")


def test_bounds_are_checked_before_decoding(reader, monkeypatch):
    monkeypatch.setattr(reader, "inspect", lambda path: {"duration_s": 10})
    for timestamps in ([], list(range(7)), [-1], [10], [float("nan")], [float("inf")]):
        with pytest.raises(ValueError):
            reader.frames("clip.mp4", timestamps)
    with pytest.raises(ValueError, match="max_dimension"):
        reader.frames("clip.mp4", [1], 5000)


@pytest.mark.parametrize("program", ["ffprobe", "ffmpeg"])
def test_media_subprocess_cannot_read_mcp_stdin_and_timeout_marks_coverage_gap(monkeypatch, program):
    def expire(command, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(video_module.subprocess, "run", expire)
    with pytest.raises(ValueError, match="requested interval as unread") as error:
        video_module._run([program], timeout=12)
    assert "shorter local clip" not in str(error.value)


def test_observations_require_real_frame_refs_and_keep_revisions_importable(reader, tmp_path):
    fact = evidence(reader)
    original = reader.observe([fact["frame_id"]], "The displayed capacity is at least 25 litres.")
    original_bytes = Path(original["path"]).read_bytes()
    assert reader.observe([fact["frame_id"]], "The displayed capacity is at least 25 litres.") == original
    with pytest.raises(ValueError, match="revision needs"):
        reader.observe([fact["frame_id"]], "More than 25 litres.", original["observation_id"])
    corrected = reader.observe([fact["frame_id"]], "The sign is strictly greater than 25 litres.",
                               original["observation_id"], "The frame shows >, not >=.")
    assert Path(original["path"]).read_bytes() == original_bytes
    row = corrected["evidence"][0]
    assert row["supersedes"] == original["observation_id"] and row["interpretation_by"] == "agent"
    assert row["source_sha256"] == fact["source_sha256"] and row["timestamp_s"] == 3.5
    backend = Backend(tmp_path / "workspace")
    try:
        imported = backend.import_dataset(corrected["path"], name="visual_observation")
        assert imported["status"] == "success"
        assert backend.describe_dataset("visual_observation")["status"] == "success"
        assert backend.integrity_report()["ok"]
    finally:
        backend.close()
    for invalid in ("../secret", "c" * 64):
        with pytest.raises(ValueError):
            reader.observe([invalid], "Claim")
    with pytest.raises(ValueError, match="Unknown"):
        reader.observe([fact["frame_id"]], "Claim", "d" * 64, "Correction")


def test_mcp_returns_real_image_content_and_matching_evidence(reader, monkeypatch):
    fact = evidence(reader)
    monkeypatch.setattr(reader, "frames", lambda *args: {"status": "success", "frames": [fact]})
    server = create_server(reader)
    result = asyncio.run(server.call_tool("read_video_frames", {"path": "clip.mp4", "timestamps_s": [3.5]}))
    assert not result.is_error
    assert result.structured_content["frames"][0]["frame_id"] == fact["frame_id"]
    assert [block.type for block in result.content] == ["text", "image"]
    assert base64.b64decode(result.content[1].data) == Path(fact["image_path"]).read_bytes()
    assert result.content[1].mime_type == "image/png"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is installed in the host image, optional on the machine")
def test_real_decoder_reports_actual_frame_time_and_source_hash(reader):
    source = reader.context_root / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240:r=4:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
    info = reader.inspect("clip.mp4")
    assert info["duration_s"] == 2 and info["has_audio"] is False
    frame = reader.frames("clip.mp4", [0.3])["frames"][0]
    assert frame["timestamp_s"] == 0.5 and frame["requested_timestamp_s"] == 0.3
    assert frame["source_sha256"] == digest(source)
    assert Path(frame["image_path"]).read_bytes().startswith(b"\x89PNG")
    assert reader.frames("clip.mp4", [0.3])["frames"][0] == frame


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="real seeking is checked inside the host image")
@pytest.mark.parametrize("offset", [0, 5])
def test_input_seek_matches_full_decode_with_vfr_gop_and_distinct_clock_origins(reader, monkeypatch, offset):
    source = reader.context_root / "seek.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-itsoffset", "2", "-f", "lavfi", "-i",
                    "testsrc2=s=320x240:r=12:d=16", "-f", "lavfi", "-i", "sine=duration=18",
                    "-map", "0:v", "-map", "1:a", "-vf", r"select=not(eq(mod(n\,7)\,2))",
                    "-fps_mode", "vfr", "-c:v", "libx264", "-g", "72", "-c:a", "aac",
                    "-output_ts_offset", str(offset), str(source)], check=True)
    info = reader.inspect("seek.mp4")
    assert float(info["video_stream"]["start_time"]) == 2 + offset
    assert float(info["format"]["start_time"]) < float(info["video_stream"]["start_time"])
    timestamps = [0.3, 2, 2.01, 6.37, 12.01, 15.8]
    seeked = reader.frames("seek.mp4", timestamps, max_dimension=320)["frames"]
    original_run = video_module._run
    seeks = []

    def without_seek(command, **kwargs):
        command = list(command)
        if "-ss" in command:
            assert command.index("-ss") < command.index("-i")
            seeks.append(float(command[command.index("-ss") + 1]))
            for flag in ("-ss", "-seek_timestamp"):
                index = command.index(flag)
                del command[index:index + 2]
            command.remove("-noaccurate_seek")
        return original_run(command, **kwargs)

    monkeypatch.setattr(video_module, "_run", without_seek)
    reference = reader.frames("seek.mp4", timestamps, max_dimension=320)["frames"]
    # Both the original PTS and PNG hashes (hence evidence ids) must agree.
    assert seeked == reference
    assert len(seeks) == 4
    assert all(frame["timestamp_s"] >= t for t, frame in zip(timestamps, seeked))
