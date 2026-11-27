"""``/v1/runtime``, the CLI that calls it, and the scheduler hold."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from mco import launcher
from mco.runtime import cli as runtime_cli
from mco.runtime.control import create_runtime_app
from mco.runtime.paths import RuntimePaths
from tests.runtime_fakes import FakeClock, fleet, make_runtime


@pytest.fixture(autouse=True)
def stop_file(tmp_path, monkeypatch):
    path = tmp_path / "STOP"
    monkeypatch.setenv("MCO_STOP_FILE", str(path))
    return path


class Tokens:
    def control_token(self) -> str:
        return "control-secret"

    def logs_token(self) -> str:
        return "logs-secret"


@pytest.fixture
def running(tmp_path):
    (tmp_path / "fleet.toml").write_text(fleet("alpha"), encoding="utf-8")
    runtime = make_runtime(tmp_path, FakeClock(), token_store=Tokens())
    runtime.start()
    yield runtime, TestClient(create_runtime_app(runtime))
    runtime.shutdown()


AUTH = {"Authorization": "Bearer control-secret"}


@pytest.mark.parametrize("method,path", [
    ("get", "/v1/runtime"),
    ("post", "/v1/runtime/stop"),
    ("post", "/v1/runtime/maintenance"),
    ("post", "/v1/runtime/rollback"),
])
def test_runtime_routes_need_the_control_token(running, method, path):
    runtime, client = running
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer logs-secret"}):
        assert getattr(client, method)(path, headers=headers).status_code == 401
    assert runtime.stop_reason is None and not runtime.maintenance


def test_status_maintenance_stop_and_rollback(running):
    runtime, client = running
    document = client.get("/v1/runtime", headers=AUTH).json()
    assert document["schema"] == "bitcadence.runtime/1" and document["state"] == "running"
    assert set(document["children"]) == {"alpha", "runtime:gateway", "runtime:scheduler"}

    assert client.post("/v1/runtime/rollback", headers=AUTH).status_code == 409
    on = client.post("/v1/runtime/maintenance", headers=AUTH, json={"enabled": True}).json()
    assert on["state"] == "maintenance" and runtime.adapter.alive() == []
    off = client.post("/v1/runtime/maintenance", headers=AUTH, json={"enabled": False}).json()
    assert off["state"] == "running" and len(runtime.adapter.alive()) == 3
    assert client.post("/v1/runtime/stop", headers=AUTH).json() == {"stopping": True}
    assert runtime.stop_reason == "control API"


def test_replacing_the_config_over_the_api_keeps_the_services(running):
    runtime, client = running
    snapshot = client.get("/v1/config", headers=AUTH).json()
    headers = dict(AUTH)
    if "generation" in snapshot:
        headers["If-Match"] = snapshot["generation"]
    response = client.put("/v1/config", headers=headers, json={"content": fleet("beta")})
    assert response.status_code == 200, response.text
    assert runtime.adapter.alive() == ["beta-program", "gateway-program", "scheduler-program"]
    assert len(runtime.config.store.generations()) == 2


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_cli, "_paths", lambda: RuntimePaths(tmp_path))
    return tmp_path


def invoke(*args):
    return CliRunner().invoke(runtime_cli.runtime_app, list(args))


def test_cli_when_nothing_is_running(home):
    assert "never-started" in invoke("status").output
    stopped = invoke("stop")
    assert stopped.exit_code == 0 and "not running" in stopped.output
    assert invoke("maintenance", "on").exit_code == 0
    assert RuntimePaths(home).maintenance.exists()
    assert invoke("maintenance", "off").exit_code == 0
    assert not RuntimePaths(home).maintenance.exists()
    assert invoke("maintenance", "sideways").exit_code == 1
    assert invoke("rollback").exit_code == 1


def test_cli_status_fails_for_a_dead_runtime(home):
    paths = RuntimePaths(home)
    paths.runtime.mkdir(parents=True)
    paths.heartbeat.write_text('{"state": "running", "pid": 1, "process_created": 1}', encoding="utf-8")
    result = invoke("status")
    assert result.exit_code == 1 and "dead" in result.output


def test_cli_offline_rollback(home):
    from mco.runtime.lkg import LkgStore

    store = LkgStore(RuntimePaths(home).lkg)
    for name in ("one", "two"):
        store.save(fleet(name).encode())
    (home / "fleet.toml").write_text(fleet("two"), encoding="utf-8")
    assert invoke("rollback").exit_code == 0
    assert (home / "fleet.toml").read_bytes() == fleet("one").encode()


def test_runtime_is_registered_on_the_main_cli():
    from mco.cli import app

    result = CliRunner().invoke(app, ["runtime", "--help"])
    assert result.exit_code == 0
    for command in ("run", "stop", "status", "maintenance", "rollback"):
        assert command in result.output


SCHEDULES = """
launchers:
  tidy:
    role: example
    title: tidy up
schedules:
  often:
    launcher: tidy
    every: 1m
"""


class Board:
    def __init__(self) -> None:
        self.sent = 0

    def send(self, **_):
        self.sent += 1
        return {"success": True, "job": {"id": f"job-{self.sent}"}}

    def jobs(self, include_archived=False):
        return []


def test_a_held_scheduler_fires_nothing_and_the_run_is_still_due_afterwards(tmp_path):
    config, state = tmp_path / "schedules.yaml", tmp_path / "state.json"
    config.write_text(SCHEDULES, encoding="utf-8")
    board, now = Board(), datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert launcher.tick(board, config, state, now=now, hold=lambda: True) == []
    assert board.sent == 0 and not state.exists()
    assert [row["action"] for row in launcher.tick(board, config, state, now=now, hold=lambda: False)] == ["fired"]
    assert board.sent == 1


def test_a_supervised_scheduler_with_no_config_is_idle(tmp_path, caplog):
    launcher.run_forever(
        Board(), tmp_path / "absent.yaml", tmp_path / "state.json",
        interval=0, max_ticks=2, hold=lambda: False,
    )
    assert "scheduler tick failed" not in caplog.text


def test_the_scheduler_child_holds_every_fire_while_stop_is_set(monkeypatch, stop_file):
    from mco import cli, logging_setup
    from mco.runtime import scheduler_child

    seen = {}
    monkeypatch.setattr(cli, "_gateway_client", lambda: "client")
    monkeypatch.setattr(logging_setup, "configure_logging", lambda: None)
    monkeypatch.setattr(launcher, "run_forever", lambda client, **options: seen.update(options, client=client))
    assert scheduler_child.main(["--interval", "5"]) == 0
    assert seen["client"] == "client" and seen["interval"] == 5
    assert seen["hold"]() is False
    stop_file.write_text("{}", encoding="utf-8")
    assert seen["hold"]() is True
