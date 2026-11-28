"""Runtime-owned plugin execution. The runtime serializes ticks and API calls."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
from subprocess import TimeoutExpired
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time
from typing import Callable
from uuid import uuid4

from pydantic import ValidationError

from mco.agentd.platform.base import PlatformAdapter, ProcessHandle
from mco.agentd.supervisor import (
    CRASH_LIMIT, CRASH_WINDOW_SECONDS, CRASHLOOP_COOLDOWN_SECONDS, STABLE_RESET_SECONDS, backoff_delay,
)
from mco.killswitch import _exists_fail_closed, stop_active

from .contract import Event, Status, Task
from .grants import GrantError, parameter_argv, read_owner, verify_grant
from .state import PluginState
from .trust import Candidate, LOCK_NAME, discover, inspect_plugin, read_lock, trust_changes

OUTPUT_LIMIT = 256 * 1024
# Credentials and configuration for clients must never be copied into children.
OS_ENV = {
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR", "HOME",
    "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL",
}
CREDENTIAL_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "ACCESS_KEY", "PRIVATE_KEY")


class PluginRefused(RuntimeError):
    """A command did not pass the runtime's admission checks."""


@dataclass
class Child:
    plugin: str
    command: str
    trust: str
    channel: str | None
    process: ProcessHandle
    output: TemporaryDirectory
    started: float
    timeout: int | None
    run: str | None
    offset: int = 0
    remainder: bytes = b""


@dataclass
class Restart:
    failures: deque[float] = field(default_factory=deque)
    exponent: int = 0
    next_start: float = 0.0


