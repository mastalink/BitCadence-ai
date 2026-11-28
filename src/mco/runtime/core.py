"""The runtime process and its lifecycle (DECISIONS.md section 4).

Exit codes are the contract with the OS bootstrap, which restarts on failure only:

* ``0``  a stop somebody asked for (``runtime stop``, the control API, an interrupt or terminate signal);
* ``1``  anything else - the runtime crashed and should be restarted;
* ``3``  another supervisor already owns this user.

Startup order is the single-owner fence: take the supervisor lock (and reap), bind the control port, and only then
start children.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import signal
import socket
import threading
import time
from typing import Any, Callable

import psutil
import uvicorn

from mco import __version__
from mco.agentd.config import FleetConfigManager
from mco.agentd.control import CONTROL_HOST
from mco.agentd.logs import LogAggregator
from mco.agentd.supervisor import Supervisor
from mco.agentd.tokens import AgentdTokenStore
from mco.killswitch import stop_active

from .config import RuntimeConfig
from .control import create_runtime_app, default_control_port
from .lkg import LkgStore
from .paths import HEARTBEAT_SECONDS, RuntimePaths, atomic_write
from .plugins.runner import PluginRunner
from .supervision import RuntimeSupervisor, ServiceProcess, default_services


EXIT_STOPPED = 0
EXIT_CRASH = 1
EXIT_ALREADY_RUNNING = 3
TICK_SECONDS = 1.0
# A single bad tick is logged and retried. This many in a row means the runtime is not doing its job: crash, and let
# the bootstrap start a clean one.
MAX_FAILED_TICKS = 30


def build_adapter() -> Any:
    if os.name == "nt":
        from mco.agentd.platform.windows import WindowsAdapter

        return WindowsAdapter()
    try:
        from mco.agentd.platform.posix import PosixAdapter  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise RuntimeError("the runtime is not supported on this platform yet") from exc
    return PosixAdapter()


def _is_already_running(exc: Exception) -> bool:
    return type(exc).__name__ == "SupervisorAlreadyRunning"


class Runtime:
    """Wires the collaborators together. Everything is injectable for tests."""

    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        adapter: Any = None,
        services: list[ServiceProcess] | None = None,
        control_port: int | None = None,
        token_store: AgentdTokenStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_interval: float = HEARTBEAT_SECONDS,
        tick_interval: float = TICK_SECONDS,
        serve_control: bool = True,
        say: Callable[[str], None] = print,
    ) -> None:
        self.paths = paths or RuntimePaths()
        self.clock = clock
        self.heartbeat_interval = heartbeat_interval
        self.tick_interval = tick_interval
        self.serve_control = serve_control
        self.control_port = control_port
        self.say = say
        self.adapter = adapter if adapter is not None else build_adapter()
        pidfile = self.paths.runtime / "children.pids"
        if hasattr(self.adapter, "pidfile"):
            self.adapter.pidfile = pidfile
        self.logs = LogAggregator(self.paths.home / "logs")
        options: dict[str, Any] = {
            "clock": clock,
            "logs": self.logs,
            "pidfile": pidfile,
            "state_file": self.paths.runtime / "supervisor.state.json",
        }
        if "lock_dir" in inspect.signature(Supervisor.__init__).parameters:
            options["lock_dir"] = self.paths.home / "locks"  # shared with any other owner of these workers
        if "foreign_owner" in inspect.signature(Supervisor.__init__).parameters:
            options["foreign_owner"] = self._foreign_owner  # never a second copy of a worker run by something else
        self._stop = threading.Event()
        self.supervisor = RuntimeSupervisor(
            self.adapter,
            stopping=self._stop,
            services=default_services() if services is None else services,
            **options,
        )
        self.manager = FleetConfigManager(self.paths.fleet)
        self.config = RuntimeConfig(
            self.manager, self.supervisor, LkgStore(self.paths.lkg), clock=clock
        )
        self.token_store = token_store or AgentdTokenStore(
            self.paths.home / "agentd.token", self.paths.home / "agentd.logs.token"
        )
        self.started_at = time.time()
        self.stop_reason: str | None = None
        self._state_lock = threading.RLock()
        self._next_heartbeat = 0.0
        self._last_reported: tuple[object, ...] | None = None
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self.plugins: PluginRunner | None = None
        self._process_created = psutil.Process().create_time()

    def _foreign_owner(self, config: Any) -> int | None:
        if config.role == "runtime":
            return None
        ownership = importlib.import_module("mco.agentd.ownership")
        handles = [item.process for item in self.supervisor.workers.values() if item.process is not None]
        return ownership.find_foreign_waker(config, own_pids=ownership.owned_pids(handles))

    # -- state --------------------------------------------------------------

    @property
    def maintenance(self) -> bool:
        return self.supervisor.held

    @property
    def state(self) -> str:
        if self.maintenance:
            return "maintenance"
        return "quiesced" if stop_active() else "running"

    def document(self, state: str | None = None, *, include_worker_errors: bool = False) -> dict[str, object]:
        workers = self.supervisor.status()
        assert isinstance(workers, list)
        return {
            "schema": "bitcadence.runtime/1",
            "state": state or self.state,
            "pid": os.getpid(),
            "process_created": self._process_created,
            "version": __version__,
            "started_at": self.started_at,
            "timestamp": time.time(),
            "stop_active": stop_active(),
            "maintenance": self.maintenance,
            "control_port": self.control_port,
            "config_generation": self.config.generation,
            "config_error": self.config.last_error,
            "children": {
                str(item["name"]): {
                    "state": item["state"], "pid": item["pid"],
                    **({"last_error": item["last_error"]} if include_worker_errors else {}),
                } for item in workers
            },
        }

    def write_heartbeat(self, state: str | None = None) -> None:
        with self._state_lock:
            document = self.document(state)
            try:
                atomic_write(self.paths.heartbeat, (json.dumps(document, indent=1) + "\n").encode("utf-8"))
            except OSError as exc:
                # A full disk must not take down the work. The heartbeat goes stale and the watchdog sees it.
                self.note(f"could not write the heartbeat: {exc}")
            self._next_heartbeat = self.clock() + self.heartbeat_interval

    def note(self, message: str) -> None:
        self.logs.write("runtime", "runtime", message)

    def set_maintenance(self, enabled: bool) -> None:
        with self._state_lock:
            if enabled:
                atomic_write(self.paths.maintenance, b"maintenance\n")
                self.supervisor.hold()
                if self.plugins is not None:
                    self.plugins.tick()
            else:
                self.paths.maintenance.unlink(missing_ok=True)
                self.supervisor.release()
            self.note(f"maintenance {'on' if enabled else 'off'}")
            self.write_heartbeat()

    def request_stop(self, reason: str) -> None:
        """A deliberate stop: the only way to exit 0."""
        self.stop_reason = self.stop_reason or reason
        self._stop.set()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Lock, bind, then children. Raises ``SupervisorAlreadyRunning`` for a second owner."""
        self.supervisor.startup()
        # From here on this process is the owner, and may write the runtime's files.
        if self.control_port is None:
            self.control_port = default_control_port()
        with self._state_lock:
            if self.paths.maintenance.exists():
                self.supervisor.held = True
            if self.serve_control:
                self._start_control()
            self.plugins = PluginRunner(
                self.paths.home, self.adapter, blocked=lambda: self.maintenance or self._stop.is_set(),
                say=self.note, clock=self.clock,
                control_credentials=(self.token_store.control_token(), self.token_store.logs_token())
                if self.serve_control else (),
            )
            self.config.load_initial()
            self.write_heartbeat()
        self.note(f"runtime started, state {self.state}")

    def _start_control(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name != "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((CONTROL_HOST, self.control_port or 0))
        except OSError:
            listener.close()
            raise
        self.control_port = listener.getsockname()[1]
        self.token_store.control_token()
        self.token_store.logs_token()
        # log_config=None: a windowless process has no stderr for uvicorn's default handlers.
        self._server = uvicorn.Server(
            uvicorn.Config(create_runtime_app(self), log_config=None, access_log=False, lifespan="off")
        )
        # Signals belong to the runtime, which turns them into a deliberate stop.
        self._server.install_signal_handlers = lambda: None  # type: ignore[attr-defined,method-assign]
        self._server_thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [listener]}, name="runtime-control", daemon=True
        )
        self._server_thread.start()

    def step(self) -> None:
        """One supervision pass."""
        if self._server_thread is not None and not self._server_thread.is_alive():
            raise ControlServerDied("the control API stopped unexpectedly")
        with self._state_lock:
            if self._stop.is_set():
                return
            self.config.poll()
            self.supervisor.tick()
            if self.plugins is not None:
                self.plugins.tick()
        reported = (self.state, self.config.generation, self.config.last_error)
        if reported != self._last_reported or self.clock() >= self._next_heartbeat:
            self._last_reported = reported
            self.write_heartbeat()

    def shutdown(self) -> None:
        """Close the control API to mutations, then stop every child and release ownership."""
        self._stop.set()
        # Wait for any admitted mutation to leave before tearing down the supervisor.
        # Release the state lock before waiting on children so pending requests can return 409.
        with self._state_lock:
            if self._server is not None:
                self._server.should_exit = True
            plugins, self.plugins = self.plugins, None
        try:
            if plugins is not None:
                plugins.close()
        finally:
            self.supervisor.shutdown()
            if self._server_thread is not None:
                self._server_thread.join(timeout=10)

    def run(self, *, handle_signals: bool = True) -> int:
        restore: list[tuple[int, Any]] = []
        started = False
        try:
            if handle_signals and threading.current_thread() is threading.main_thread():
                for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                    number = getattr(signal, name, None)
                    if number is not None:
                        restore.append((number, signal.signal(
                            number, lambda *_, label=name: self.request_stop(label))))
            self.start()
            started = True
            failed = 0
            while not self._stop.is_set():
                try:
                    self.step()
                    failed = 0
                except ControlServerDied:
                    raise
                except Exception as exc:
                    failed += 1
                    self.note(f"tick failed ({failed}): {type(exc).__name__}: {exc}")
                    if failed >= MAX_FAILED_TICKS:
                        raise
                self._stop.wait(self.tick_interval)
        except Exception as exc:
            if not started and _is_already_running(exc):
                self.say(str(exc))
                return EXIT_ALREADY_RUNNING
            self.say(f"runtime crashed: {type(exc).__name__}: {exc}")
            self._finish("crashed", f"crashed: {type(exc).__name__}: {exc}", owner=self.supervisor._started)
            return EXIT_CRASH
        else:
            self._finish("stopped", f"stopped on request ({self.stop_reason})", owner=True)
            return EXIT_STOPPED
        finally:
            for number, previous in restore:
                signal.signal(number, previous)
            close_logs = getattr(self.logs, "close", None)
            if close_logs is not None:
                close_logs()

    def _finish(self, state: str, message: str, *, owner: bool) -> None:
        if not owner:
            return  # never overwrite another owner's heartbeat
        try:
            self.note(message)
            self.shutdown()
        except Exception as exc:
            # The Job Object (or the process exit) still takes the children; the exit code must not change.
            self.note(f"shutdown was incomplete: {type(exc).__name__}: {exc}")
        self.write_heartbeat(state)


class ControlServerDied(RuntimeError):
    """The control API thread ended while the runtime was running."""
