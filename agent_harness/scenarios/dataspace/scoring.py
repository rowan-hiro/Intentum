"""Scoring a prediction: the official evaluator, and the champion repository's scorer when present."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_harness.scenarios.dataspace import EVALUATOR


def run_official_evaluator(pred_root: Path, benchmark: Path, task: str, out_dir: Path) -> dict[str, Any]:
    """Run the vendored evaluator on ``pred_root/<task>/prediction.csv`` against the benchmark's gold table."""
    config_root = out_dir / "configs"
    config_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(benchmark / "evaluation" / "configs" / f"{task}.json", config_root / f"{task}.json")
    summary_path = out_dir / "evaluation_summary.json"
    cmd = [sys.executable, str(EVALUATOR), "--prediction-root", str(pred_root), "--gold-root", str(benchmark / "output"),
           "--config-root", str(config_root), "--output", str(summary_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    return {"command": " ".join(cmd), "returncode": proc.returncode, "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-2000:], "summary": summary}


def run_champion_scorer(pred_root: Path, benchmark: Path, task: str, champion: Path | None) -> dict[str, Any] | None:
    """The champion repository's column-signature scorer, run in its own environment; None when absent."""
    if champion is None:
        return None
    scorer = champion / "src" / "data_agent_baseline" / "run_compare_to_gt.py"
    if not scorer.exists():
        return None
    code = (
        "import json, sys; sys.path.insert(0, %r); from run_compare_to_gt import score_task; "
        "from pathlib import Path; print(json.dumps(score_task(%r, Path(%r), Path(%r), write_score=False), default=str))"
        % (str(scorer.parent), task, str(benchmark / "output"), str(pred_root))
    )
    proc = subprocess.run(["uv", "run", "--directory", str(champion), "python", "-c", code], capture_output=True, text=True)
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        result = None
    return {"returncode": proc.returncode, "result": result, "stderr": proc.stderr[-1000:] if result is None else ""}


def official_verdict(summary: dict[str, Any], task: str) -> dict[str, Any] | None:
    """The task's entry in the evaluator summary (``passed``, row/column counts, error)."""
    for entry in summary.get("tasks", []):
        if entry.get("task_id") == task:
            return entry
    return None
