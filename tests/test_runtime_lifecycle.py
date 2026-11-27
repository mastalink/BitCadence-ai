"""The lifecycle table (DECISIONS.md section 4), row by row, without real processes."""

from __future__ import annotations

import json

import pytest

from mco.runtime import core
from mco.runtime.core import EXIT_ALREADY_RUNNING, EXIT_CRASH, EXIT_STOPPED
from mco.runtime.paths import observed_state, read_heartbeat
from mco.runtime.supervision import windowless_interpreter
from tests.runtime_fakes import FakeAdapter, FakeClock, fleet, make_runtime


ALL = ["alpha-program", "gateway-program", "scheduler-program"]


@pytest.fixture(autouse=True)
def stop_file(tmp_path, monkeypatch):
    path = tmp_path / "STOP"
    monkeypatch.setenv("MCO_STOP_FILE", str(path))
    return path


@pytest.fixture
def home(tmp_path):
    directory = tmp_path / "home"
    directory.mkdir()
    (directory / "fleet.toml").write_text(fleet("alpha"), encoding="utf-8")
    return directory


def test_gateway_and_scheduler_are_supervised_children(home):
    runtime = make_runtime(home)
    runtime.start()
    assert runtime.adapter.alive() == ALL
    runtime.shutdown()
    assert runtime.adapter.alive() == []


@pytest.mark.parametrize("program", ["gateway-program", "scheduler-program"])
def test_a_service_that_exits_is_restarted_even_with_code_zero(home, program):
    clock = FakeClock()
    runtime = make_runtime(home, clock)
    runtime.start()
    first = runtime.adapter.latest(program)
    first.returncode = 0
    runtime.step()
    clock.advance(5)
    runtime.step()
    second = runtime.adapter.latest(program)
    assert second is not first and second.poll() is None
    runtime.shutdown()


def test_every_spawn_carries_its_own_identity_never_an_inherited_one(home, monkeypatch):
    monkeypatch.setenv("AGENT_INSTANCE_ID", "the-owner")
    monkeypatch.setenv("AGENT_ROLE", "admin")
    runtime = make_runtime(home)
    runtime.start()
    identities = {
        argv[0]: (env["AGENT_ROLE"], env["AGENT_INSTANCE_ID"]) for argv, env, _ in runtime.adapter.spawns
    }
    assert identities == {
        "alpha-program": ("example", "alpha"),
        "gateway-program": ("runtime", "runtime-gateway"),
        "scheduler-program": ("runtime", "runtime-scheduler"),
    }
    runtime.shutdown()


def test_waker_children_get_a_hidden_console_interpreter(tmp_path):
    (tmp_path / "python.exe").write_bytes(b"")
    windowed = str(tmp_path / "pythonw.exe")
    assert windowless_interpreter([windowed, "-m", "mco"]) == [str(tmp_path / "python.exe"), "-m", "mco"]
    assert windowless_interpreter(["alpha-program"]) == ["alpha-program"]


def test_pane_workers_are_left_to_the_terminal_host(home):
    (home / "fleet.toml").write_text(fleet("alpha", mode="pane"), encoding="utf-8")
    runtime = make_runtime(home)
    runtime.start()
    assert "alpha-program" not in runtime.adapter.programs()
    assert runtime.supervisor.status("alpha")["state"] == "stopped"
    runtime.shutdown()


def test_deliberate_stop_exits_zero_and_records_stopped(home):
    runtime = make_runtime(home)
    runtime.request_stop("test")
    assert runtime.run(handle_signals=False) == EXIT_STOPPED
    assert runtime.adapter.alive() == []
    assert runtime.adapter.released == 1
    assert observed_state(read_heartbeat(runtime.paths)) == "stopped"


def test_unexpected_crash_exits_non_zero_and_records_crashed(home, monkeypatch):
    monkeypatch.setattr(core, "MAX_FAILED_TICKS", 3)
    runtime = make_runtime(home)

    def broken() -> None:
        raise OSError("tick broke")

    runtime.supervisor.tick = broken
    assert runtime.run(handle_signals=False) == EXIT_CRASH
    assert runtime.adapter.alive() == []
    assert observed_state(read_heartbeat(runtime.paths)) == "crashed"


def test_one_bad_tick_is_not_a_crash(home):
    runtime = make_runtime(home)
    calls = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("once")
        runtime.request_stop("test")

    runtime.supervisor.tick = flaky
    assert runtime.run(handle_signals=False) == EXIT_STOPPED


