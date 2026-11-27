"""Last-known-good configuration generations: the newest five validated ``fleet.toml`` files."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from .paths import atomic_write


KEEP_GENERATIONS = 5
_NAME = re.compile(r"^(\d{8})-[0-9a-f]{12}\.toml$")


class LkgStore:
    def __init__(self, directory: Path, keep: int = KEEP_GENERATIONS) -> None:
        self.directory = directory
        self.keep = keep

    def generations(self) -> list[Path]:
        """Oldest first. The newest is the generation that was last accepted."""
        if not self.directory.is_dir():
            return []
        found = [path for path in self.directory.iterdir() if _NAME.match(path.name)]
        return sorted(found, key=lambda path: path.name)

    def save(self, content: bytes) -> None:
        existing = self.generations()
        if existing and existing[-1].read_bytes() == content:
            return
        sequence = int(existing[-1].name[:8]) + 1 if existing else 1
        digest = hashlib.sha256(content).hexdigest()[:12]
        atomic_write(self.directory / f"{sequence:08d}-{digest}.toml", content)
        for old in self.generations()[: -self.keep]:
            old.unlink(missing_ok=True)
