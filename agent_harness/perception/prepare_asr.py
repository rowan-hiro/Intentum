"""Prepare offline ASR weights from a local copy or pinned download, outside perception calls."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .video import digest, save_json

DEFAULT_REPO = "Systran/faster-whisper-medium"
DEFAULT_REVISION = "08e178d48790749d25932bbc082711ddcfdfbc4f"
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


def verify_model(model_dir: Path) -> dict:
    """Check the offline model before a run, without loading the recognizer."""
    model_dir = model_dir.resolve(strict=True)
    manifest_path = model_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("ASR model needs manifest.json; run agent_harness.perception.prepare_asr first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("ASR model manifest must be an object naming the required model files.")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or set(files) != set(MODEL_FILES):
        raise ValueError("ASR model manifest must name the required model files.")
    for name in MODEL_FILES:
        path = (model_dir / name).resolve(strict=True)
        if not path.is_relative_to(model_dir) or digest(path) != files[name]:
            raise ValueError(f"ASR model file does not match its manifest: {name}")
    return manifest


def copy_local(source: Path, out: Path) -> Path:
    source, out = source.resolve(strict=True), out.resolve()
    if out.exists():
        raise ValueError("Use a new destination directory for a local model copy.")
    hashes = {name: digest(source / name) for name in MODEL_FILES}
    out.mkdir(parents=True)
    for name, expected in hashes.items():
        shutil.copyfile(source / name, out / name)
        if digest(out / name) != expected:
            raise ValueError(f"The copied file failed SHA256 verification: {name}")
    save_json(out / "manifest.json", {"source_directory": str(source), "preparation": "verified_local_copy", "files": hashes})
    return out


def prepare(out: Path, repo: str = DEFAULT_REPO, revision: str = DEFAULT_REVISION) -> Path:
    from huggingface_hub import snapshot_download

    out = out.resolve()
    if (out / "manifest.json").exists():
        raise ValueError("A model manifest already exists here; use another directory for another model revision.")
    snapshot_download(repo_id=repo, revision=revision, local_dir=out,
                      allow_patterns=[*MODEL_FILES, "README.md"])
    manifest = {"repository": repo, "revision": revision,
                "files": {name: digest(out / name) for name in MODEL_FILES}}
    save_json(out / "manifest.json", manifest)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, help="copy an existing local model instead of downloading")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    args = parser.parse_args()
    print(copy_local(args.source_model, args.out) if args.source_model else prepare(args.out, args.repo, args.revision))


if __name__ == "__main__":
    main()