def test_second_instance_is_refused_and_leaves_the_owners_heartbeat_alone(home):
    runtime = make_runtime(home, adapter=FakeAdapter(already_running=True))
    runtime.paths.runtime.mkdir(parents=True)
    runtime.paths.heartbeat.write_text('{"owner": true}', encoding="utf-8")
    assert runtime.run(handle_signals=False) == EXIT_ALREADY_RUNNING
    assert runtime.adapter.spawns == []
    assert json.loads(runtime.paths.heartbeat.read_text(encoding="utf-8")) == {"owner": True}


def test_heartbeat_is_written_every_fifteen_seconds(home):
    clock = FakeClock()
    runtime = make_runtime(home, clock)
    runtime.start()
    runtime.step()
    writes = []
    original = runtime.write_heartbeat
    runtime.write_heartbeat = lambda *a, **k: (writes.append(clock.now), original(*a, **k))
    start = clock.now
    for _ in range(45):
        clock.advance(1)
        runtime.step()
    assert [moment - start for moment in writes] == [15, 30, 45]
    beat = read_heartbeat(runtime.paths)
    assert beat["state"] == "running" and beat["children"]["runtime:gateway"]["pid"]
    runtime.shutdown()


def test_a_silent_or_vanished_runtime_is_not_reported_as_running(home):
    runtime = make_runtime(home)
    runtime.start()
    beat = read_heartbeat(runtime.paths)
    assert observed_state(beat) == "running"
    assert observed_state(beat, now=beat["timestamp"] + 91) == "stale"
    assert observed_state({**beat, "process_created": 1.0}) == "dead"
    runtime.shutdown()


def test_stop_file_means_no_spawns_no_exit(home, stop_file):
    stop_file.write_text("{}", encoding="utf-8")
    clock = FakeClock()
    runtime = make_runtime(home, clock)
    ticks = []
    real_tick = runtime.supervisor.tick

    def tick() -> None:
        real_tick()
        clock.advance(120)
        ticks.append(1)
        if len(ticks) == 20:
            assert read_heartbeat(runtime.paths)["state"] == "quiesced"
            runtime.request_stop("test finished")

    runtime.supervisor.tick = tick
    assert runtime.run(handle_signals=False) == EXIT_STOPPED
    assert len(ticks) == 20, "it must keep running while STOP is set"
    assert runtime.adapter.spawns == []


def test_stop_leaves_running_children_alone_and_holds_restarts_until_cleared(home, stop_file):
    clock = FakeClock()
    runtime = make_runtime(home, clock)
    runtime.start()
    stop_file.write_text("{}", encoding="utf-8")
    runtime.step()
    assert runtime.adapter.alive() == ALL

    runtime.adapter.latest("gateway-program").returncode = 1
    (home / "fleet.toml").write_text(fleet("alpha", "beta"), encoding="utf-8")
    for _ in range(5):
        clock.advance(120)
        runtime.step()
    assert len(runtime.adapter.spawns) == 3, "no restart and no new worker while STOP is set"

    stop_file.unlink()
    clock.advance(120)
    runtime.step()
    assert runtime.adapter.alive() == ["alpha-program", "beta-program", "gateway-program", "scheduler-program"]
    runtime.shutdown()


def test_maintenance_stops_children_holds_spawns_and_releases(home):
    clock = FakeClock()
    runtime = make_runtime(home, clock)
    runtime.start()
    runtime.set_maintenance(True)
    assert runtime.adapter.alive() == []
    assert read_heartbeat(runtime.paths)["state"] == "maintenance"

    # Neither a config change, an operator start nor time brings anything back.
    (home / "fleet.toml").write_text(fleet("alpha", "beta"), encoding="utf-8")
    runtime.supervisor.start("alpha")
    runtime.supervisor.restart("runtime:gateway")
    for _ in range(3):
        clock.advance(1000)
        runtime.step()
    assert runtime.adapter.alive() == []

    runtime.set_maintenance(False)
    runtime.step()
    assert runtime.adapter.alive() == ["alpha-program", "beta-program", "gateway-program", "scheduler-program"]
    runtime.shutdown()


def test_maintenance_survives_a_restart(home):
    first = make_runtime(home)
    first.start()
    first.set_maintenance(True)
    first.shutdown()

    second = make_runtime(home)
    second.start()
    assert second.adapter.spawns == []
    assert second.state == "maintenance"
    second.set_maintenance(False)
    assert second.adapter.alive() == ALL
    second.shutdown()
