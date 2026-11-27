"""Fleet configuration under the runtime: validate, apply, retain, roll back.

Built on :class:`mco.agentd.config.FleetConfigManager` (parsing, validation, atomic writes). It presents the same
surface as ``FleetWatcher`` (``poll``, ``replace``, ``last_error``, ``generation``) so the agentd control API works
unchanged, and adds the two things a deployed runtime needs: generations that survive a restart, and a rule that
nothing is reconciled while the global STOP is set.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
from pathlib import Path
import threading
import time
from typing import Callable

from mco.agentd.config import FleetConfigManager
from mco.fleet import WorkerConfig
from mco.killswitch import stop_active

from .lkg import LkgStore
from .paths import atomic_write
from .supervision import RESERVED_PREFIX, RuntimeSupervisor


POLL_SECONDS = 2.0
SETTLE_SECONDS = 0.25
ABSENT = "absent"


class RollbackUnavailable(RuntimeError):
    """There is no earlier generation to return to."""


def _digest(content: bytes | None) -> str:
    return ABSENT if content is None else hashlib.sha256(content).hexdigest()


def parse(manager: FleetConfigManager, content: bytes) -> dict[str, WorkerConfig]:
    """Validate one candidate. Raises on anything that must not become the running configuration."""
    workers = manager.validate(content.decode("utf-8"))
    reserved = sorted(name for name in workers if name.startswith(RESERVED_PREFIX))
    if reserved:
        raise ValueError(f"worker name(s) reserved for the runtime: {', '.join(reserved)}")
    return workers


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, content)


def rollback_files(
    manager: FleetConfigManager,
    store: LkgStore,
    apply: Callable[[bytes, dict[str, WorkerConfig]], None] | None = None,
) -> bytes:
    """Make the previous generation the current ``fleet.toml``; returns its content.

    The file being replaced is kept beside it as ``fleet.toml.rolled-back``. The newest generation is dropped, so a
    second rollback goes one further back instead of undoing the first.
    """
    generations = store.generations()
    if len(generations) < 2:
        raise RollbackUnavailable("there is no earlier configuration generation to roll back to")
    content = generations[-2].read_bytes()
    parsed = parse(manager, content)
    current = _read_optional(manager.path)
    newest = generations[-1]
    newest_content = newest.read_bytes()
    with ExitStack() as undo:
        if current is not None:
            backup = manager.path.with_name(manager.path.name + ".rolled-back")
            old_backup = _read_optional(backup)
            atomic_write(backup, current)
            undo.callback(_restore, backup, old_backup)
        atomic_write(manager.path, content)
        undo.callback(_restore, manager.path, current)
        newest.unlink()
        undo.callback(atomic_write, newest, newest_content)
        if apply is not None:
            apply(content, parsed)
        undo.pop_all()
    return content


class RuntimeConfig:
    def __init__(
        self,
        manager: FleetConfigManager,
        supervisor: RuntimeSupervisor,
        store: LkgStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float = POLL_SECONDS,
        settle: float = SETTLE_SECONDS,
    ) -> None:
        self.manager = manager
        self.supervisor = supervisor
        self.store = store
        self.clock = clock
        self.interval = interval
        self.settle = settle
        self.next_check = 0.0
        self.last_error: str | None = None
        self._fallback_generation: str | None = None
        self._running: str | None = None  # digest of the applied content; None until something is applied
        self._lock = threading.RLock()

    @property
    def generation(self) -> str:
        return self._running or ABSENT

    def _read(self) -> bytes | None:
        return _read_optional(self.manager.path)

    def _config_error(self, message: str) -> None:
        if self._fallback_generation is not None:
            message += f"; running last-known-good generation {self._fallback_generation}"
        self.last_error = message

    def load_initial(self) -> None:
        """Apply the file; if it cannot be used, apply the newest generation that can."""
        with self._lock:
            error = None
            try:
                content = self._read()
                if content is None or not content.strip():
                    error = f"{self.manager.path.name} is missing or empty"
                else:
                    self._apply(content, parse(self.manager, content))
                    return
            except Exception as exc:
                error = f"invalid fleet config: {exc}"
            for path in reversed(self.store.generations()):
                try:
                    content = path.read_bytes()
                    self._apply(content, parse(self.manager, content))
                except Exception:
                    continue
                self._fallback_generation = path.name
                self._config_error(str(error))
                return
            # Nothing usable anywhere: the runtime still owns the gateway and the scheduler.
            self.supervisor.reconcile({})
            fresh_install = not self.manager.path.exists()
            self.last_error = None if fresh_install else f"{error}; no last-known-good generation, no workers"

    def poll(self, force: bool = False) -> bool:
        with self._lock:
            now = self.clock()
            if not force and now < self.next_check:
                return False
            self.next_check = now + self.interval
            if stop_active():
                # Reconciling can halt a worker that STOP would then forbid restarting.
                return False
            try:
                content = self._read()
                if _digest(content) == self.generation and not force:
                    self._fallback_generation = None
                    self.last_error = None
                    return False
                if content is None or not content.strip():
                    if self._running is None and content is None:
                        return False
                    # What an in-place write looks like halfway through; never read as "remove every worker".
                    self._config_error(
                        f"{self.manager.path.name} is missing or empty; keeping the running configuration"
                    )
                    return False
                if self.settle > 0:
                    time.sleep(self.settle)
                    if self._read() != content:
                        self._config_error("fleet config is still changing; waiting for it to settle")
                        return False
                self._apply(content, parse(self.manager, content))
            except Exception as exc:
                self._config_error(f"invalid fleet config, keeping the running configuration: {exc}")
                return False
            return True

    def replace(self, content: str, expected_generation: str | None = None) -> dict[str, WorkerConfig]:
        """Control-API write: validate, write atomically, apply (held back while STOP is set)."""
        with self._lock:
            parsed = parse(self.manager, content.encode("utf-8"))
            if expected_generation is None:
                self.manager.write(content)
            else:
                self.manager.write(content, expected_generation)  # type: ignore[call-arg]
            if not stop_active():
                written = self._read()
                assert written is not None
                self._apply(written, parsed)
            return parsed

    def rollback(self) -> str:
        with self._lock:
            if stop_active():
                # The file is restored now; poll() applies it when STOP is cleared.
                return _digest(rollback_files(self.manager, self.store))

            def apply(content: bytes, workers: dict[str, WorkerConfig]) -> None:
                self._apply(content, workers, retain=False)

            return _digest(rollback_files(self.manager, self.store, apply))

    def _apply(self, content: bytes, workers: dict[str, WorkerConfig], *, retain: bool = True) -> None:
        self.supervisor.reconcile(workers)
        self._running = _digest(content)
        self._fallback_generation = None
        self.last_error = None
        if not retain:
            return
        try:
            self.store.save(content)
        except OSError as exc:
            # The configuration is running; only its copy for later could not be kept.
            self.last_error = f"running, but could not retain this generation: {exc}"
