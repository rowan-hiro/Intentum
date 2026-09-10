"""Check the actual MCP-image → OpenCode → configured model path with a fresh visual code.

    python -m agent_harness.hosts.vision_probe --out /absolute/path/to/new/probe --image IMAGE

The answer is generated locally and never appears in the model prompt, tool text
or mounted filenames. The model sees only a video whose pixels carry the code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
from dataclasses import asdict
from pathlib import Path

from agent_harness.config import model_config
from agent_harness.hosts.opencode import DEFAULT_IMAGE, run_opencode


def probe(out: Path, image: str) -> dict:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = out / "input"
    source.mkdir()
    expected = f"{secrets.randbelow(1000000):06d}"
    subprocess.run([
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{source}:/data", "--entrypoint", "ffmpeg", image,
        "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=960x480:r=4:d=2",
        "-vf", f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text={expected}:"
        "fontsize=110:fontcolor=black:x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "/data/clip.mp4",
    ], check=True, timeout=60)
    settings = model_config()
    ran = run_opencode(
        out / "run", prompt="Read the six-digit code visible in /data/context/clip.mp4 at 0.5 seconds. "
        "Call the video-frame tool to see the image, then reply CODE: followed by those six digits. "
        "If you cannot see the image, say so; do not guess.",
        system_prompt="You are checking visual input. Use the perception MCP tools to read the video pixels.",
        settings=settings, mounts={source: "/data/context"}, video_context="/data/context",
        image=image, timeout_s=180, title="visual-input-probe",
    )
    found = re.search(r"CODE:\s*(\d{6})", ran.final_text)
    read = any(e["tool"] == "perception_read_video_frames" and e["status"] == "success" for e in ran.tool_events)
    result = {"model": settings["model"], "gateway": settings["url"], "expected": expected,
              "passed": bool(read and found and found.group(1) == expected), "host_run": asdict(ran)}
    (out / "probe_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="a new directory; existing runs are never replaced")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    result = probe(args.out, args.image)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
