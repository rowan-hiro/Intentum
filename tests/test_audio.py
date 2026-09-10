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
            "has_audio": True, "video_stream": {"start_time": "0"},
            "audio_streams": [{"index": 1, "start_time": "0", "duration": "10"}],
            "format": {"start_time": "0", "duration": "10"}}
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
        assert kwargs["stdin"] == subprocess.DEVNULL
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


def test_selected_track_bounds_fallback_and_cache_provenance(reader, monkeypatch):
    info = fake_media(reader, monkeypatch)
    info["duration_s"] = 5
    info["audio_streams"] = [{"start_time": "0", "duration": "20"},
                             {"start_time": "2", "duration": "5"}]
    info["format"]["duration"] = "25"
    calls = []
    monkeypatch.setattr(reader, "_recognize", lambda *args: calls.append(args) or recognition())
    first = reader.transcribe("clip.mp4", start_s=12, duration_s=3)
    assert first["audio_bounds"] == {"start_s": 0, "end_s": 20,
                                     "end_source": "stream.duration", "end_is_estimate": False}
    raw = json.loads(Path(first["raw_transcript_path"]).read_text())["request"]
    assert raw["video_reader_sha256"] == digest(Path(audio_module.__file__).with_name("video.py"))
    assert reader.transcribe("clip.mp4", start_s=12, duration_s=3)["cache_hit"]
    assert len(calls) == 1
    original_digest = audio_module.digest
    monkeypatch.setattr(audio_module, "digest", lambda path: "f" * 64 if path.name == "video.py" else original_digest(path))
    changed_code = reader.transcribe("clip.mp4", start_s=12, duration_s=3)
    assert not changed_code["cache_hit"] and changed_code["transcript_id"] != first["transcript_id"]
    # Same clipped interval, changed clock origin: no reuse of a previous WAV.
    info["video_stream"]["start_time"] = "1"
    changed_clock = reader.transcribe("clip.mp4", start_s=12, duration_s=3)
    assert not changed_clock["cache_hit"] and len(calls) == 3
    assert changed_clock["audio_bounds"]["end_s"] == 19
    with pytest.raises(ValueError, match=r"track 1's end 6 s.*track start 1 s; stream.duration"):
        reader.transcribe("clip.mp4", start_s=6, audio_stream=1)
    info["audio_streams"][0]["duration"] = "N/A"
    fallback = reader.transcribe("clip.mp4", start_s=12, duration_s=3)
    assert fallback["audio_bounds"]["end_source"] == "format.duration"
    assert fallback["audio_bounds"]["end_is_estimate"] and fallback["audio_bounds"]["end_s"] == 24
    for value in (None, "N/A", "NaN", "inf", "-2", "0"):
        info["format"]["duration"] = value
        with pytest.raises(ValueError, match="track 0 has no usable stream or container duration"):
            reader.transcribe("clip.mp4", start_s=12, duration_s=3)


def test_reading_begins_at_the_track_and_earlier_intervals_are_refused(reader, monkeypatch):
    info = fake_media(reader, monkeypatch)
    info["audio_streams"] = [{"start_time": "3", "duration": "4"}]
    calls = []
    monkeypatch.setattr(reader, "_recognize", lambda *args: calls.append(args) or recognition())
    # The requested window opens three seconds before the track carries samples.
    clipped = reader.transcribe("clip.mp4", start_s=0, duration_s=7)
    assert clipped["clip_start_s"] == 3 and clipped["audio_bounds"]["start_s"] == 3
    assert clipped["segments"][0]["start_s"] == 3.5
    assert json.loads(Path(clipped["raw_transcript_path"]).read_text())["request"]["clip_start_s"] == 3
    # The narrowed interval carries the request's identity, so the read is reused.
    assert reader.transcribe("clip.mp4", start_s=3, duration_s=4)["cache_hit"]
    with pytest.raises(ValueError, match=r"\[0, 2\) s is entirely before audio track 0 starts at 3 s") as refusal:
        reader.transcribe("clip.mp4", start_s=0, duration_s=2)
    assert "unread, not silence" in str(refusal.value)
    assert "Request an interval inside [3, 7) s" in str(refusal.value)
    assert len(calls) == 1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="real audio decoding is checked inside the host image")
