"""Where the runtime keeps its files, and the small facts read back from them."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
import time

import psutil


HEARTBEAT_SECONDS = 15.0
# DECISIONS.md section 4: a heartbeat older than this means the runtime hung or died.
STALE_AFTER_SECONDS = 90.0


@dataclass(frozen=True)
class RuntimePaths:
    """Everything lives under one BitCadence home (``~/.mco``)."""

    home: Path = field(default_factory=lambda: Path.home() / ".mco")

    @property
    def runtime(self) -> Path:
        return self.home / "runtime"

    @property
    def fleet(self) -> Path:
        return self.home / "fleet.toml"

    @property
    def heartbeat(self) -> Path:
        return self.runtime / "heartbeat.json"

    @property
    def maintenance(self) -> Path:
        """Presence means maintenance mode; it survives a restart or a crash."""
        return self.runtime / "maintenance"

    @property
    def lkg(self) -> Path:
        return self.runtime / "lkg"


def atomic_write(path: Path, content: bytes) -> None:
    """Replace ``path`` so a reader sees the old file or the new one, never part of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows refuses the swap while a reader has the target open.
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        temporary.unlink(missing_ok=True)


def read_heartbeat(paths: RuntimePaths) -> dict[str, object] | None:
    try:
        data = json.loads(paths.heartbeat.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_process_alive(beat: dict[str, object] | None) -> bool:
    """Whether the process that wrote the heartbeat still exists (a recycled pid does not count)."""
    if not beat:
        return False
    try:
        process = psutil.Process(int(beat["pid"]))  # type: ignore[arg-type]
        return abs(process.create_time() - float(beat["process_created"])) < 1.0  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


def observed_state(beat: dict[str, object] | None, *, now: float | None = None) -> str:
    """The state a client or watchdog should believe.

    ``stopped`` and ``crashed`` are final words written on the way out. Any other state is only believed while its
    writer is alive and recent; otherwise it is ``dead`` (process gone) or ``stale`` (alive but silent: a hang).
    """
    if beat is None:
        return "never-started"
    state = str(beat.get("state", "unknown"))
    if state in {"stopped", "crashed"}:
        return state
    if not heartbeat_process_alive(beat):
        return "dead"
    try:
        age = (time.time() if now is None else now) - float(beat["timestamp"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return "stale"
    return "stale" if age > STALE_AFTER_SECONDS else state
