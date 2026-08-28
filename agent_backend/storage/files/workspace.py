"""Managed workspace directory.

All physical state lives under one directory that the backend owns:

    <root>/
      metadata.sqlite      backend metadata (datasets, lineage, operations, audit)
      analytics.duckdb     analytical tables
      files/               imported source files, copied in and content-addressed

Agents never see these paths.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.files_dir = self.root / "files"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)

    @property
    def metadata_path(self) -> Path:
        return self.root / "metadata.sqlite"

    @property
    def analytics_path(self) -> Path:
        return self.root / "analytics.duckdb"

    @staticmethod
    def content_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def import_file(self, source: Path, content_hash: str) -> Path:
        """Copy a source file into the managed area, keyed by content hash.

        Idempotent: re-importing identical content reuses the managed copy.
        """
        target = self.files_dir / f"{content_hash[:16]}{source.suffix.lower()}"
        if not target.exists():
            shutil.copy2(source, target)
        return target
