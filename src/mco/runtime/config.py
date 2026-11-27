"""Fleet configuration under the runtime: validate, apply, retain, roll back.

Built on :class:`mco.agentd.config.FleetConfigManager` (parsing, validation, atomic writes). It presents the same
surface as ``FleetWatcher`` (``poll``, ``replace``, ``last_error``, ``generation``) so the agentd control API works
unchanged, and adds the two things a deployed runtime needs: generations that survive a restart, and a rule that
nothing is reconciled while the global STOP is set.
"""

from __future__ import annotations

import hashlib
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


def rollback_files(manager: FleetConfigManager, store: LkgStore) -> bytes:
    """Make the previous generation the current ``fleet.toml``; returns its content.

    The file being replaced is kept beside it as ``fleet.toml.rolled-back``. The newest generation is dropped, so a
    second rollback goes one further back instead of undoing the first.
    """
    generations = store.generations()
    if len(generations) < 2:
        raise RollbackUnavailable("there is no earlier configuration generation to roll back to")
    content = generations[-2].read_bytes()
    parse(manager, content)  # a generation that no longer validates is never written over a file
    try:
        current: bytes | None = manager.path.read_bytes()
    except FileNotFoundError:
        current = None
    if current is not None:
        atomic_write(manager.path.with_name(manager.path.name + ".rolled-back"), current)
    atomic_write(manager.path, content)
    generations[-1].unlink(missing_ok=True)
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
        self._running: str | None = None  # digest of the applied content; None until something is applied
        self._lock = threading.RLock()

    @property
    def generation(self) -> str:
        return self._running or ABSENT

    def _read(self) -> bytes | None:
        try:
            return self.manager.path.read_bytes()
        except FileNotFoundError:
            return None

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
                self.last_error = f"{error}; running last-known-good generation {path.name}"
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
                    self.last_error = None
                    return False
                if content is None or not content.strip():
                    if self._running is None and content is None:
                        return False
                    # What an in-place write looks like halfway through; never read as "remove every worker".
                    self.last_error = (
                        f"{self.manager.path.name} is missing or empty; keeping the running configuration"
                    )
                    return False
                if self.settle > 0:
                    time.sleep(self.settle)
                    if self._read() != content:
                        self.last_error = "fleet config is still changing; waiting for it to settle"
                        return False
                self._apply(content, parse(self.manager, content))
            except Exception as exc:
                self.last_error = f"invalid fleet config, keeping the running configuration: {exc}"
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
            content = rollback_files(self.manager, self.store)
            if stop_active():
                # The file is restored now; poll() applies it when STOP is cleared.
                return _digest(content)
            self._apply(content, parse(self.manager, content))
            return self.generation

    def _apply(self, content: bytes, workers: dict[str, WorkerConfig]) -> None:
        self.supervisor.reconcile(workers)
        self._running = _digest(content)
        self.last_error = None
        self.store.save(content)
