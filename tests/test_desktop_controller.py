import json
import os
import time

import psutil
import pytest

from mco.desktop.app import status_line
from mco.desktop.controller import DesktopController
from mco.desktop.controller import RuntimeView
from mco.runtime.control import ControlError
from mco.runtime.paths import RuntimePaths


def test_desktop_without_runtime_is_offline_and_creates_no_runtime_files(tmp_path):
    paths = RuntimePaths(tmp_path / ".mco")
    controller = DesktopController(paths=paths)
    view = controller.snapshot()
    assert view.state == "never-started"
    assert view.rows == []
    assert view.lines == []
    assert not paths.home.exists()


def test_desktop_observes_runtime_over_read_only_api(tmp_path, monkeypatch):
    paths = RuntimePaths(tmp_path)
    paths.runtime.mkdir()
    beat = {
        "pid": os.getpid(), "process_created": psutil.Process().create_time(),
        "timestamp": time.time(), "control_port": 19200, "state": "running",
    }
    paths.heartbeat.write_text(json.dumps(beat), encoding="utf-8")
    (tmp_path / "agentd.token").write_text("control", encoding="utf-8")
    (tmp_path / "agentd.logs.token").write_text("logs", encoding="utf-8")
    calls = []

    def call(method, path, *, port, token, timeout):
        calls.append((method, path, port, token))
        if path == "/v1/runtime":
            return {**beat, "schema": "bitcadence.runtime/1",
                    "state": "maintenance", "config_error": "invalid config",
                    "children": {"runtime:gateway": {"state": "stopped", "pid": None}}}
        return {"lines": ["runtime log"]}

    monkeypatch.setattr("mco.desktop.controller.call_runtime", call)
    controller = DesktopController(paths=paths)
    view = controller.snapshot("runtime:gateway")
    assert view.state == "maintenance"
    assert view.rows == [("runtime:gateway", "stopped", "", "")]
    assert view.config_error == "invalid config"
    assert view.lines == ["runtime log"]
    assert calls == [
        ("GET", "/v1/runtime", 19200, "control"),
        ("GET", "/v1/logs?tail=100&worker=runtime%3Agateway", 19200, "logs"),
    ]

    def unavailable(*args, **kwargs):
        raise ControlError("connection refused")

    monkeypatch.setattr("mco.desktop.controller.call_runtime", unavailable)
    view = controller.snapshot()
    assert view.state == "unreachable"
    assert view.rows == []
    assert "connection refused" in view.error


@pytest.mark.parametrize(("state", "label"), [
    ("never-started", "Offline"),
    ("unreachable", "unreachable"),
    ("stale", "stale"),
    ("maintenance", "maintenance"),
    ("quiesced", "STOP"),
    ("running", "observing"),
])
def test_desktop_status_distinguishes_offline_from_runtime_conditions(state, label):
    assert label in status_line(RuntimeView(state))
