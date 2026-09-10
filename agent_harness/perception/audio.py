"""Bounded video-audio extraction and offline ASR evidence, on the harness side only."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

from .asr import OPTIONS
from .prepare_asr import MODEL_FILES
from .video import VideoReader, _json, _run, digest, save_json

MAX_CLIP_SECONDS = 180


def _seconds(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _audio_bounds(info: dict, ordinal: int) -> dict:
    """Track endpoints on the video clock; container fallback is only an estimate."""
    stream, container = info["audio_streams"][ordinal], info.get("format", {})
    video_start = _seconds(info["video_stream"].get("start_time")) or 0
    container_start = _seconds(container.get("start_time")) or 0
    track_start = _seconds(stream.get("start_time"))
    if track_start is None:
        track_start = container_start
    duration = _seconds(stream.get("duration"))
    estimated = duration is None or duration <= 0
    if estimated:
        duration = _seconds(container.get("duration"))
    if duration is None or duration <= 0:
        raise ValueError(f"Audio track {ordinal} has no usable stream or container duration; its endpoint is unknown.")
    # Container duration can include timestamp offsets or other longer tracks.
    # Treat it as an upper estimate, and stop extraction at the actual audio EOF.
    end = (container_start if estimated else track_start) + duration - video_start
    start = track_start - video_start
    if start >= end:
        # Only a container fallback can land before the track's own start. The
        # extent is unknown, and no interval could be recommended from it.
        raise ValueError(f"Audio track {ordinal} starts at {start:g} s on the video clock, at or after the "
                         f"{end:g} s endpoint estimated from container duration; its extent is unknown.")
    return {"start_s": float(start), "end_s": float(end),
            "end_source": "format.duration" if estimated else "stream.duration",
            "end_is_estimate": estimated}


class AudioReader:
    def __init__(self, video: VideoReader, model_dir: Path, timeout_s: int = 300):
        self.video = video
        self.model_dir = model_dir.resolve(strict=True)
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        manifest_path = self.model_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("ASR model needs manifest.json; run agent_harness.perception.prepare_asr first.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files", {})
        if set(files) != set(MODEL_FILES):
            raise ValueError("ASR model manifest must name the required model files.")
        for name in MODEL_FILES:
            path = (self.model_dir / name).resolve(strict=True)
            if not path.is_relative_to(self.model_dir) or digest(path) != files[name]:
                raise ValueError(f"ASR model file does not match its manifest: {name}")
        self.model = manifest
        self.model_fingerprint = hashlib.sha256(_json(manifest).encode()).hexdigest()
        self.root = video.evidence_root / "audio"
        self.root.mkdir(exist_ok=True)

    def _recognize(self, audio: Path, output: Path, language: str | None) -> dict[str, Any]:
        command = [sys.executable, "-m", "agent_harness.perception.asr", "--model-dir", str(self.model_dir),
                   "--audio", str(audio), "--output", str(output)]
        if language is not None:
            command += ["--language", language]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=self.timeout_s,
                                    env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
        except subprocess.TimeoutExpired as error:
            raise ValueError("ASR timed out; request a shorter clip. No transcript was committed.") from error
        if result.returncode:
            raise ValueError("Offline ASR failed. Install the perception dependency group and verify the local model. "
                             + result.stderr[-1200:])
        return json.loads(output.read_text(encoding="utf-8"))

    def transcribe(self, path: str, start_s: float = 0, duration_s: float = 90,
                   audio_stream: int = 0, language: str | None = None) -> dict[str, Any]:
        if not math.isfinite(start_s) or start_s < 0 or not math.isfinite(duration_s) or not 0 < duration_s <= MAX_CLIP_SECONDS:
            raise ValueError(f"start_s must be nonnegative and duration_s in (0, {MAX_CLIP_SECONDS}], both finite.")
        if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
            raise ValueError("language must be a Whisper language code, or omitted for audio-based detection.")
        info = self.video.inspect(path)
        if not info["has_audio"]:
            raise ValueError("This video has no audio track. Use its frames; no speech transcript can be produced.")
        if not 0 <= audio_stream < len(info["audio_streams"]):
            raise ValueError(f"audio_stream is a zero-based audio-track ordinal; accepted: 0 to {len(info['audio_streams']) - 1}.")
        bounds = _audio_bounds(info, audio_stream)
        # Both endpoints are floats so that request identity, and the cached
        # transcript it addresses, cannot depend on a caller sending 3 or 3.0.
        end_s = float(min(start_s + duration_s, bounds["end_s"]))
        # Begin at the track's own samples. Silence synthesized ahead of its
        # first packet would otherwise be returned as if it had been heard.
        clip_start_s = float(max(start_s, bounds["start_s"]))
        if clip_start_s >= end_s:
            qualifier = "estimated " if bounds["end_is_estimate"] else ""
            if start_s >= bounds["end_s"]:
                raise ValueError(f"start_s={start_s:g} is at or beyond audio track {audio_stream}'s {qualifier}"
                                 f"end {bounds['end_s']:g} s on the video clock "
                                 f"(track start {bounds['start_s']:g} s; {bounds['end_source']}). "
                                 f"Request an interval inside [{bounds['start_s']:g}, {bounds['end_s']:g}) s; "
                                 f"its {qualifier}end comes from {bounds['end_source']}.")
            raise ValueError(f"[{start_s:g}, {end_s:g}) s is entirely before audio track {audio_stream} starts at "
                             f"{bounds['start_s']:g} s on the video clock; that interval is unread, not silence. "
                             f"Request an interval inside [{bounds['start_s']:g}, {bounds['end_s']:g}) s; "
                             f"its {qualifier}end comes from {bounds['end_source']}.")
        request = {"source_path": info["source_path"], "source_sha256": info["source_sha256"],
                   "clip_start_s": clip_start_s, "clip_end_s": end_s, "audio_stream": audio_stream,
                   "video_origin_s": float(_seconds(info["video_stream"].get("start_time")) or 0),
                   "audio_bounds": bounds,
                   "model_fingerprint": self.model_fingerprint, "language": language, "options": OPTIONS,
                   "reader_sha256": digest(Path(__file__)), "video_reader_sha256": digest(Path(__file__).with_name("video.py")),
                   "recognizer_sha256": digest(Path(__file__).with_name("asr.py"))}
        request_id = hashlib.sha256(_json(request).encode()).hexdigest()
        # Serialize ASR calls in one server: avoid multiplying model memory and
        # ensure retries read a fully committed transcript rather than rerun ASR.
        with self._lock:
            cached = self.root / f"{request_id}.json"
            if cached.is_file():
                return {**json.loads(cached.read_text(encoding="utf-8")), "cache_hit": True}
            return self._extract_and_transcribe(request, request_id, info, cached)

    def _extract_and_transcribe(self, request: dict, request_id: str, info: dict, cached: Path) -> dict:
        start, end = request["clip_start_s"], request["clip_end_s"]
        video_origin = request["video_origin_s"]
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            audio = Path(temporary) / "audio.wav"
            # Preserve the original track timing, move to the video's zero
            # origin, and pad gaps inside the track with silence before cutting
            # the requested interval. Neither edge is padded: the cut starts at
            # the track's own samples, and a container duration fallback can
            # overestimate its endpoint. ASR times add clip_start_s.
            _run(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-copyts",
                  "-i", info["source_path"], "-map", f"0:a:{request['audio_stream']}", "-vn", "-sn", "-dn",
                  "-af", f"asetpts=PTS-({video_origin})/TB,aresample=16000:async=1:first_pts=0,"
                  f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS",
                  "-ac", "1", "-c:a", "pcm_s16le", str(audio)], timeout=30)
            with wave.open(str(audio), "rb") as wav:
                audio_duration = wav.getnframes() / wav.getframerate()
            if audio_duration == 0:
                raise ValueError(f"Audio track {request['audio_stream']} yielded no samples in [{start:g}, {end:g}) s "
                                 "on the video clock. Container timing may overestimate its endpoint; "
                                 "this is an unread interval, not verified silence.")
            if not 0 < audio_duration <= end - start + 0.1:
                raise ValueError("Decoded audio duration is outside the requested bounds.")
            end = min(end, start + audio_duration)
            raw = self._recognize(audio, Path(temporary) / "raw.json", request["language"])
            audio_hash = digest(audio)
            audio_path = self.root / f"{audio_hash}.wav"
            transcript_id = hashlib.sha256(_json({"request": request, "audio_sha256": audio_hash, "raw": raw}).encode()).hexdigest()
            segments = []
            for segment in raw["segments"]:
                left, right = float(segment["start"]), float(segment["end"])
                if not math.isfinite(left) or not math.isfinite(right) or not 0 <= left <= right <= audio_duration + 0.1:
                    raise ValueError("ASR returned a segment outside the clip; no transcript was committed.")
                if not segment["text"].strip():
                    continue
                fact = {"transcript_id": transcript_id, "source_path": request["source_path"],
                        "source_sha256": request["source_sha256"], "audio_sha256": audio_hash,
                        "audio_stream": request["audio_stream"], "model_fingerprint": self.model_fingerprint,
                        "start_s": round(start + left, 6), "end_s": round(start + min(right, audio_duration), 6),
                        "text": segment["text"], "language": raw["language"], "interpretation_by": "asr",
                        "avg_logprob": segment.get("avg_logprob"), "no_speech_prob": segment.get("no_speech_prob")}
                fact["segment_id"] = hashlib.sha256(_json({"index": len(segments), **fact}).encode()).hexdigest()
                segments.append(fact)
            rows_path = self.root / f"{transcript_id}.segments.json"
            raw_path = self.root / f"{transcript_id}.raw.json"
            body = {"status": "success", "summary": f"ASR produced {len(segments)} speech segments; recognition is not verification.",
                    "transcript_id": transcript_id, "source_path": request["source_path"], "source_sha256": request["source_sha256"],
                    "clip_start_s": start, "clip_end_s": end, "video_duration_s": info["duration_s"],
                    "audio_bounds": request["audio_bounds"], "decoded_duration_s": audio_duration,
                    "audio_stream": request["audio_stream"], "audio_sha256": audio_hash, "audio_path": str(audio_path),
                    "model": self.model, "model_fingerprint": self.model_fingerprint,
                    "language": raw["language"], "language_probability": raw.get("language_probability"),
                    "segments": segments, "segments_path": str(rows_path) if segments else None,
                    "raw_transcript_path": str(raw_path), "cache_hit": False,
                    "coverage": "Only the selected track and interval were processed. Segment times are ASR estimates on the video clock. "
                                "audio_bounds describes that track; end_is_estimate marks a container-duration fallback. "
                                "clip_start_s and clip_end_s are the decoded interval: reading begins at the track's own "
                                "samples and the audio may outlast the video. "
                                "No detected speech does not prove silence; unclear words and numbers need review against frames or another reading.",
                    "next_step": ("Import segments_path with backend import_dataset before using the transcript. "
                                  "Cite segment_ids in record_observation for an interpretation or correction; keep raw ASR unchanged."
                                  if segments else "No segment table was created. Check another interval or the video frames; "
                                  "an empty ASR result alone does not establish silence.")}
            audio.replace(audio_path)
            save_json(raw_path, {"request": request, "audio_sha256": audio_hash, "model": self.model, "recognition": raw})
            if segments:
                save_json(rows_path, segments)
            for fact in segments:
                save_json(self.video.evidence_root / "segments" / f"{fact['segment_id']}.json", fact)
            save_json(cached, body)
            return body
