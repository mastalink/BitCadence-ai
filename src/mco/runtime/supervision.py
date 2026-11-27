"""The agentd supervisor, extended for the runtime.

:class:`mco.agentd.supervisor.Supervisor` keeps the restart policy (backoff, crash-loop latch, cooldown) and the
STOP check before every spawn. This subclass adds what the runtime needs:

* the gateway and the scheduler as supervised children (a generic argv, not only worker modes);
* a *hold* (maintenance) during which nothing is spawned;
* an explicit identity on every spawn: ``AGENT_INSTANCE_ID`` is always set from the child's own configuration and
  never inherited from the runtime's environment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from threading import Event
from typing import Any, Mapping

from mco import quiet
from mco.agentd.supervisor import Supervisor, WorkerState
from mco.fleet import WorkerConfig


RESERVED_PREFIX = "runtime:"
IDENTITY_VARIABLES = ("AGENT_INSTANCE_ID", "AGENT_ROLE")


@dataclass(frozen=True)
class ServiceProcess:
    """A long-running child of the runtime that is not a fleet worker. One argv element each; never a shell."""

    name: str
    argv: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{RESERVED_PREFIX}{self.name}"

    def as_config(self) -> WorkerConfig:
        # "waker" gives the long-running policy: any exit is a failure and is restarted with backoff.
        return WorkerConfig(
            worker=self.key,
            role="runtime",
            instance=f"runtime-{self.name}",
            mode="waker",
            exec_command="\0".join(self.argv),  # only so a changed command restarts the child
            min_interval=1.0,
            poll_interval=1.0,
        )


def default_services(
    *, gateway: bool = True, scheduler: bool = True, gateway_port: int = 18789
) -> list[ServiceProcess]:
    # python.exe, never pythonw.exe: the adapter already hides the console, and children inherit that hidden console.
    python = quiet.hidden_python()
    services: list[ServiceProcess] = []
    if gateway:
        services.append(ServiceProcess(
            "gateway",
            (python, "-m", "mco.cli", "serve", "--host", "127.0.0.1", "--port", str(gateway_port)),
        ))
    if scheduler:
        argv = (python, "-m", "mco.runtime.scheduler_child")
        if gateway and gateway_port != 18789:
            argv += ("--gateway-url", f"http://127.0.0.1:{gateway_port}")
        services.append(ServiceProcess("scheduler", argv))
    return services


def child_environment() -> dict[str, str]:
    """The runtime's environment without any identity of its own."""
    return {key: value for key, value in os.environ.items() if key.upper() not in IDENTITY_VARIABLES}


def windowless_interpreter(argv: list[str]) -> list[str]:
    """Run a Python child under ``python.exe``, never ``pythonw.exe``.

    The adapter gives a console child a console with no window, and everything it starts inherits that. ``pythonw.exe``
    has no console, so the first console program below it opens a visible one.
    """
    if argv and os.path.basename(argv[0]).casefold() == "pythonw.exe":
        candidate = os.path.join(os.path.dirname(argv[0]), "python.exe")
        if os.path.exists(candidate):
            return [candidate, *argv[1:]]
    return argv


class RuntimeSupervisor(Supervisor):
    def __init__(
        self, adapter: Any, *, stopping: Event, services: list[ServiceProcess] | None = None, **kwargs: Any
    ) -> None:
        kwargs.setdefault("env_factory", child_environment)
        super().__init__(adapter, **kwargs)
        self.services: Mapping[str, ServiceProcess] = {item.key: item for item in services or []}
        self.held = False
        self._stopping = stopping

    def reconcile(self, configs: dict[str, WorkerConfig]) -> None:
        """Every path into the supervisor goes through here, so services cannot be reconciled away."""
        desired: dict[str, WorkerConfig] = {
            item.key: item.as_config() for item in self.services.values()
        }
        for name, config in configs.items():
            if name.startswith(RESERVED_PREFIX):
                raise ValueError(f"worker name reserved for the runtime: {name}")
            if not config.instance:
                config = replace(config, instance=name)
            if config.mode == "pane":
                # Pane workers belong to the optional terminal host, not to this supervisor.
                config = replace(config, mode="off")
            desired[name] = config
        super().reconcile(desired)

    def _argv(self, config: WorkerConfig) -> list[str]:  # type: ignore[override]
        service = self.services.get(config.worker)
        if service is not None:
            return list(service.argv)
        return windowless_interpreter(Supervisor._argv(config))

    def _spawn(self, runtime: Any) -> None:
        if self.held or self._stopping.is_set():
            return
        super()._spawn(runtime)

    def hold(self) -> None:
        """Stop every child and spawn nothing until :meth:`release`. Crash-loop latches are kept."""
        with self._lock:
            self.held = True
            drain = getattr(super(), "drain", None)
            if drain is not None:
                drain()
                return
            for name, runtime in list(self.workers.items()):
                if runtime.state != WorkerState.CRASHLOOPED:
                    self.stop(name)

    def release(self) -> None:
        with self._lock:
            self.held = False
            resume = getattr(super(), "resume", None)
            if resume is not None:
                resume()
                return
            for name, runtime in list(self.workers.items()):
                if runtime.config.mode != "off" and runtime.state != WorkerState.CRASHLOOPED:
                    self.start(name)
