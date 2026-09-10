"""Opt-in stdio MCP tools for the host's visual reading. No model is loaded here."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.types import CallToolResult, TextContent

from .video import VideoReader

INSTRUCTIONS = """\
Video perception is available beside the backend tools. Use backend list_artifacts
to find videos, inspect_video for duration, then read_video_frames at chosen times.
The images are tool attachments; read them yourself. Sample across the duration and
inspect transitions or unclear text more closely, at up to 1920 pixels if needed.
These tools decode images deterministically; they do not interpret video or hear
audio. If the evidence is missing or unclear, report the gap instead of guessing.
Before using a video-derived condition, record_observation with the frame ids and
your reading, including relevant comparisons and units as shown. Import the saved
JSON using backend import_dataset so the backend records its provenance. A saved
observation is your interpretation, not verification of it: compare it with the
images and the request before dependent calls. Correct it with supersedes and a
reason, retaining the previous reading. Use the backend for all data operations.
"""


def create_server(reader: VideoReader) -> MCPServer:
    server = MCPServer(name="harness-perception", instructions=INSTRUCTIONS)

    @server.tool()
    def inspect_video(path: str) -> dict:
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
                           reason: str | None = None) -> dict:
        """Save your reading of previously returned frames as importable JSON with source hashes and timestamps. Revise by citing the old observation id in supersedes and explaining the reason; old evidence is retained."""
        return reader.observe(frame_ids, statement, supersedes, reason)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context-root", required=True)
    parser.add_argument("--evidence-root", required=True)
    args = parser.parse_args()
    create_server(VideoReader(Path(args.context_root), Path(args.evidence_root))).run(transport="stdio")


if __name__ == "__main__":
    main()
