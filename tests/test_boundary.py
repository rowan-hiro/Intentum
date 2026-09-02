"""The harness/backend boundary (MADR 0004, 0009), checked rather than assumed.

``agent_harness`` may import from ``agent_backend`` only its public API and the
MCP server factory; ``agent_backend`` imports nothing from the harness and names
no validation scenario; the harness is not shipped in the wheel.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "agent_backend"
HARNESS = ROOT / "agent_harness"

ALLOWED_BACKEND_MODULES = {"agent_backend", "agent_backend.mcp.server"}
PUBLIC_API = {"Backend", "BackendError", "ErrorCode"}
SCENARIO_WORDS = re.compile(r"dataspace|kddcup|champion|task_\d+", re.IGNORECASE)


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "runs" not in p.parts)


def imports(path: Path):
    """``(module, imported names or None)`` for every absolute import statement in the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, None
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module or "", [alias.name for alias in node.names]


def test_harness_packages_exist_beside_the_backend():
    assert (HARNESS / "__init__.py").is_file()
    assert (HARNESS / "perception" / "__init__.py").is_file()
    assert (HARNESS / "scenarios" / "__init__.py").is_file()
    assert not (ROOT / "examples" / "dataspace").exists()


def test_harness_uses_only_the_backend_public_surface():
    offenders = []
    for path in python_files(HARNESS):
        for module, names in imports(path):
            if module != "agent_backend" and not module.startswith("agent_backend."):
                continue
            if module not in ALLOWED_BACKEND_MODULES:
                offenders.append(f"{path.relative_to(ROOT)}: {module}")
            elif module == "agent_backend" and names is not None and not set(names) <= PUBLIC_API:
                offenders.append(f"{path.relative_to(ROOT)}: from agent_backend import {sorted(set(names) - PUBLIC_API)}")
    assert not offenders, "the harness reaches past the backend's public surface:\n" + "\n".join(offenders)


def test_backend_never_imports_the_harness():
    offenders = [
        str(path.relative_to(ROOT)) for path in python_files(BACKEND)
        if any(module == "agent_harness" or module.startswith("agent_harness.") for module, _ in imports(path))
    ]
    assert not offenders, "the backend imports the harness:\n" + "\n".join(offenders)


def test_backend_names_no_scenario():
    offenders = []
    for path in python_files(BACKEND):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if SCENARIO_WORDS.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, "the backend names a validation scenario:\n" + "\n".join(offenders)


def test_harness_is_not_shipped_in_the_wheel():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["agent_backend"]' in text
