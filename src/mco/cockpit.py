"""The cockpit: every long-running BitCadence process, and every one-off job, in one herdr session.

Why this exists. On Windows each background piece (a worker's waker, a render, a review, a watcher) used to be
its own Scheduled Task, and anything that started ``python.exe`` or ``cmd.exe`` flashed a console window across
whatever the operator was doing. herdr (https://github.com/herdrdev/herdr, Apache-2.0, a Rust terminal
multiplexer that recognises coding agents) gives one persistent, headless session instead: each process lives in
a labelled pane you can attach to, read, and wait on, and nothing ever opens a window of its own.

Three things live here:

* :class:`Herdr` - a thin, testable wrapper over the ``herdr`` CLI (it returns JSON; we never parse the TUI).
* :func:`run` - **the** way to start a one-off job (a render, a review, a script): a labelled pane in the
  ``jobs`` workspace. Callers never spawn consoles themselves.
* :func:`up` / :func:`status` - bring the declared panes up from ``~/.mco/cockpit.toml`` plus every
  ``fleet.toml`` worker whose ``mode = "pane"``. Idempotent: a pane whose label already exists is left alone.

``cockpit.toml``::

    session = "bitcadence"          # optional; herdr named session
    herdr = "/path/to/herdr"        # optional; else BITCADENCE_HERDR, else `herdr` on PATH

    [[pane]]
    workspace = "services"          # created if missing
    label = "watcher"               # unique pane label; the idempotency key
    cwd = "~/projects/example-project"
    command = "python watcher.py --watch"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

try:  # pragma: no cover - exercised by interpreter version
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

COCKPIT_CONFIG_PATH = Path.home() / ".mco" / "cockpit.toml"
DEFAULT_SESSION = "bitcadence"
JOBS_WORKSPACE = "jobs"
AGENTS_WORKSPACE = "agents"
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
ALLOWED_PANE_FIELDS = {"workspace", "label", "cwd", "command"}


_UNSET: Any = object()   # Herdr(exe=...) not given -> discover; None -> resolved as missing


class HerdrError(RuntimeError):
    """herdr is missing, its server will not start, or a command failed."""


class CockpitConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PaneSpec:
    workspace: str
    label: str
    command: str
    cwd: Optional[str] = None


@dataclass
class CockpitConfig:
    session: str = DEFAULT_SESSION
    panes: list[PaneSpec] = field(default_factory=list)
    herdr: Optional[str] = None     # explicit herdr executable (deployment config, never a code default)


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _no_window() -> dict[str, Any]:
    return {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}


def find_herdr(configured: Optional[str] = None) -> Optional[str]:
    """``BITCADENCE_HERDR``, then ``herdr`` in cockpit.toml, then ``herdr`` on PATH. No machine-specific defaults:
    where herdr lives is deployment configuration, not product code.

    An explicitly pinned path that does not exist returns ``None`` on purpose (no fallback to PATH): running a
    different herdr binary than the one the operator pinned is worse than a clear "not installed" error."""
    for candidate in (os.environ.get("BITCADENCE_HERDR"), configured):
        if candidate:
            path = os.path.expanduser(candidate)
            return path if os.path.exists(path) else None
    return shutil.which("herdr")


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=120, **_no_window())


class Herdr:
    """One herdr named session, driven through its JSON CLI."""

    def __init__(self, session: str = DEFAULT_SESSION, exe: Any = _UNSET,
                 runner: Optional[Runner] = None,
                 spawn: Optional[Callable[[Sequence[str]], Any]] = None):
        self.session = session
        # exe=None means "resolved, and not installed" (e.g. a pinned path that is missing): never re-resolve it
        # to whatever herdr happens to be on PATH. Only an omitted exe triggers discovery.
        self.exe = find_herdr() if exe is _UNSET else exe
        self._runner = runner or _default_runner
        self._spawn = spawn or self._spawn_hidden

    # -- plumbing --------------------------------------------------------------------------------------------
    def _argv(self, *args: str) -> list[str]:
        if not self.exe:
            raise HerdrError("herdr is not installed (or the path pinned in BITCADENCE_HERDR / cockpit.toml "
                             "`herdr` does not exist). Get it from https://github.com/herdrdev/herdr/releases "
                             "and put herdr.exe on PATH (or fix the pinned path).")
        return [self.exe, "--session", self.session, *args]

    def call(self, *args: str) -> Any:
        """Run one herdr command. Returns parsed JSON when the output is JSON, else the raw text."""
        proc = self._runner(self._argv(*args))
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise HerdrError(f"herdr {' '.join(args[:2])} failed (exit {proc.returncode}): {detail[:400]}")
        out = (proc.stdout or "").strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return out

    @staticmethod
    def _spawn_hidden(argv: Sequence[str]) -> Any:
        flags = (_CREATE_NO_WINDOW | _DETACHED_PROCESS) if os.name == "nt" else 0
        return subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)

    # -- server ----------------------------------------------------------------------------------------------
    def running(self) -> bool:
        """Is the session's server up? Raises HerdrError when herdr itself is missing (not "not running")."""
        self._argv()
        try:
            out = self.call("status", "server")
        except HerdrError:
            return False
        return "status: running" in str(out)

    def ensure_server(self, wait_s: float = 15.0) -> None:
        """Start the headless server (no window) if it is not already up."""
        if self.running():
            return
        self._spawn(self._argv("server"))
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.running():
                return
            time.sleep(0.5)
        raise HerdrError(f"herdr server for session {self.session!r} did not start within {wait_s:.0f}s")

    # -- topology --------------------------------------------------------------------------------------------
    def workspaces(self) -> list[dict]:
        return ((self.call("workspace", "list") or {}).get("result") or {}).get("workspaces") or []

    def panes(self, workspace_id: Optional[str] = None) -> list[dict]:
        args = ["pane", "list"] + (["--workspace", workspace_id] if workspace_id else [])
        return ((self.call(*args) or {}).get("result") or {}).get("panes") or []

    def find_pane(self, label: str) -> Optional[dict]:
        return next((p for p in self.panes() if p.get("label") == label), None)

    def ensure_workspace(self, label: str, cwd: Optional[str] = None) -> tuple[str, Optional[str]]:
        """(workspace_id, root pane id if we just created it and it is still free)."""
        for ws in self.workspaces():
            if ws.get("label") == label:
                return ws["workspace_id"], None
        args = ["workspace", "create", "--label", label, "--no-focus"] + (["--cwd", cwd] if cwd else [])
        res = (self.call(*args) or {}).get("result") or {}
        return res["workspace"]["workspace_id"], res["root_pane"]["pane_id"]

    def new_pane(self, workspace: str, label: str, cwd: Optional[str] = None) -> str:
        """A fresh, labelled shell pane in ``workspace``."""
        ws_id, free_root = self.ensure_workspace(workspace, cwd)
        if free_root:
            pane_id = free_root
        else:
            existing = self.panes(ws_id)
            if not existing:
                raise HerdrError(f"workspace {workspace!r} ({ws_id}) has no panes to split; "
                                 "close it in herdr and run again")
            # alternate split direction so a busy workspace stays a readable grid, not slivers
            direction = "down" if len(existing) % 2 else "right"
            args = ["pane", "split", existing[-1]["pane_id"], "--direction", direction, "--no-focus"]
            if cwd:
                args += ["--cwd", cwd]
            pane_id = ((self.call(*args) or {}).get("result") or {})["pane"]["pane_id"]
        self.call("pane", "rename", pane_id, label)
        return pane_id

    def shell_pid(self, pane_id: str) -> Optional[int]:
        res = (self.call("pane", "process-info", "--pane", pane_id) or {}).get("result") or {}
        pid = (res.get("process_info") or {}).get("shell_pid")
        return int(pid) if pid else None

    def busy(self, pane_id: str) -> bool:
        """True when the pane's shell is running something.

        herdr restores panes after a server restart or reboot as *fresh shells* (snapshot restore keeps layout,
        labels and cwd, not processes), so a live label does not prove the command is running. On Windows
        ``process-info`` only ever reports the shell (no foreground process groups), so ask the OS whether the
        shell has a child process instead.
        """
        pid = self.shell_pid(pane_id)
        return bool(pid) and _has_child_process(pid)

    # -- work ------------------------------------------------------------------------------------------------
    def run_in(self, pane_id: str, command: str) -> None:
        self.call("pane", "run", pane_id, command)

    def read(self, pane_id: str, lines: int = 80) -> str:
        out = self.call("pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", str(lines))
        return out if isinstance(out, str) else json.dumps(out)

    def wait_output(self, pane_id: str, match: str, timeout_ms: int = 120_000) -> bool:
        try:
            self.call("pane", "wait-output", pane_id, "--match", match, "--timeout", str(timeout_ms))
            return True
        except HerdrError:
            return False


