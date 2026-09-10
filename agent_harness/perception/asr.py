"""Isolated offline speech recognizer; the caller bounds its lifetime and captures its output."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from .video import save_json

OPTIONS = {"beam_size": 5, "temperature": 0.0, "vad_filter": True,
           "word_timestamps": True, "condition_on_previous_text": False}


def transcribe(model_dir: Path, audio: Path, language: str | None) -> dict:
    from faster_whisper import WhisperModel

    model = WhisperModel(str(model_dir), device="cpu", compute_type="int8", cpu_threads=4,
                         num_workers=1, local_files_only=True)
    segments, info = model.transcribe(str(audio), language=language, **OPTIONS)
    return {"language": info.language, "language_probability": info.language_probability,
            "duration_s": info.duration, "duration_after_vad_s": info.duration_after_vad,
            "segments": [asdict(segment) for segment in segments],
            "runtime": {name: version(name) for name in ("faster-whisper", "ctranslate2", "onnxruntime")},
            "options": OPTIONS, "device": "cpu", "compute_type": "int8"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language")
    args = parser.parse_args()
    save_json(args.output, transcribe(args.model_dir, args.audio, args.language))


if __name__ == "__main__":
    main()
