"""Fakes shared by the runtime unit tests: no real processes, no real clock."""

from __future__ import annotations

from pathlib import Path
import subprocess

from mco.runtime.core import Runtime
from mco.runtime.paths import RuntimePaths
from mco.runtime.supervision import ServiceProcess


SERVICES = [
    ServiceProcess("gateway", ("gateway-program", "serve")),
    ServiceProcess("scheduler", ("scheduler-program",)),
]


def fleet(*names: str, mode: str = "poll") -> str:
    return "".join(
        f'[workers.{name}]\nrole = "example"\nmode = "{mode}"\nexec = "{name}-program"\n\n'
        for name in names
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class SupervisorAlreadyRunning(RuntimeError):
    pass


class FakeAdapter:
    def __init__(self, *, already_running: bool = False) -> None:
        self.already_running = already_running
        self.spawns: list[tuple[list[str], dict[str, str], FakeProcess]] = []
        self.released = 0

    def acquire_supervisor_lock(self) -> None:
        if self.already_running:
            raise SupervisorAlreadyRunning("already running for this user; holding pid 1")

    def release_supervisor_lock(self) -> None:
        self.released += 1

    def bind_to_parent_lifetime(self) -> None:
        pass

    def reap_orphans(self, pidfile: Path) -> list[int]:
        return []

    def spawn(self, argv, cwd, env, stdout, stderr) -> FakeProcess:
        process = FakeProcess(5000 + len(self.spawns))
        self.spawns.append((list(argv), dict(env), process))
        return process

    def programs(self) -> list[str]:
        return [argv[0] for argv, _, _ in self.spawns]

    def alive(self) -> list[str]:
        return sorted(argv[0] for argv, _, process in self.spawns if process.poll() is None)

    def latest(self, program: str) -> FakeProcess:
        return [process for argv, _, process in self.spawns if argv[0] == program][-1]


def make_runtime(home: Path, clock: FakeClock | None = None, **kwargs) -> Runtime:
    options = {
        "adapter": FakeAdapter(),
        "services": SERVICES,
        "serve_control": False,
        "control_port": 1,
        "tick_interval": 0.0,
        "say": lambda message: None,
    }
    options.update(kwargs)
    runtime = Runtime(RuntimePaths(home), clock=clock or FakeClock(), **options)
    runtime.config.settle = 0.0
    return runtime