def _has_child_process(pid: int) -> bool:
    """Does process ``pid`` have at least one child? (Toolhelp32 on Windows, /proc or pgrep elsewhere.)"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        # A private WinDLL with explicit argtypes: on 64-bit Windows an untyped HANDLE argument is marshalled as a
        # 32-bit int (OverflowError / truncated handle), and setting them on the shared ctypes.windll would leak
        # into other modules.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lp_entry = ctypes.POINTER(PROCESSENTRY32W)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, lp_entry]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, lp_entry]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
        if snap in (None, 0, wintypes.HANDLE(-1).value):
            return False
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                # conhost.exe is the shell's own console host, not a command it is running
                if entry.th32ParentProcessID == pid and entry.szExeFile.lower() != "conhost.exe":
                    return True
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
            return False
        finally:
            k32.CloseHandle(snap)
    proc = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return proc.returncode == 0 and bool(proc.stdout.strip())


# -- the one function every job goes through ---------------------------------------------------------------
def command_line(argv: Sequence[str]) -> str:
    """Quote an argv for the pane's shell (PowerShell on Windows, POSIX sh elsewhere).

    The line is typed into a PTY, so a CR/LF inside an argument would end the line there and run the rest as a
    second command: refused, like an empty argv."""
    if not argv:
        raise HerdrError("empty command")
    if any("\r" in str(a) or "\n" in str(a) for a in argv):
        raise HerdrError("a command argument contains a line break; refusing to type it into a shell")
    if os.name == "nt":
        quoted = ["'" + str(a).replace("'", "''") + "'" for a in argv]
        return "& " + " ".join(quoted)
    import shlex
    return " ".join(shlex.quote(str(a)) for a in argv)


def run(label: str, command: str | Sequence[str], workspace: str = JOBS_WORKSPACE, cwd: Optional[str] = None,
        herdr: Optional[Herdr] = None, reuse: bool = False) -> str:
    """Start ``command`` in its own labelled pane and return the pane id. Never opens a window.

    ``reuse=False`` (default) refuses a label that is already live, so two renders can never share a pane by
    accident; ``reuse=True`` sends the command to the existing pane instead (for re-runs at its prompt).
    """
    h = herdr or herdr_for(load_cockpit())
    h.ensure_server()
    text = command if isinstance(command, str) else command_line(command)
    if not text.strip() or "\r" in text or "\n" in text:
        raise HerdrError("a pane command must be one non-empty line")
    existing = h.find_pane(label)
    if existing:
        if not reuse:
            raise HerdrError(f"a pane labelled {label!r} already exists ({existing['pane_id']}); "
                             "pick a new label or pass reuse=True")
        pane_id = existing["pane_id"]
    else:
        pane_id = h.new_pane(workspace, label, cwd)
    h.run_in(pane_id, text)
    return pane_id


# -- declared panes ----------------------------------------------------------------------------------------
def parse_cockpit_data(data: dict[str, Any]) -> CockpitConfig:
    session = data.get("session", DEFAULT_SESSION)
    if not isinstance(session, str) or not session:
        raise CockpitConfigError("session must be a non-empty string")
    raw = data.get("pane", [])
    if not isinstance(raw, list):
        raise CockpitConfigError("[[pane]] must be an array of tables")
    panes, seen = [], set()
    for i, p in enumerate(raw):
        if not isinstance(p, dict):
            raise CockpitConfigError(f"pane #{i + 1} must be a table")
        unknown = set(p) - ALLOWED_PANE_FIELDS
        if unknown:
            raise CockpitConfigError(f"pane #{i + 1} has unsupported field(s): {', '.join(sorted(unknown))}")
        for key in ("workspace", "label", "command"):
            if not isinstance(p.get(key), str) or not p[key].strip():
                raise CockpitConfigError(f"pane #{i + 1} needs a non-empty {key}")
        if "\r" in p["command"] or "\n" in p["command"]:
            raise CockpitConfigError(f"pane #{i + 1} command must be a single line")
        if p["label"] in seen:
            raise CockpitConfigError(f"duplicate pane label {p['label']!r}")
        seen.add(p["label"])
        panes.append(PaneSpec(workspace=p["workspace"], label=p["label"], command=p["command"], cwd=p.get("cwd")))
    exe = data.get("herdr")
    if exe is not None and (not isinstance(exe, str) or not exe.strip()):
        raise CockpitConfigError("herdr must be a non-empty path string")
    return CockpitConfig(session=session, panes=panes, herdr=exe)


def herdr_for(cfg: CockpitConfig) -> Herdr:
    """The Herdr client for a loaded config (its session and, if set, its herdr executable)."""
    return Herdr(session=cfg.session, exe=find_herdr(cfg.herdr))


def load_cockpit(path: Path = COCKPIT_CONFIG_PATH) -> CockpitConfig:
    if not path.exists():
        return CockpitConfig()
    with path.open("rb") as handle:
        try:
            return parse_cockpit_data(tomllib.load(handle))
        except tomllib.TOMLDecodeError as exc:
            raise CockpitConfigError(f"Invalid TOML in {path}: {exc}") from exc


def fleet_panes(fleet_path: Optional[Path] = None) -> list[PaneSpec]:
    """Every fleet.toml worker with ``mode = "pane"``: its waker runs in the ``agents`` workspace."""
    from mco import fleet, service

    try:
        workers = fleet.load_fleet(fleet_path) if fleet_path else fleet.load_fleet()
    except fleet.FleetConfigMissing:
        return []
    specs = []
    for w in workers.values():
        if w.mode != "pane":
            continue
        argv = service._wake_argv(w.role, w.exec_command or "", instance=w.instance, min_interval=w.min_interval)
        # a pane IS the console, so use python.exe (visible output) rather than the windowless pythonw.exe
        argv[0] = _console_python(argv[0])
        specs.append(PaneSpec(workspace=AGENTS_WORKSPACE, label=w.worker, command=command_line(argv)))
    return specs


def _console_python(exe: str) -> str:
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


def declared(path: Path = COCKPIT_CONFIG_PATH, fleet_path: Optional[Path] = None) -> CockpitConfig:
    cfg = load_cockpit(path)
    labels = {p.label for p in cfg.panes}
    for spec in fleet_panes(fleet_path):
        if spec.label in labels:
            raise CockpitConfigError(f"pane label {spec.label!r} is declared in both cockpit.toml and fleet.toml")
        cfg.panes.append(spec)
    return cfg


def up(path: Path = COCKPIT_CONFIG_PATH, fleet_path: Optional[Path] = None,
       herdr: Optional[Herdr] = None) -> list[str]:
    """Bring every declared pane up. Idempotent: a pane already running its command is left alone, a pane that
    came back from a restart/reboot as an idle shell (herdr restores layout, not processes) gets its command
    again, and a missing pane is created. Run this at sign-in."""
    cfg = declared(path, fleet_path)
    h = herdr or herdr_for(cfg)
    h.ensure_server()
    live = {p.get("label"): p for p in h.panes()}
    lines = []
    for spec in cfg.panes:
        pane = live.get(spec.label)
        if pane and h.busy(pane["pane_id"]):
            lines.append(f"{spec.label}: already running ({spec.workspace})")
            continue
        if pane:   # restored as an idle shell after a restart: run the command in it again
            h.run_in(pane["pane_id"], spec.command)
            lines.append(f"{spec.label}: restarted in its restored pane ({pane['pane_id']})")
            continue
        pane_id = h.new_pane(spec.workspace, spec.label, spec.cwd)
        h.run_in(pane_id, spec.command)
        lines.append(f"{spec.label}: started in {spec.workspace} ({pane_id})")
    return lines or ["nothing declared: add [[pane]] entries to ~/.mco/cockpit.toml or set a fleet worker mode=pane"]


def status(path: Path = COCKPIT_CONFIG_PATH, fleet_path: Optional[Path] = None,
           herdr: Optional[Herdr] = None) -> list[dict[str, object]]:
    cfg = declared(path, fleet_path)
    h = herdr or herdr_for(cfg)
    by_label = {p.get("label"): p for p in h.panes()} if h.running() else {}
    rows = []
    for spec in cfg.panes:
        live = by_label.get(spec.label)
        rows.append({"label": spec.label, "workspace": spec.workspace,
                     "running": bool(live) and h.busy(live["pane_id"]),
                     "pane": (live or {}).get("pane_id", ""), "agent": (live or {}).get("agent_status", "")})
    declared_labels = {s.label for s in cfg.panes}
    for label, live in by_label.items():
        if label and label not in declared_labels:   # one-off jobs started with run()
            rows.append({"label": label, "workspace": "(job)", "running": h.busy(live["pane_id"]),
                         "pane": live.get("pane_id", ""), "agent": live.get("agent_status", "")})
    return rows