def test_real_delayed_track_is_read_without_leading_padding(reader, monkeypatch):
    import array

    source = reader.video.context_root / "delayed.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=320x240:r=4:d=8",
                    "-itsoffset", "2", "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
                    "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
    monkeypatch.setattr(reader, "_recognize", lambda *args: {"language": "en", "segments": []})
    result = reader.transcribe("delayed.mp4", start_s=0, duration_s=6)
    start = result["audio_bounds"]["start_s"]
    assert start == pytest.approx(2, abs=0.03)  # AAC encoder delay.
    assert result["clip_start_s"] == start
    assert result["clip_end_s"] - start == pytest.approx(result["decoded_duration_s"], abs=0.002)
    with wave.open(result["audio_path"], "rb") as wav:
        rate = wav.getframerate()
        samples = array.array("h", wav.readframes(wav.getnframes()))
    # The delay is not stood in for by two seconds of synthesized silence.
    leading = next((index for index, value in enumerate(samples) if value), len(samples))
    assert leading / rate < 0.1
    with pytest.raises(ValueError, match="entirely before audio track 0"):
        reader.transcribe("delayed.mp4", start_s=0, duration_s=1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="real audio decoding is checked inside the host image")
@pytest.mark.parametrize("offset", [0, 5])
def test_real_audio_outlasts_video_and_clamps_to_selected_track(reader, monkeypatch, offset):
    source = reader.video.context_root / "long-audio.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=320x240:r=4:d=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
                    "-itsoffset", "2", "-f", "lavfi", "-i", "sine=frequency=880:duration=5",
                    "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac",
                    "-output_ts_offset", str(offset), str(source)], check=True)
    info = reader.video.inspect("long-audio.mp4")
    assert info["duration_s"] == 5
    monkeypatch.setattr(reader, "_recognize", lambda *args: {"language": "en", "segments": []})
    for start, duration, expected_end in ((0, 10, 10), (4, 10, 14), (6, 3, 9), (12, 10, 20)):
        result = reader.transcribe("long-audio.mp4", start_s=start, duration_s=duration)
        assert result["clip_end_s"] == pytest.approx(expected_end, abs=0.002)
        with wave.open(result["audio_path"], "rb") as wav:
            assert wav.getnframes() / wav.getframerate() == pytest.approx(expected_end - start, abs=0.002)
            assert any(wav.readframes(wav.getnframes()))  # Real samples, not EOF padding.
    shorter = reader.transcribe("long-audio.mp4", start_s=4, duration_s=10, audio_stream=1)
    assert shorter["clip_end_s"] == pytest.approx(7, abs=0.002)
    assert shorter["audio_bounds"]["start_s"] == pytest.approx(2, abs=0.03)  # AAC encoder delay.
    with pytest.raises(ValueError, match=r"track 1's end .*stream.duration"):
        reader.transcribe("long-audio.mp4", start_s=8, audio_stream=1)


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
        # The request opens half a second before track 2, so the clip begins at
        # that track's own samples rather than at synthesized silence.
        assert max(abs(x) for x in samples[:6400]) > 1000
        return {"language": "en", "segments": [{"start": 0.6, "end": 1, "text": "fixture"}]}

    monkeypatch.setattr(reader, "_recognize", recognize)
    result = reader.transcribe("tracks.mkv", start_s=1, duration_s=2, audio_stream=1)
    assert result["clip_start_s"] == pytest.approx(1.5, abs=0.002)
    assert result["segments"][0]["start_s"] == pytest.approx(2.1, abs=0.002)
    assert result["segments"][0]["end_s"] == pytest.approx(2.5, abs=0.002)
    assert result["audio_bounds"]["end_source"] == "format.duration"
    assert result["audio_bounds"]["end_is_estimate"]
    monkeypatch.setattr(reader, "_recognize", lambda *args: {"language": "en", "segments": []})
    # Missing per-track duration: don't turn the container's longer duration
    # (including its timestamp offset) into padded, supposedly read audio.
    tail = reader.transcribe("tracks.mkv", start_s=2, duration_s=10, audio_stream=1)
    assert tail["clip_end_s"] == pytest.approx(3, abs=0.002)
    assert tail["decoded_duration_s"] == pytest.approx(1, abs=0.002)
    with pytest.raises(ValueError, match="track 1 yielded no samples"):
        reader.transcribe("tracks.mkv", start_s=4, duration_s=2, audio_stream=1)
