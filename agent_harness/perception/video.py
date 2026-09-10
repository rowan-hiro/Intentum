"""Deterministic video decoding and evidence storage; interpretation stays with the host model."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
MAX_FRAMES = 6
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 12 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _run(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise ValueError("Video reading requires ffmpeg and ffprobe on PATH.") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("Video decoding timed out; request fewer frames or a shorter local clip.") from error
    if result.returncode:
        raise ValueError(f"Video decoding failed: {result.stderr[-1500:]}")
    return result


class VideoReader:
    """Read only the supplied context; write content-addressed evidence only under this run."""

    def __init__(self, context_root: Path, evidence_root: Path):
        self.context_root = context_root.resolve(strict=True)
        self.evidence_root = evidence_root.resolve()
        if not self.context_root.is_dir():
            raise ValueError("context_root must be a directory")
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        for name in ("frames", "observations"):
            (self.evidence_root / name).mkdir(exist_ok=True)

    def source(self, path: str) -> Path:
        candidate = Path(path)
        resolved = (candidate if candidate.is_absolute() else self.context_root / candidate).resolve(strict=True)
        if not resolved.is_relative_to(self.context_root) or not resolved.is_file():
            raise ValueError("Video path must be a file inside the supplied task context, including after symlink resolution.")
        if resolved.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError(f"Accepted video extensions: {sorted(VIDEO_SUFFIXES)}")
        if resolved.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("This reader accepts local videos up to 512 MiB.")
        return resolved

    def inspect(self, path: str) -> dict[str, Any]:
        source = self.source(path)
        raw = json.loads(_run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,avg_frame_rate,start_time,duration",
            "-of", "json", str(source),
        ]).stdout)
        videos = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"]
        if not videos:
            raise ValueError("The file has no video stream.")
        stream = videos[0]
        duration = float(stream.get("duration") or raw.get("format", {}).get("duration") or 0)
        if not math.isfinite(duration) or not 0 < duration <= 3600:
            raise ValueError("This reader requires a known video duration of at most 3600 seconds.")
        return {
            "status": "success", "summary": "Video metadata only; no image or speech has been interpreted.",
            "source_path": str(source), "source_sha256": digest(source), "duration_s": duration,
            "video_stream": stream, "has_audio": any(s.get("codec_type") == "audio" for s in raw.get("streams", [])),
            "limits": {"frames_per_call": MAX_FRAMES, "max_dimension": 1920},
            "coverage": "Frames sample visible content only. Audio is not transcribed. Sampling can miss brief changes.",
        }

    def frames(self, path: str, timestamps_s: list[float], max_dimension: int = 1280) -> dict[str, Any]:
        if not 1 <= len(timestamps_s) <= MAX_FRAMES:
            raise ValueError(f"Request 1 to {MAX_FRAMES} timestamps per call.")
        if not 320 <= max_dimension <= 1920:
            raise ValueError("max_dimension must be between 320 and 1920.")
        info = self.inspect(path)
        if any(not math.isfinite(t) or not 0 <= t < info["duration_s"] for t in timestamps_s):
            raise ValueError(f"Each timestamp must be finite and in [0, {info['duration_s']}).")
        frames: list[dict[str, Any]] = []
        batch_bytes = 0
        for timestamp in timestamps_s:
            # Decode from the beginning so showinfo's PTS is the original stream
            # time, including for variable-rate video. Subtract its start offset
            # to put both the requested position and the evidence on a zero origin.
            start = float(info["video_stream"].get("start_time") or 0)
            with tempfile.TemporaryDirectory(dir=self.evidence_root) as temporary:
                output = Path(temporary) / "frame.png"
                result = _run([
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-protocol_whitelist", "file,pipe",
                    "-copyts", "-i", info["source_path"], "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-vf", f"select=gte(t\\,{timestamp + start}),showinfo,scale={max_dimension}:{max_dimension}:"
                    "force_original_aspect_ratio=decrease:flags=lanczos",
                    "-frames:v", "1", "-fps_mode", "vfr", "-threads", "1", str(output),
                ], timeout=12)
                pts = re.search(r"n:\s*0\s+pts:\s*\S+\s+pts_time:([\d.eE+-]+)", result.stderr)
                if not output.exists() or pts is None:
                    raise ValueError("No frame exists at or after that timestamp; choose an earlier position.")
                size = output.stat().st_size
                batch_bytes += size
                if size > MAX_IMAGE_BYTES or batch_bytes > MAX_BATCH_BYTES:
                    raise ValueError("Image response exceeds the byte limit; request fewer or smaller frames.")
                fact = {
                    "source_path": info["source_path"], "source_sha256": info["source_sha256"],
                    "requested_timestamp_s": timestamp, "timestamp_s": float(pts.group(1)) - start,
                    "frame_sha256": digest(output), "max_dimension": max_dimension,
                }
                frame_id = hashlib.sha256(_json(fact).encode()).hexdigest()
                fact.update(frame_id=frame_id, image_path=str(self.evidence_root / "frames" / f"{frame_id}.png"))
                output.replace(fact["image_path"])
                (self.evidence_root / "frames" / f"{frame_id}.json").write_text(_json(fact), encoding="utf-8")
                frames.append(fact)
        return {"status": "success", "summary": f"Decoded {len(frames)} frames. Images follow in this order.",
                "frames": frames, "coverage": info["coverage"]}

    def _record(self, kind: str, record_id: str) -> Any:
        if not re.fullmatch(r"[0-9a-f]{64}", record_id):
            raise ValueError("Evidence ids must be returned by an earlier reader call.")
        path = self.evidence_root / kind / f"{record_id}.json"
        if not path.is_file():
            raise ValueError(f"Unknown {kind} id: {record_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def observe(self, frame_ids: list[str], statement: str, supersedes: str | None = None,
                reason: str | None = None) -> dict[str, Any]:
        if not 1 <= len(frame_ids) <= 12 or not statement.strip() or len(statement) > 8000:
            raise ValueError("An observation needs 1 to 12 existing frame ids and a statement of 1 to 8000 characters.")
        if bool(supersedes) != bool(reason and reason.strip()):
            raise ValueError("A revision needs both supersedes (an earlier observation id) and a nonempty reason.")
        if supersedes:
            self._record("observations", supersedes)
        facts = [self._record("frames", frame_id) for frame_id in dict.fromkeys(frame_ids)]
        rows = [{**{k: fact[k] for k in ("frame_id", "source_path", "source_sha256", "timestamp_s", "frame_sha256")},
                 "statement": statement, "supersedes": supersedes, "revision_reason": reason,
                 "interpretation_by": "agent"} for fact in facts]
        observation_id = hashlib.sha256(_json(rows).encode()).hexdigest()
        rows = [{"observation_id": observation_id, **row} for row in rows]
        path = self.evidence_root / "observations" / f"{observation_id}.json"
        path.write_text(_json(rows), encoding="utf-8")
        return {"status": "success", "summary": "Saved the agent's interpretation with cited frames, without validating its meaning.",
                "observation_id": observation_id, "path": str(path), "evidence": rows,
                "next_step": "Use backend import_dataset on this JSON file before relying on the observation. "
                             "Compare the statement with the frames and request; revise with supersedes and reason if needed."}
