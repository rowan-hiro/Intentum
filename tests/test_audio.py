"""Offline audio stays attributable, bounded and separate from backend data operations."""

import asyncio
import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from agent_backend import Backend
from agent_harness.perception import audio as audio_module
from agent_harness.perception.audio import AudioReader
from agent_harness.perception.prepare_asr import MODEL_FILES, copy_local
from agent_harness.perception.server import create_server
from agent_harness.perception.video import VideoReader, digest, save_json


@pytest.fixture
def reader(tmp_path):
    context, model = tmp_path / "context", tmp_path / "model"
    context.mkdir()
    model.mkdir()
    for name in MODEL_FILES:
        (model / name).write_bytes(name.encode())
    save_json(model / "manifest.json", {"files": {name: digest(model / name) for name in MODEL_FILES}})
    return AudioReader(VideoReader(context, tmp_path / "evidence"), model)


def wav_file(path, seconds=3):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * int(seconds * 16000))


def fake_media(reader, monkeypatch):
    source = reader.video.context_root / "clip.mp4"
    source.write_bytes(b"fixture")
    info = {"source_path": str(source), "source_sha256": digest(source), "duration_s": 10,
            "has_audio": True, "video_stream": {"start_time": "0"}, "audio_streams": [{"index": 1}]}
    monkeypatch.setattr(reader.video, "inspect", lambda path: info)
    monkeypatch.setattr(audio_module, "_run", lambda command, timeout: wav_file(command[-1]))
    return info


def recognition(text="The capacity is twenty five litres."):
    return {"language": "en", "language_probability": 0.99,
            "segments": [{"start": 0.5, "end": 2.5, "text": text, "avg_logprob": -0.2, "no_speech_prob": 0.01}]}


