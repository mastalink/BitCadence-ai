"""Read runtime evidence on the watchdog host without consulting local PIDs."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time

from mco.runtime.bootstrap import RESTART_COUNT
from mco.runtime.paths import (
    BOOTSTRAP_STATUS_FILE, HEARTBEAT_FILE, STALE_AFTER_SECONDS, read_json_object,
)


@dataclass(frozen=True)
class Observation:
    state: str
    alert: str | None = None
    age: float | None = None
    restarts: int | None = None


def age_of(document: dict, now: float) -> float | None:
    timestamp = document.get("timestamp")
    if type(timestamp) not in (float, int):
        return None
    try:
        age = now - timestamp
    except OverflowError:
        return None
    if not math.isfinite(age) or age < -STALE_AFTER_SECONDS:
        return None
    return max(0.0, age)


def check(runtime_dir: Path, *, now: float | None = None) -> Observation:
    # Read the heartbeat last so new runtime evidence can supersede an old
    # bootstrap event. Both timestamps are written on the runtime host.
    bootstrap = read_json_object(runtime_dir / BOOTSTRAP_STATUS_FILE) or {}
    heartbeat = read_json_object(runtime_dir / HEARTBEAT_FILE) or {}
    now = time.time() if now is None else now
    age = age_of(heartbeat, now)
    bootstrap_age = age_of(bootstrap, now)
    state = heartbeat.get("state")

    if age is not None and state != "stopped" and (
        state == "maintenance" or heartbeat.get("maintenance") is True
    ):
        return Observation("maintenance", age=age)

    if bootstrap_age is not None and (age is None or bootstrap["timestamp"] >= heartbeat["timestamp"]):
        phase = bootstrap.get("state")
        restarts = bootstrap.get("restarts")
        if phase == "stopped":
            return Observation("stopped")
        if phase == "exhausted" and restarts == RESTART_COUNT:
            return Observation("down", "recovery-failed", age, RESTART_COUNT)
        if phase in ("starting", "retrying"):
            if bootstrap_age <= STALE_AFTER_SECONDS:
                return Observation("recovering")
            if state == "stopped":
                return Observation("stale", "heartbeat-stale", bootstrap_age)

    if age is None:
        return Observation("unavailable", "heartbeat-unavailable")
    if state == "stopped":
        return Observation("stopped", age=age)
    if age > STALE_AFTER_SECONDS:
        return Observation("stale", "heartbeat-stale", age)
    if state in ("running", "quiesced", "upgrading", "crashed"):
        return Observation(state, age=age)
    return Observation("unknown", "runtime-state-unknown", age)
