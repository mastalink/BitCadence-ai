"""Run the Windows scheduler drill with a disposable task, profile and mutex."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import psutil

from mco.agentd.tokens import current_user_sid
from mco.runtime.bootstrap import launch_runtime, register_task, task_xml, unregister_task
from mco.runtime.paths import heartbeat_process_alive, read_heartbeat
from tests.test_runtime_faults import DRIVER, Live, free_port, wait_for


def action(home: Path, suffix: str, control_port: str, gateway_port: str) -> int:
    for key in list(os.environ):
        if key.upper().startswith(("MCO_", "AGENT_", "SUPABASE_")):
            del os.environ[key]
    os.environ.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(ROOT / "src"),
                      MCO_STOP_FILE=str(home / "STOP"), MCO_ENV_FILE=str(home / ".env"))
    argv = [str(Path(sys.executable).with_name("python.exe")), str(DRIVER),
            suffix, control_port, "gateway", gateway_port]
    return launch_runtime(argv, home, home / ".mco" / "runtime")


def scheduler(name: str, operation: str, *, check: bool = True) -> None:
    subprocess.run(["schtasks.exe", operation, "/tn", name], check=check, capture_output=True,
                   text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)


def exercise(live: Live, evidence: Path, log: TextIO) -> None:
    suffix = uuid.uuid4().hex
    name = "BitCadence Runtime TEST-" + suffix
    sid = current_user_sid()
    control, gateway = free_port(), free_port()
    while gateway == control:
        gateway = free_port()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    argv = [str(Path(__file__).resolve()), "--action", str(live.root), suffix, str(control), str(gateway)]
    xml = task_xml(name, sid, pythonw, argv, live.root)

    def record(message: str) -> None:
        print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {message}", file=log, flush=True)

    def active(previous=None):
        beat = read_heartbeat(live.paths)
        if (beat and beat["pid"] != previous and beat["state"] == "running"
                and beat.get("children", {}).get("runtime:gateway", {}).get("pid")
                and heartbeat_process_alive(beat)):
            return beat
        return None

    exported = register_task(name, sid, xml)
    try:
        sanitized = (exported.replace("UTF-16", "UTF-8").replace(psutil.Process().username(), r"WORKSTATION\test-user")
                     .replace(sid, "S-1-5-21-0000000000-0000000000-0000000000-1000")
                     .replace(str(live.root), r"C:\Temp\BitCadence-isolated").replace(str(ROOT), r"C:\BitCadence"))
        (evidence / "bootstrap-task.xml").write_text(sanitized, encoding="utf-8")
        record(f"Registered {name}; isolated loopback ports control={control}, gateway={gateway}.")
        scheduler(name, "/run")
        beat = wait_for(active, "the scheduled runtime to start", 30)
        record(f"Initial start: runtime PID {beat['pid']}.")
        duplicate = subprocess.run(
            [sys.executable, str(DRIVER), suffix, str(control), "gateway", str(gateway)],
            env=live.env, cwd=live.root, timeout=30, capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert duplicate.returncode == 3 and active()["pid"] == beat["pid"], duplicate
        record("Second start refused: exit 3; original runtime still owns the isolated mutex.")

        late = False
        for attempt in range(1, 4):
            victim = psutil.Process(beat["pid"])
            assert heartbeat_process_alive(beat)
            crashed_at = time.time()
            victim.kill()
            victim.wait(timeout=5)
            beat = wait_for(lambda: active(victim.pid), f"automatic restart {attempt}", 80)
            launch_delay = beat["process_created"] - crashed_at
            ready_delay = time.time() - crashed_at
            late |= launch_delay > 61.0  # PT1M plus one second of dispatch latency.
            record(f"Crash {attempt}: PID {victim.pid} -> {beat['pid']}; "
                   f"runtime process launched at +{launch_delay:.2f}s; heartbeat observed at +{ready_delay:.2f}s.")

        stopped = live.cli("stop")
        assert stopped.returncode == 0, stopped.stderr
        wait_for(lambda: not heartbeat_process_alive(beat), "the stopped runtime to exit", 10)
        deadline = time.monotonic() + 66
        while time.monotonic() < deadline:
            assert (read_heartbeat(live.paths) or {}).get("pid") == beat["pid"]
            time.sleep(0.25)
        final = read_heartbeat(live.paths)
        assert final and final["state"] == "stopped" and final["pid"] == beat["pid"]
        record("Deliberate runtime stop: stayed stopped for 66s; no replacement runtime.")
        record("Reboot and sign-in check deferred to R0-05 activation.")
        assert not late, "runtime relaunch exceeded PT1M plus one second of dispatch latency"
    except Exception as exc:
        record(f"FAIL: {exc}")
        log_path = live.paths.runtime / "bootstrap.log"
        if log_path.exists():
            print(log_path.read_text(encoding="utf-8", errors="replace").replace(str(live.root), "<test-home>"),
                  file=log, flush=True)
        raise
    finally:
        scheduler(name, "/end", check=False)
        unregister_task(name, sid)
        wait_for(lambda: not heartbeat_process_alive(read_heartbeat(live.paths)), "test runtime cleanup", 20)
        record("Isolated TEST task unregistered and no test runtime remains.")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--action":
        home, suffix, control, gateway = sys.argv[2:]
        return action(Path(home), suffix, control, gateway)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("the scheduler drill requires Windows")
    args.evidence.mkdir(parents=True, exist_ok=True)
    with (args.evidence / "bootstrap-live-check.log").open("w", encoding="utf-8") as log:
        with tempfile.TemporaryDirectory(prefix="bitcadence-runtime-test-") as directory:
            exercise(Live(Path(directory)), args.evidence, log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
