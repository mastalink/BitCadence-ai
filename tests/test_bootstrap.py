import os
from pathlib import Path
import sys
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


@pytest.mark.parametrize(("runtime_codes", "task_code", "delays"), [
    ([0], 0, []),
    ([3], 0, []),
    ([1, 15, 1, 0], 0, [60, 60, 60]),
    ([1, 3], 0, [60]),
    ([1, 7, 1, 15], 1, [60, 60, 60]),
])
def test_task_launcher_retries_crashes_only(monkeypatch, tmp_path, runtime_codes, task_code, delays):
    from types import SimpleNamespace

    outcomes = iter(runtime_codes)
    slept = []

    class Adapter:
        def __init__(self, *, pidfile):
            assert pidfile == tmp_path / "bootstrap.pids"

        def spawn(self, argv, cwd, env, stdout, stderr):
            assert argv == ["python.exe", "-m", "mco.cli", "runtime", "run"]
            return SimpleNamespace(pid=123, wait=lambda timeout: next(outcomes), close=lambda: None)

        def forget_pid(self, pid):
            assert pid == 123

    monkeypatch.setattr("mco.agentd.platform.windows.WindowsAdapter", Adapter)
    monkeypatch.setattr("time.sleep", slept.append)
    assert bootstrap.launch_runtime(
        ["python.exe", "-m", "mco.cli", "runtime", "run"],
        tmp_path, tmp_path,
    ) == task_code
    assert not list(outcomes)
    assert slept == delays