def test_local_model_copy_is_verified_and_never_replaces_source(reader, tmp_path):
    before = {name: digest(reader.model_dir / name) for name in MODEL_FILES}
    copied = copy_local(reader.model_dir, tmp_path / "copied")
    manifest = json.loads((copied / "manifest.json").read_text())
    assert manifest["files"] == before and manifest["preparation"] == "verified_local_copy"
    assert {name: digest(copied / name) for name in MODEL_FILES} == before
    assert {name: digest(reader.model_dir / name) for name in MODEL_FILES} == before
    with pytest.raises(ValueError, match="new destination"):
        copy_local(reader.model_dir, copied)
    (copied / "model.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="manifest"):
        AudioReader(reader.video, copied)


def test_transcript_times_provenance_cache_import_and_correction(reader, monkeypatch, tmp_path):
    info = fake_media(reader, monkeypatch)
    calls = []
    monkeypatch.setattr(reader, "_recognize", lambda *args: calls.append(args) or recognition())
    result = reader.transcribe("clip.mp4", start_s=5, duration_s=3)
    assert result["clip_start_s"] == 5 and result["clip_end_s"] == 8
    segment = result["segments"][0]
    assert (segment["start_s"], segment["end_s"]) == (5.5, 7.5)
    assert segment["source_sha256"] == info["source_sha256"]
    assert segment["audio_sha256"] == digest(Path(result["audio_path"]))
    assert segment["model_fingerprint"] == reader.model_fingerprint
    replay = reader.transcribe("clip.mp4", start_s=5, duration_s=3)
    assert replay["cache_hit"] and replay["transcript_id"] == result["transcript_id"] and len(calls) == 1
    raw_before = Path(result["raw_transcript_path"]).read_bytes()
    original = reader.video.observe([], "Capacity is 25 litres.", segment_ids=[segment["segment_id"]])
    revised = reader.video.observe([], "Capacity is at least 25 litres.", original["observation_id"],
                                   "Correcting my interpretation of the comparison.", [segment["segment_id"]])
    assert revised["evidence"][0]["supersedes"] == original["observation_id"]
    assert revised["evidence"][0]["text"] == segment["text"]
    assert Path(result["raw_transcript_path"]).read_bytes() == raw_before
    frame = {"frame_id": "a" * 64, "source_path": info["source_path"],
             "source_sha256": info["source_sha256"], "timestamp_s": 6, "frame_sha256": "b" * 64}
    save_json(reader.video.evidence_root / "frames" / f"{frame['frame_id']}.json", frame)
    mixed = reader.video.observe([frame["frame_id"]], "Compare speech with the displayed units.",
                                 segment_ids=[segment["segment_id"]])
    backend = Backend(tmp_path / "workspace")
    try:
        assert backend.import_dataset(result["segments_path"], name="transcript")["status"] == "success"
        assert backend.import_dataset(revised["path"], name="observation")["status"] == "success"
        assert backend.import_dataset(mixed["path"], name="mixed_evidence")["status"] == "success"
        assert backend.integrity_report()["ok"]
    finally:
        backend.close()


def test_no_audio_bad_track_bounds_and_unknown_segments_are_refused(reader, monkeypatch):
    info = fake_media(reader, monkeypatch)
    monkeypatch.setattr(reader, "_recognize", lambda *args: pytest.fail("invalid requests must not run ASR"))
    for args in ({"start_s": -1}, {"duration_s": 181}, {"duration_s": 0}, {"start_s": 10},
                 {"start_s": float("nan")}, {"duration_s": float("inf")}, {"audio_stream": 1}):
        with pytest.raises(ValueError):
            reader.transcribe("clip.mp4", **args)
    info["has_audio"] = False
    with pytest.raises(ValueError, match="no audio track"):
        reader.transcribe("clip.mp4")
    with pytest.raises(ValueError, match="Unknown"):
        reader.video.observe([], "Claim", segment_ids=["f" * 64])


def test_empty_transcript_and_invalid_alignment_are_not_fabricated(reader, monkeypatch):
    fake_media(reader, monkeypatch)
    monkeypatch.setattr(reader, "_recognize", lambda *args: {"language": "en", "segments": []})
    result = reader.transcribe("clip.mp4", duration_s=3)
    assert result["segments"] == [] and result["segments_path"] is None
    assert "does not prove silence" in result["coverage"]
    bad = recognition()
    bad["segments"][0]["end"] = 999
    monkeypatch.setattr(reader, "_recognize", lambda *args: bad)
    with pytest.raises(ValueError, match="outside the clip"):
        reader.transcribe("clip.mp4", start_s=1, duration_s=3)
    assert len(list(reader.root.glob("*.raw.json"))) == 1


def test_recognizer_is_offline_and_timeout_has_no_committed_transcript(reader, monkeypatch, tmp_path):
    def expire(command, **kwargs):
        assert kwargs["timeout"] == 300
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert "--model-dir" in command
        raise subprocess.TimeoutExpired(command, 300)
    monkeypatch.setattr(audio_module.subprocess, "run", expire)
    with pytest.raises(ValueError, match="ASR timed out"):
        reader._recognize(tmp_path / "audio.wav", tmp_path / "output.json", None)
    assert not list(reader.root.glob("*.json"))


def test_audio_mcp_is_opt_in_and_returns_segment_records(reader, monkeypatch):
    plain = create_server(reader.video)
    assert "transcribe_audio" not in [tool.name for tool in asyncio.run(plain.list_tools())]
    fake_media(reader, monkeypatch)
    monkeypatch.setattr(reader, "_recognize", lambda *args: recognition())
    server = create_server(reader.video, reader)
    result = asyncio.run(server.call_tool("transcribe_audio", {"path": "clip.mp4", "start_s": 4, "duration_s": 3}))
    assert not result.is_error and result.structured_content["segments"][0]["start_s"] == 4.5


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="real audio decoding is checked inside the host image")
def test_real_audio_track_selection_and_delayed_stream_alignment(reader, monkeypatch):
    import array

    source = reader.video.context_root / "tracks.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=4:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-itsoffset", "1.5", "-f", "lavfi", "-i", "sine=frequency=880:duration=1.5",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-c:a", "pcm_s16le",
                    "-output_ts_offset", "5", str(source)], check=True)
    info = reader.video.inspect("tracks.mkv")
    assert len(info["audio_streams"]) == 2 and float(info["video_stream"]["start_time"]) == 5

    def recognize(audio, output, language):
        with wave.open(str(audio), "rb") as wav:
            samples = array.array("h", wav.readframes(wav.getnframes()))
            assert wav.getframerate() == 16000 and wav.getnchannels() == 1
        # Clip [1,3] on the video clock starts half a second before track 2.
        assert max(abs(x) for x in samples[:6400]) == 0
        assert max(abs(x) for x in samples[9600:]) > 1000
        return {"language": "en", "segments": [{"start": 0.6, "end": 1, "text": "fixture"}]}

    monkeypatch.setattr(reader, "_recognize", recognize)
    result = reader.transcribe("tracks.mkv", start_s=1, duration_s=2, audio_stream=1)
    assert result["segments"][0]["start_s"] == 1.6 and result["segments"][0]["end_s"] == 2
