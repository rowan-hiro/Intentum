"""Harness configuration: the repository root, ``.env`` and the model gateway.

Settings are read from the process environment first, then from a ``.env``
file at the repository root (git-ignored). The model gateway is described by
three settings:

    DEFAULT_MODEL_API_URL   OpenAI-compatible chat completions base URL
    DEFAULT_MODEL_API_KEY   bearer token
    DEFAULT_MODEL_NAME      model identifier

Scenarios add their own settings (benchmark and comparison paths) on top.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

MODEL_KEYS = ("DEFAULT_MODEL_API_URL", "DEFAULT_MODEL_API_KEY", "DEFAULT_MODEL_NAME")


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """``KEY=value`` lines; blank lines and ``#`` comments are ignored."""
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def setting(name: str, default: str | None = None) -> str | None:
    """One setting: the process environment wins over ``.env``; empty counts as unset."""
    value = os.environ.get(name)
    if not value:
        value = load_env_file().get(name)
    return value if value else default


def boolean_setting(name: str, default: bool = False) -> bool:
    value = setting(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true/false or 1/0, got {value!r}")


def configured_asr_model() -> Path | None:
    """Resolve repository-relative weights independently of the caller's cwd."""
    value = setting("HARNESS_ASR_MODEL")
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def model_config() -> dict[str, str]:
    values = {key: setting(key) for key in MODEL_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise SystemExit(f"missing model configuration: {missing} (set them in the environment or in {ENV_FILE})")
    return {"url": str(values["DEFAULT_MODEL_API_URL"]).rstrip("/"), "key": str(values["DEFAULT_MODEL_API_KEY"]),
            "model": str(values["DEFAULT_MODEL_NAME"])}
