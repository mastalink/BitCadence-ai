"""Register one interactive Windows task and launch the console runtime invisibly."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from mco.agentd.tokens import current_user_sid

from .paths import RuntimePaths, atomic_write


NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DESCRIPTION = "BitCadence managed runtime bootstrap for"
RESTART_COUNT = 3
RESTART_INTERVAL = 60
STABILITY_WINDOW_SECONDS = 10 * 60
POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$service = New-Object -ComObject Schedule.Service
$service.Connect()
$root = $service.GetFolder('\')
$name = $env:BITCADENCE_BOOTSTRAP_TASK
if ($env:BITCADENCE_BOOTSTRAP_XML) {
    $xml = [IO.File]::ReadAllText($env:BITCADENCE_BOOTSTRAP_XML)
    # TASK_CREATE (2) fails on a name collision. TASK_LOGON_INTERACTIVE_TOKEN is 3.
    $task = $root.RegisterTask($name, $xml, 2, $env:BITCADENCE_BOOTSTRAP_SID, $null, 3, $null)
    [Console]::Write($task.Xml)
} else {
    try { $task = $root.GetTask($name) }
    catch [System.IO.FileNotFoundException] { exit 0 }
    if ($task.Definition.RegistrationInfo.Description -cne $env:BITCADENCE_BOOTSTRAP_DESCRIPTION) {
        throw 'Refusing to remove a task without the BitCadence bootstrap ownership marker.'
    }
    if ($task.State -eq 4) { $task.Stop(0) }
    $root.DeleteTask($name, 0)
}
"""


def task_name(sid: str) -> str:
    return f"BitCadence Runtime {sid}"


def task_xml(name: str, sid: str, pythonw: Path, argv: list[str], cwd: Path) -> str:
    task = ET.Element("Task", xmlns=NAMESPACE, version="1.2")
    registration = ET.SubElement(task, "RegistrationInfo")
    ET.SubElement(registration, "Description").text = f"{DESCRIPTION} {sid}"
    ET.SubElement(registration, "URI").text = "\\" + name
    logon = ET.SubElement(ET.SubElement(task, "Triggers"), "LogonTrigger")
    ET.SubElement(logon, "UserId").text = sid
    principal = ET.SubElement(ET.SubElement(task, "Principals"), "Principal", id="CurrentUser")
    ET.SubElement(principal, "UserId").text = sid
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    ET.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, "StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, "ExecutionTimeLimit").text = "PT0S"
    restart = ET.SubElement(settings, "RestartOnFailure")
    ET.SubElement(restart, "Interval").text = f"PT{RESTART_INTERVAL // 60}M"
    ET.SubElement(restart, "Count").text = str(RESTART_COUNT)
    action = ET.SubElement(ET.SubElement(task, "Actions", Context="CurrentUser"), "Exec")
    ET.SubElement(action, "Command").text = str(pythonw)
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(argv)
    ET.SubElement(action, "WorkingDirectory").text = str(cwd)
    return ET.tostring(task, encoding="unicode", xml_declaration=False)


def _scheduler(name: str, sid: str, xml: Path | None = None) -> str:
    if os.name != "nt":
        raise RuntimeError("Runtime bootstrap registration is available only on Windows.")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL],
        env=dict(os.environ, BITCADENCE_BOOTSTRAP_TASK=name, BITCADENCE_BOOTSTRAP_SID=sid,
                 BITCADENCE_BOOTSTRAP_XML=str(xml) if xml is not None else "",
                 BITCADENCE_BOOTSTRAP_DESCRIPTION=f"{DESCRIPTION} {sid}"),
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Task Scheduler operation failed.")
    return result.stdout


def register_task(name: str, sid: str, xml: str) -> str:
    with tempfile.TemporaryDirectory(prefix="bitcadence-bootstrap-") as directory:
        path = Path(directory) / "task.xml"
        path.write_text(xml, encoding="utf-8")
        return _scheduler(name, sid, path)


def unregister_task(name: str, sid: str) -> None:
    _scheduler(name, sid)


def install_bootstrap(paths: RuntimePaths, export: Path | None = None) -> Path:
    if os.name != "nt":
        raise RuntimeError("Runtime bootstrap registration is available only on Windows.")
    sid = current_user_sid()
    name = task_name(sid)
    python = Path(sys.executable).with_name("python.exe")
    pythonw = python.with_name("pythonw.exe")
    if not python.is_file() or not pythonw.is_file():
        raise RuntimeError("Install using a Python environment with python.exe and pythonw.exe.")
    # Pin this package and interpreter so a source checkout works at the next sign-in too.
    source = Path(__file__).resolve().parents[2]
    code = (
        f"import sys; sys.path.insert(0, {str(source)!r}); "
        "from mco.runtime.bootstrap import run_scheduled; "
        f"raise SystemExit(run_scheduled({str(python)!r}, {str(source)!r}))"
    )
    paths.home.mkdir(parents=True, exist_ok=True)
    export = export if export is not None else paths.runtime / "bootstrap.xml"
    export.parent.mkdir(parents=True, exist_ok=True)
    definition = register_task(name, sid, task_xml(name, sid, pythonw, ["-c", code], paths.home))
    try:
        # The scheduler's XML declares UTF-16. Preserve it in the exported file.
        atomic_write(export, definition.encode("utf-16"))
    except OSError:
        unregister_task(name, sid)
        raise
    return export


def uninstall_bootstrap() -> None:
    if os.name != "nt":
        raise RuntimeError("Runtime bootstrap registration is available only on Windows.")
    sid = current_user_sid()
    unregister_task(task_name(sid), sid)


def launch_runtime(argv: list[str], cwd: Path, runtime_dir: Path, *, source: Path | None = None) -> int:
    from mco.agentd.platform.windows import WindowsAdapter

    from .core import EXIT_ALREADY_RUNNING, EXIT_CRASH, EXIT_STOPPED

    runtime_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if source is not None:
        env["PYTHONPATH"] = str(source)
    adapter = WindowsAdapter(pidfile=runtime_dir / "bootstrap.pids")
    with (
        (runtime_dir / "bootstrap.log").open("a", encoding="utf-8") as log,
        (runtime_dir / "bootstrap-child.log").open("ab") as child_log,
    ):
        # Task Scheduler counts launch failures. A successfully started action
        # returning a nonzero process result does not reliably trigger a retry.
        restarts = 0
        while True:
            child = adapter.spawn(argv, cwd, env, child_log.fileno(), child_log.fileno())
            started = time.monotonic()
            try:
                code = child.wait(timeout=None)
                elapsed = time.monotonic() - started
            finally:
                child.close()
                adapter.forget_pid(child.pid)
            if code in {EXIT_STOPPED, EXIT_ALREADY_RUNNING}:
                return EXIT_STOPPED
            if elapsed > STABILITY_WINDOW_SECONDS:
                restarts = 0
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if restarts == RESTART_COUNT:
                log.write(f"{timestamp} Runtime exited {code}; restart budget exhausted.\n")
                return EXIT_CRASH
            restarts += 1
            log.write(f"{timestamp} Runtime exited {code}; "
                      f"restart {restarts}/{RESTART_COUNT} in {RESTART_INTERVAL}s.\n")
            log.flush()
            time.sleep(RESTART_INTERVAL)


def run_scheduled(python: str, source: str) -> int:
    paths = RuntimePaths()
    return launch_runtime([python, "-m", "mco.cli", "runtime", "run"],
                          paths.home, paths.runtime, source=Path(source))
