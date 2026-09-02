"""DataSpace (KDD Cup 2026 Data Agent track) as a validation scenario.

Paths come from the environment or the repository ``.env``:

    DATASPACE_BENCHMARK   benchmark package root (input/, output/, evaluation/)
    KDDCUP_CHAMPION       champion repository root, for the comparison scorer

Runs are written under ``runs/`` beside this file (git-ignored).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_harness.config import setting

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
EVALUATOR = HERE / "evaluate.py"


def benchmark_root(override: str | None = None) -> Path:
    return Path(override or setting("DATASPACE_BENCHMARK", "~/dev/kddcup2026_champion/DataSpace-Benchmark") or "").expanduser()


def champion_root(override: str | None = None) -> Path:
    return Path(override or setting("KDDCUP_CHAMPION", "~/dev/kddcup2026_champion") or "").expanduser()


def task_context(benchmark: Path, task: str) -> Path:
    return benchmark / "input" / task / "context"


def task_question(benchmark: Path, task: str) -> str:
    return json.loads((benchmark / "input" / task / "task.json").read_text(encoding="utf-8"))["question"]
