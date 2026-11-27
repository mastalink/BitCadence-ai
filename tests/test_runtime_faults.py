"""Fault injection against a real runtime process with real children (Windows).

One test per required fault: kill the gateway, corrupt fleet.toml, set STOP, stop deliberately, kill the runtime.
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
import psutil
import pytest

from mco.runtime.paths import RuntimePaths, read_heartbeat


pytestmark = pytest.mark.skipif(os.name != "nt", reason="real-process runtime faults run on Windows")

ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).with_name("runtime_driver.py")

# Starts a grandchild, records both pids, then sleeps. argv[1] names the record.
STUB = """
import os, subprocess, sys, time
from pathlib import Path
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
record = Path(__file__).with_name(sys.argv[1] + ".pids")
record.with_suffix(".tmp").write_text(f"{os.getpid()} {grandchild.pid}")
os.replace(record.with_suffix(".tmp"), record)
time.sleep(300)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, what: str, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


class Live:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths = RuntimePaths(root / ".mco")
        self.paths.home.mkdir()
        self.stop_file = root / "STOP"
        self.stub = root / "stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        (root / ".env").write_text("", encoding="utf-8")
        # Nothing of this machine's own deployment may reach the runtime under test.
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("MCO_", "AGENT_", "SUPABASE_"))
        }
        self.env.update(
            HOME=str(root), USERPROFILE=str(root), PYTHONPATH=str(ROOT / "src"),
            MCO_STOP_FILE=str(self.stop_file), MCO_ENV_FILE=str(root / ".env"),
        )
        self.process: subprocess.Popen | None = None
        self.seen: set[int] = set()

    def start(self, kind: str = "stub", argument: str | None = None) -> None:
        with open(self.root / "runtime.out", "ab") as output:
            self.process = subprocess.Popen(
                [sys.executable, str(DRIVER), uuid.uuid4().hex, str(free_port()), kind,
                 argument or str(self.stub)],
                cwd=self.root, env=self.env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )

    def beat(self) -> dict:
        assert self.process is not None and self.process.poll() is None, (
            f"runtime exited: {(self.root / 'runtime.out').read_text(errors='replace')}"
        )
        return read_heartbeat(self.paths) or {}

    def child_pid(self, name: str, *, not_pid: int | None = None) -> int:
        def look():
            pid = self.beat().get("children", {}).get(name, {}).get("pid")
            return pid if pid and pid != not_pid else None

        pid = wait_for(look, f"a pid for {name}")
        self.seen.add(pid)
        return pid

    def tree(self, record: str) -> tuple[int, int]:
        path = self.root / f"{record}.pids"
        wait_for(path.exists, f"{record} to start")
        child, grandchild = (int(item) for item in path.read_text().split())
        self.seen.update((child, grandchild))
        return child, grandchild

    def fleet(self, *names: str) -> str:
        # Unquoted on purpose: neither path may contain a space.
        exe, stub = Path(sys.executable).as_posix(), self.stub.as_posix()
        if " " in exe + stub:
            pytest.skip("interpreter or temporary path contains a space")
        return "".join(
            f"[workers.{name}]\nrole = \"example\"\nmode = \"poll\"\n"
            f"exec = \"{exe} {stub} {name}\"\n\n" for name in names
        )

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "mco.cli", "runtime", *args], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()  # the Job Object takes the children with it
            self.process.wait(timeout=20)
        for pid in self.seen:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


@pytest.fixture
def live(tmp_path):
    instance = Live(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def test_kill_gateway_is_restarted(live):
    port = free_port()
    live.start("gateway", str(port))

    def healthy() -> bool:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1, trust_env=False).status_code == 200
        except httpx.HTTPError:
            return False

    first = live.child_pid("runtime:gateway")
    wait_for(healthy, "the supervised gateway to answer", 90)
    psutil.Process(first).kill()
    second = live.child_pid("runtime:gateway", not_pid=first)
    wait_for(healthy, "the restarted gateway to answer", 90)
    assert second != first


def test_gateway_and_scheduler_run_together_on_a_custom_port(live):
    port = free_port()
    live.start("gateway-scheduler", str(port))
    gateway_pid = live.child_pid("runtime:gateway")
    scheduler_pid = live.child_pid("runtime:scheduler")

    def healthy():
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1, trust_env=False)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    wait_for(healthy, "the gateway to answer while the scheduler is running", 90)
    scheduler = psutil.Process(scheduler_pid)
    assert scheduler.cmdline()[-2:] == ["--gateway-url", f"http://127.0.0.1:{port}"]
    assert not gone(gateway_pid) and not gone(scheduler_pid)

    stop = live.cli("stop")
    assert stop.returncode == 0, stop.stdout + stop.stderr
    assert live.process.wait(timeout=30) == 0
    wait_for(lambda: gone(gateway_pid) and gone(scheduler_pid), "both services to stop", 20)


def test_corrupt_fleet_toml_keeps_the_last_known_good(live):
    live.paths.fleet.write_text(live.fleet("alpha"), encoding="utf-8")
    live.start()
    alpha = live.tree("alpha")
    generation = live.beat()["config_generation"]
    live.paths.fleet.write_text("[workers.alpha\nrole = ", encoding="utf-8")
    wait_for(lambda: live.beat().get("config_error"), "the invalid edit to be reported")
    assert live.beat()["config_generation"] == generation
    assert live.child_pid("alpha") and not any(gone(pid) for pid in alpha)
    assert len(list(live.paths.lkg.glob("*.toml"))) == 1


def test_stop_file_means_no_spawns_and_no_exit(live):
    live.stop_file.write_text("{}", encoding="utf-8")
    live.paths.fleet.write_text(live.fleet("alpha"), encoding="utf-8")
    live.start()
    wait_for(lambda: live.beat().get("state") == "quiesced", "the quiesced heartbeat")
    time.sleep(3)
    beat = live.beat()  # also asserts the process is still alive
    assert beat["state"] == "quiesced"
    assert [child["pid"] for child in beat["children"].values()] == [None, None]
    assert not list(live.root.glob("*.pids"))
    live.stop_file.unlink()
    live.tree("alpha")
    live.tree("service")


def test_deliberate_stop_exits_zero_and_stops_the_children(live):
    live.paths.fleet.write_text(live.fleet("alpha"), encoding="utf-8")
    live.start()
    tree = live.tree("alpha") + live.tree("service")
    wait_for(lambda: live.beat().get("state") == "running", "the runtime")
    result = live.cli("stop")
    assert result.returncode == 0, result.stdout + result.stderr
    assert live.process.wait(timeout=30) == 0
    assert read_heartbeat(live.paths)["state"] == "stopped"
    # Direct children are stopped by the supervisor; the Job Object closes with the process and takes the rest.
    wait_for(lambda: all(gone(pid) for pid in tree), "every child and grandchild to exit", 20)


def test_cli_maintenance_on_a_live_runtime(live):
    live.start()
    first = live.child_pid("runtime:stub")
    assert live.cli("maintenance", "on").returncode == 0
    wait_for(lambda: gone(first), "the child to stop")
    assert live.beat()["state"] == "maintenance"
    assert live.cli("maintenance", "off").returncode == 0
    live.child_pid("runtime:stub", not_pid=first)


def test_kill_runtime_leaves_no_orphans(live):
    live.paths.fleet.write_text(live.fleet("alpha"), encoding="utf-8")
    live.start()
    tree = live.tree("alpha") + live.tree("service")
    assert not any(gone(pid) for pid in tree)
    live.process.kill()
    live.process.wait(timeout=20)
    wait_for(lambda: all(gone(pid) for pid in tree), "every child and grandchild to die", 20)
