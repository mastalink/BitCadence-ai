from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET

import pytest

from mco.runtime import bootstrap
from mco.runtime.paths import RuntimePaths


def test_task_runs_at_this_users_sign_in_without_a_time_limit(tmp_path):
    sid = "S-1-5-21-1000"
    pythonw = tmp_path / "Python & tools" / "pythonw.exe"
    xml = bootstrap.task_xml("BitCadence Runtime TEST-123", sid, pythonw, ["-m", "example"], tmp_path)
    task = ET.fromstring(xml)

    def value(path):
        return task.findtext("/".join("{*}" + part for part in path.split("/")))

    assert value("Triggers/LogonTrigger/UserId") == sid
    assert value("Principals/Principal/UserId") == sid
    assert value("Principals/Principal/LogonType") == "InteractiveToken"
    assert value("Principals/Principal/RunLevel") == "LeastPrivilege"
    assert value("Settings/MultipleInstancesPolicy") == "IgnoreNew"
    assert value("Settings/ExecutionTimeLimit") == "PT0S"
    assert value("Settings/RestartOnFailure/Count") == "3"
    assert value("Settings/RestartOnFailure/Interval") == "PT1M"
    assert value("Actions/Exec/Command") == str(pythonw)
    assert value("Actions/Exec/Arguments") == "-m example"


@pytest.mark.skipif(os.name != "nt", reason="Windows task registration")
def test_install_exports_scheduler_definition_and_uninstall_uses_same_name(tmp_path, monkeypatch):
    paths = RuntimePaths(tmp_path / ".mco")
    sid = "S-1-5-21-1000"
    python = tmp_path / "python.exe"
    python.touch()
    python.with_name("pythonw.exe").touch()
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(bootstrap, "current_user_sid", lambda: sid)
    operations = []
    definition = '<?xml version="1.0" encoding="UTF-16"?><Task />'

    def register(name, principal, xml):
        operations.append(("register", name, principal))
        action = ET.fromstring(xml).find("{*}Actions/{*}Exec")
        assert Path(action.findtext("{*}Command")) == python.with_name("pythonw.exe")
        assert "run_scheduled" in action.findtext("{*}Arguments")
        return definition

    monkeypatch.setattr(bootstrap, "register_task", register)
    monkeypatch.setattr(bootstrap, "unregister_task",
                        lambda name, principal: operations.append(("unregister", name, principal)))
    export = bootstrap.install_bootstrap(paths)
    assert export == paths.runtime / "bootstrap.xml"
    assert export.read_text(encoding="utf-16") == definition
    bootstrap.uninstall_bootstrap()
    assert operations == [
        ("register", f"BitCadence Runtime {sid}", sid),
        ("unregister", f"BitCadence Runtime {sid}", sid),
    ]
    assert export.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows task registration")
def test_scheduler_uninstall_checks_ownership_and_is_idempotent(tmp_path):
    sid = bootstrap.current_user_sid()
    name = f"BitCadence Runtime TEST-{uuid.uuid4().hex}"
    # Remove the trigger so this task can never run on sign-in.
    xml = bootstrap.task_xml(name, sid, Path(sys.executable), ["-c", "pass"], tmp_path)
    trigger_start = xml.index("<Triggers>")
    trigger_end = xml.index("</Triggers>") + len("</Triggers>")
    xml = xml[:trigger_start] + xml[trigger_end:]
    bootstrap.register_task(name, sid, xml)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="without the BitCadence bootstrap ownership marker"):
                bootstrap.unregister_task(name, sid + "-0")
    finally:
        bootstrap.unregister_task(name, sid)

    bootstrap.unregister_task(name, sid)


@pytest.mark.parametrize(("runtime_codes", "run_seconds", "task_code", "delays"), [
    ([0], [1], 0, []),
    ([3], [1], 0, []),
    ([1, 15, 1, 0], [1, 1, 1, 1], 0, [60, 60, 60]),
    ([1, 3], [1, 1], 0, [60]),
    ([1, 7, 1, 15], [1, 599, 1, 1], 1, [60, 60, 60]),
    pytest.param([1, 1, 1, 1, 1, 1, 0], [1, 1, 1, 601, 1, 1, 1], 0, [60] * 6,
                 id="stable-run-resets-restart-budget"),
])
def test_task_launcher_retries_crashes_only(
    monkeypatch, tmp_path, runtime_codes, run_seconds, task_code, delays,
):
    from types import SimpleNamespace

    outcomes = iter(zip(runtime_codes, run_seconds, strict=True))
    slept = []
    recovery_states = []
    status_file = tmp_path / "bootstrap-status.json"
    now = 0

    def wait(timeout):
        nonlocal now
        code, elapsed = next(outcomes)
        now += elapsed
        return code

    def sleep(seconds):
        nonlocal now
        slept.append(seconds)
        status = json.loads(status_file.read_text(encoding="utf-8"))
        assert status["state"] == "retrying"
        recovery_states.append(status["restarts"])
        now += seconds

    class Adapter:
        def __init__(self, *, pidfile):
            assert pidfile == tmp_path / "bootstrap.pids"

        def spawn(self, argv, cwd, env, stdout, stderr):
            assert argv == ["python.exe", "-m", "mco.cli", "runtime", "run"]
            os.write(stderr, b"runtime diagnostic\n")
            return SimpleNamespace(pid=123, wait=wait, close=lambda: None)

        def forget_pid(self, pid):
            assert pid == 123

    monkeypatch.setattr("mco.agentd.platform.windows.WindowsAdapter", Adapter)
    monkeypatch.setattr(bootstrap, "time", SimpleNamespace(monotonic=lambda: now, sleep=sleep))
    assert bootstrap.launch_runtime(
        ["python.exe", "-m", "mco.cli", "runtime", "run"],
        tmp_path, tmp_path,
    ) == task_code
    assert not list(outcomes)
    assert slept == delays
    assert recovery_states == [index % 3 + 1 for index in range(len(delays))]
    final = json.loads(status_file.read_text(encoding="utf-8"))
    expected_state = {0: "stopped", 3: "observing"}.get(runtime_codes[-1], "exhausted")
    assert final["state"] == expected_state
    assert final["restarts"] == (recovery_states[-1] if recovery_states else 0)
    entries = (tmp_path / "bootstrap.log").read_text(encoding="ascii").splitlines()
    assert len(entries) == len(delays) + (task_code == 1)
    for entry in entries:
        timestamp, message = entry.split(" ", 1)
        assert datetime.fromisoformat(timestamp).tzinfo == timezone.utc
        assert message.startswith("Runtime exited ")
    assert (tmp_path / "bootstrap-child.log").read_text(encoding="ascii").splitlines() == [
        "runtime diagnostic",
    ] * len(runtime_codes)
