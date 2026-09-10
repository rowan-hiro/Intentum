"""Opt-in stdio MCP tools for visual reading and separately configured offline ASR."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import CallToolResult, TextContent

from .video import VideoReader
from .audio import AudioReader

INSTRUCTIONS = """\
Video perception is available beside the backend tools. Use backend list_artifacts
to find videos, inspect_video for duration, then read_video_frames at chosen times.
The images are tool attachments; read them yourself. Sample across the duration and
inspect transitions or unclear text more closely, at up to 1920 pixels if needed.
The frame tools decode images deterministically; they do not interpret video or
hear audio. If evidence is missing or unclear, report the gap instead of guessing.
Before using a video-derived condition, record_observation with the frame ids and
your reading, including relevant comparisons and units as shown. Import the saved
JSON using backend import_dataset so the backend records its provenance. A saved
observation is your interpretation, not verification of it: compare it with the
images and the request before dependent calls. Correct it with supersedes and a
reason, retaining the previous reading. Use the backend for all data operations.
"""

AUDIO_INSTRUCTIONS = """\
Offline speech transcription is also enabled. When inspect_video reports audio,
use transcribe_audio to read relevant intervals (up to 180 seconds per call;
audio_stream selects a zero-based audio-track ordinal). Cover the full audio when
its relevance is unknown. Language is detected from audio unless specified.
Import the returned segments_path with backend import_dataset before relying on
the transcript. Speech recognition and its timestamps are estimates, not verified
facts. Compare important words, numbers and comparisons with frames at the cited
times, and record unresolved conflicts rather than silently choosing one reading.
record_observation accepts segment_ids beside frame_ids (frame_ids may be empty).
Use it to state your interpretation or correct an earlier observation with a
reason; raw ASR output stays intact. Do not treat an empty transcript as proof
that the audio contains no relevant information.
"""


def create_server(reader: VideoReader, audio: AudioReader | None = None) -> MCPServer:
    server = MCPServer(name="harness-perception", instructions=INSTRUCTIONS + (AUDIO_INSTRUCTIONS if audio else ""))

    if audio is not None:
        @server.tool()
        def transcribe_audio(path: str, start_s: float = 0, duration_s: float = 90,
                             audio_stream: int = 0, language: str | None = None) -> dict[str, Any]:
            """Transcribe a video's selected audio track offline, at most 180 seconds per call. Returns estimated video-clock segment times, source/model hashes and importable JSON. Empty output is not verified silence. Audio tracks are zero-based; omit language for detection from audio."""
            return audio.transcribe(path, start_s, duration_s, audio_stream, language)

    @server.tool()
    def inspect_video(path: str) -> dict[str, Any]:
        """Inspect a local video inside the supplied context: duration, dimensions, audio presence and reading limits."""
        return reader.inspect(path)

    @server.tool(structured_output=False)
    def read_video_frames(path: str, timestamps_s: list[float], max_dimension: int = 1280) -> CallToolResult:
        """Read 1–6 actual images at chosen seconds from the start. Returns frame ids, actual timestamps and PNG attachments in matching order. max_dimension is 320–1920."""
        body = reader.frames(path, timestamps_s, max_dimension)
        return CallToolResult(
            content=[TextContent(text=json.dumps(body, ensure_ascii=False)),
                     *[Image(path=frame["image_path"]).to_image_content() for frame in body["frames"]]],
            structured_content=body,
        )

    @server.tool()
    def record_observation(frame_ids: list[str], statement: str, supersedes: str | None = None,
                           reason: str | None = None, segment_ids: list[str] | None = None) -> dict[str, Any]:
        """Save your reading of returned frames or speech segments as importable JSON with source hashes and timestamps. For speech alone use frame_ids=[] and segment_ids from transcribe_audio. Revise with supersedes and reason; old observations and raw ASR are retained."""
        return reader.observe(frame_ids, statement, supersedes, reason, segment_ids)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--asr-model", type=Path, help="prepared offline model directory; omit to disable transcription")
    args = parser.parse_args()
    reader = VideoReader(Path(args.context_root), Path(args.evidence_root))
    audio = AudioReader(reader, args.asr_model) if args.asr_model else None
    create_server(reader, audio).run(transport="stdio")


if __name__ == "__main__":
    main()