class PluginRunner:
    def __init__(
        self, home: Path, adapter: PlatformAdapter, *, blocked: Callable[[], bool],
        say: Callable[[str], None] = lambda _: None,
        wall: Callable[[], float] = time, clock: Callable[[], float] = monotonic,
        owner_key=None, owner_instance: str | None = None,
        control_credentials: tuple[str, ...] = (),
    ) -> None:
        self.home = home
        self.adapter = adapter
        self.blocked = blocked
        self.say = say
        self.wall = wall
        self.clock = clock
        self.owner_key = owner_key
        self.owner_instance = owner_instance
        self.control_credentials = control_credentials
        if owner_key is None and owner_instance is None:
            try:
                self.owner_instance, self.owner_key = read_owner(home / "plugin-owner.toml")
            except (OSError, ValueError, KeyError, TypeError):
                self.say("plugin owner configuration invalid; actions disabled")
        self.state = PluginState(home / "runtime" / "plugins.sqlite3")
        self.audience = self.state.audience
        self.audit_ok = self.state.audit_valid()
        if self.audit_ok:
            self.state.interrupted(self.wall())
        else:
            self.say("plugin audit chain invalid; actions disabled")
        self.candidates: dict[str, Candidate] = {}
        self.inventory: dict[str, dict] = {}
        self.status: dict[tuple[str, str], dict] = {}
        self.children: dict[tuple[str, str], Child] = {}
        self.next_checks: dict[tuple[str, str], float] = {}
        self.restarts: dict[tuple[str, str], Restart] = {}
        self.events: dict[str, deque] = {}
        self.closed = False

    def _refresh(self) -> None:
        try:
            approved = read_lock(self.home / LOCK_NAME)
        except (OSError, ValueError):
            approved = {}
            self.say("plugin trust lock invalid; plugins paused")
        inventory = {}
        candidates: dict[str, Candidate] = {}
        hashes: dict[Path, str] = {}
        for manifest in discover(self.home / "plugins"):
            try:
                candidate = inspect_plugin(manifest, hashes=hashes)
            except (OSError, ValueError, RuntimeError):
                # An unreadable executable or schema-invalid edit also fails closed.
                inventory.setdefault(manifest.parent.name, {"state": "review", "error": "invalid candidate"})
                continue
            plugin = candidate.manifest.id
            if plugin in inventory:
                inventory[plugin] = {"state": "review", "error": "duplicate plugin id"}
                candidates.pop(plugin, None)
                continue
            changes = trust_changes(candidate, approved.get(plugin))
            if changes:
                inventory[plugin] = {"state": "review", "changes": changes}
            elif candidate.manifest.identity.instance == self.owner_instance:
                inventory[plugin] = {"state": "review", "error": "plugin identity conflicts with owner"}
            else:
                candidates[plugin] = candidate
                inventory[plugin] = {
                    "state": "paused" if self._blocked(plugin) else "ready",
                    "trust": candidate.fingerprint,
                    "resources": candidate.manifest.resources,
                    "views": {name: view.model_dump(by_alias=True, exclude_none=True)
                              for name, view in candidate.manifest.views.items()},
                    "actions": {name: {
                        "dangerous": action.dangerous,
                        "parameters": [parameter.model_dump() for parameter in action.parameters],
                    } for name, action in candidate.manifest.actions.items()},
                }
        for plugin in self.candidates.keys() | candidates.keys():
            old = self.candidates.get(plugin)
            new = candidates.get(plugin)
            if old is None or new is None or old.fingerprint != new.fingerprint:
                self.events.pop(plugin, None)
                for collection in (self.status, self.restarts, self.next_checks):
                    for key in [key for key in collection if key[0] == plugin]:
                        del collection[key]
        self.inventory = inventory
        self.candidates = candidates

    def _blocked(self, plugin: str, channel: str | None = None) -> bool:
        pauses = self.home / "plugin-pauses"
        return (self.closed or self.blocked() or stop_active()
                or _exists_fail_closed(pauses / "plugins" / plugin)
                or (channel is not None and _exists_fail_closed(pauses / "channels" / channel)))

    def _spawn(self, candidate: Candidate, command: str, *, extra: list[str] | None = None,
               run: str | None = None, owner: str | None = None, expires_at: float | None = None) -> None:
        plugin = candidate.manifest.id
        spec = candidate.manifest.commands()[command]
        output = TemporaryDirectory(prefix="bitcadence-plugin-")
        output_path = Path(output.name) / "stdout"
        try:
            # A fresh hash check immediately before launch, including actions
            # admitted after the last tick and changes during grant verification.
            current = inspect_plugin(candidate.source)
            if (current.folder != candidate.folder or current.fingerprint != candidate.fingerprint or
                    trust_changes(current, read_lock(self.home / LOCK_NAME).get(plugin))):
                raise PluginRefused("plugin changed and requires review")
            env = {key: value for key, value in os.environ.items() if key.upper() in OS_ENV}
            env["AGENT_INSTANCE_ID"] = owner or candidate.manifest.identity.instance
            env["AGENT_ROLE"] = "owner" if owner else candidate.manifest.identity.role
            argv = [*current.argv[command], *(extra or [])]
            # Windows passes argv via one CreateProcess command line.
            if os.name == "nt":
                from subprocess import list2cmdline
                if len(list2cmdline(argv).encode("utf-16-le")) // 2 >= 32_767:
                    raise PluginRefused("command line exceeds platform limit")
            with output_path.open("wb") as stream, open(os.devnull, "wb") as discard:
                if self._blocked(plugin, spec.channel):
                    raise PluginRefused("plugin is stopped or paused")
                if expires_at is not None and self.wall() >= expires_at:
                    raise PluginRefused("owner grant expired before spawn")
                process = self.adapter.spawn(argv, candidate.folder, env, stream.fileno(), discard.fileno())
        except Exception:
            output.cleanup()
            raise
        self.children[plugin, command] = Child(
            plugin, command, candidate.fingerprint, spec.channel, process, output, self.clock(),
            spec.timeout_seconds if isinstance(spec, Task) else None, run,
        )

    def _safe_output(self, document: dict) -> bool:
        # Credentials available to the runtime are never allowed in published
        # documents. Plugin stdout/stderr is not logged; only validated data escapes.
        credentials = self.control_credentials + tuple(
            value for key, value in os.environ.items()
            if any(marker in key.upper() for marker in CREDENTIAL_MARKERS)
        )
        def values(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from values(item)
            elif isinstance(value, list):
                for item in value:
                    yield from values(item)
            else:
                yield str(value)

        return not any(secret and secret in value for value in values(document) for secret in credentials)

    def _event(self, child: Child, line: bytes) -> None:
        try:
            event = Event.model_validate_json(line)
        except ValidationError:
            return
        document = event.model_dump(exclude_none=True)
        if self._safe_output(document):
            self.events.setdefault(child.plugin, deque(maxlen=32)).append({"command": child.command, **document})

    def _read_output(self, child: Child, *, done: bool) -> tuple[bool, list[bytes]]:
        path = Path(child.output.name) / "stdout"
        with path.open("rb") as stream:
            stream.seek(child.offset)
            received = stream.read(max(0, OUTPUT_LIMIT - child.offset) + 1)
        child.offset += len(received)
        if child.offset > OUTPUT_LIMIT:
            return False, []
        lines = (child.remainder + received).split(b"\n")
        child.remainder = b"" if done else lines.pop()
        data = []
        for line in lines:
            if line.startswith(b"@@bcd "):
                self._event(child, line[6:])
            else:
                data.append(line)
        return True, data

    def _read_status(self, child: Child, lines: list[bytes]) -> None:
        try:
            status = Status.model_validate_json(b"\n".join(lines))
            document = status.model_dump(by_alias=True, exclude_none=True)
        except ValidationError:
            document = {}
        # Enforce the manifest's list of secret names. These entries carry only
        # optional expiry and presence; never accept a plugin's credential values.
        declared = self.candidates[child.plugin].manifest.required_secrets
        if document and document["secrets"].keys() <= set(declared) and self._safe_output(document):
            self.status[child.plugin, child.command] = document
            return
        self.status[child.plugin, child.command] = {"error": "invalid or sensitive status output"}

    def _finish(self, key: tuple[str, str], child: Child, outcome: str) -> None:
        # Leave the handle registered if kill or wait fails. A later tick may
        # retry, and there can be no overlap with a process that is still alive.
        try:
            if child.process.poll() is None:
                child.process.kill()
                child.process.wait(timeout=5)
            close = getattr(child.process, "close", None)
            if close is not None:
                close()
        except (OSError, TimeoutError, TimeoutExpired):
            return
        if child.run is not None:
            self.state.finish(child.run, outcome, self.wall())
        forget = getattr(self.adapter, "forget_pid", None)
        if forget is not None:
            forget(child.process.pid)
        del self.children[key]
        child.output.cleanup()

    def _failure(self, key: tuple[str, str], elapsed: float = 0.0) -> None:
        restart = self.restarts.setdefault(key, Restart())
        now = self.clock()
        if elapsed >= STABLE_RESET_SECONDS:
            restart.exponent = 0
        while restart.failures and now - restart.failures[0] >= CRASH_WINDOW_SECONDS:
            restart.failures.popleft()
        restart.failures.append(now)
        delay = CRASHLOOP_COOLDOWN_SECONDS if len(restart.failures) >= CRASH_LIMIT else backoff_delay(restart.exponent)
        restart.next_start = now + delay
        restart.exponent = min(restart.exponent + 1, CRASH_LIMIT + 1)

    def _poll(self) -> None:
        for key, child in list(self.children.items()):
            candidate = self.candidates.get(child.plugin)
            if (candidate is None or candidate.fingerprint != child.trust
                    or self._blocked(child.plugin, child.channel)):
                self._finish(key, child, "refused")
                continue
            code = child.process.poll()
            elapsed = self.clock() - child.started
            status_command = child.command in ("status", "health")
            # Status/health stdout is one JSON document at exit, optionally
            # interleaved with event lines. Other children stream events only.
            if status_command and code is None:
                within_limit = (Path(child.output.name) / "stdout").stat().st_size <= OUTPUT_LIMIT
                lines = []
            else:
                within_limit, lines = self._read_output(child, done=code is not None)
            if not within_limit:
                outcome = "output-limit"
            elif code is not None:
                outcome = "ok" if code == 0 else "failed"
            elif child.timeout is not None and elapsed >= child.timeout:
                outcome = "timed-out"
            else:
                continue
            if self._blocked(child.plugin, child.channel):
                self._finish(key, child, "refused")
                continue
            if status_command:
                if outcome == "ok":
                    self._read_status(child, lines)
                else:
                    self.status[key] = {"error": outcome}
            if child.command.startswith("process:"):
                self._failure(key, elapsed)
            self._finish(key, child, outcome)

    def tick(self) -> None:
        if self.closed:
            return
        self._refresh()
        self._poll()
        now = self.wall()
        for plugin, candidate in self.candidates.items():
            if self._blocked(plugin):
                continue
            manifest = candidate.manifest
            self.state.prune_schedules(plugin, set(manifest.schedules))
            for key, spec in manifest.commands().items():
                identity = (plugin, key)
                if key.startswith("action:") or self._blocked(plugin, spec.channel):
                    continue
                if key.startswith("schedule:"):
                    name = key.split(":", 1)[1]
                    if not self.state.schedule_due(plugin, name, manifest.schedules[name].interval_seconds, now):
                        continue
                if identity in self.children:
                    continue
                if key in ("status", "health"):
                    if self.clock() < self.next_checks.get(identity, 0):
                        continue
                    self.next_checks[identity] = self.clock() + spec.interval_seconds
                if key.startswith("process:"):
                    restart = self.restarts.get(identity)
                    if restart is not None and self.clock() < restart.next_start:
                        continue
                try:
                    self._spawn(candidate, key)
                except (OSError, ValueError, PluginRefused, RuntimeError):
                    self.say(f"plugin {plugin} command {key} failed admission or spawn")
                    if key.startswith("process:"):
                        self._failure(identity)
                    if key in ("status", "health"):
                        self.status[identity] = {"error": "spawn refused or failed"}

    def start_action(self, plugin: str, action: str, parameters: dict, grant: dict) -> str:
        if self.closed:
            raise PluginRefused("plugin runner is stopped")
        if not self.audit_ok:
            raise PluginRefused("plugin audit chain is invalid")
        self._refresh()
        candidate = self.candidates.get(plugin)
        key = f"action:{action}"
        if candidate is None or action not in candidate.manifest.actions:
            raise PluginRefused("trusted action not available")
        spec = candidate.manifest.actions[action]
        try:
            extra = parameter_argv(spec, parameters)
            claims = verify_grant(
                grant, key=self.owner_key, owner=self.owner_instance, audience=self.audience,
                candidate=candidate, action_id=action, parameters=parameters, now=self.wall(),
            )
        except GrantError as exc:
            raise PluginRefused(str(exc)) from None
        if (plugin, key) in self.children:
            raise PluginRefused("action already running")
        run = uuid4().hex
        if not self.state.admit(run, claims.nonce, plugin, action, claims.owner, self.wall()):
            raise PluginRefused("owner grant already used")
        try:
            # Check expiry again after durable admission. _spawn also checks all
            # switches, after I/O and as close to actual process creation as possible.
            self._spawn(candidate, key, extra=extra, run=run, owner=claims.owner,
                        expires_at=claims.expires_at)
        except PluginRefused:
            self.state.finish(run, "refused", self.wall())
            raise
        except (OSError, ValueError, RuntimeError):
            self.state.finish(run, "failed", self.wall())
            raise PluginRefused("action spawn failed") from None
        return run

    def action_result(self, run: str) -> dict | None:
        return self.state.action_result(run)

    def document(self) -> dict:
        result = {}
        for plugin, item in self.inventory.items():
            document = {
                **item,
                **{key: self.status[plugin, key] for key in ("status", "health") if (plugin, key) in self.status},
                "events": list(self.events.get(plugin, ())),
            }
            if self._safe_output({plugin: document}):
                result[plugin] = document
        return result

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            for key, child in list(self.children.items()):
                self._finish(key, child, "interrupted")
        finally:
            self.state.close()
