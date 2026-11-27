"""Last-known-good generations and rollback."""

from __future__ import annotations

import pytest

from mco.runtime.config import RollbackUnavailable
from mco.runtime.lkg import LkgStore
from tests.runtime_fakes import FakeClock, fleet, make_runtime


CORRUPT = ['[workers.alpha\nrole = ', '[workers.alpha]\nmode = "waker"\n', fleet("alpha") + "\x00"]


@pytest.fixture(autouse=True)
def stop_file(tmp_path, monkeypatch):
    path = tmp_path / "STOP"
    monkeypatch.setenv("MCO_STOP_FILE", str(path))
    return path


def started(home, *names):
    home.mkdir(exist_ok=True)
    (home / "fleet.toml").write_text(fleet(*names), encoding="utf-8")
    clock = FakeClock()
    runtime = make_runtime(home, clock)
    runtime.start()
    return runtime, clock


def edit(runtime, clock, content: str) -> None:
    runtime.paths.fleet.write_text(content, encoding="utf-8")
    clock.advance(10)
    runtime.step()


def workers(runtime) -> list[str]:
    return [row["name"] for row in runtime.supervisor.status() if not row["name"].startswith("runtime:")]


def test_store_keeps_five_generations_and_skips_repeats(tmp_path):
    store = LkgStore(tmp_path / "lkg")
    for number in range(8):
        store.save(f"# {number}\n".encode())
        store.save(f"# {number}\n".encode())
    assert [p.read_bytes() for p in store.generations()] == [f"# {n}\n".encode() for n in range(3, 8)]


@pytest.mark.parametrize("bad", CORRUPT + ["", '[workers."runtime:gateway"]\nrole = "x"\nmode = "off"\n'])
def test_corrupt_fleet_keeps_the_running_generation(tmp_path, bad):
    runtime, clock = started(tmp_path / "home", "alpha")
    generation = runtime.config.generation
    kept = [p.read_bytes() for p in runtime.config.store.generations()]
    edit(runtime, clock, bad)
    assert workers(runtime) == ["alpha"]
    assert runtime.adapter.latest("alpha-program").poll() is None
    assert runtime.config.generation == generation
    assert runtime.config.last_error
    assert [p.read_bytes() for p in runtime.config.store.generations()] == kept
    edit(runtime, clock, fleet("alpha", "beta"))
    assert workers(runtime) == ["alpha", "beta"] and runtime.config.last_error is None
    runtime.shutdown()


def test_restart_with_a_corrupt_file_runs_the_last_known_good(tmp_path):
    home = tmp_path / "home"
    first, _ = started(home, "alpha")
    first.shutdown()
    (home / "fleet.toml").write_text(CORRUPT[0], encoding="utf-8")
    second = make_runtime(home)
    second.start()
    assert "alpha-program" in second.adapter.alive()
    assert "last-known-good" in second.config.last_error
    second.shutdown()


def test_corrupt_file_with_no_history_still_runs_the_services(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "fleet.toml").write_text(CORRUPT[0], encoding="utf-8")
    runtime = make_runtime(home)
    runtime.start()
    assert runtime.adapter.alive() == ["gateway-program", "scheduler-program"]
    assert "no last-known-good" in runtime.config.last_error
    runtime.shutdown()


def test_rollback_restores_the_previous_generation_and_walks_back(tmp_path):
    runtime, clock = started(tmp_path / "home", "one")
    edit(runtime, clock, fleet("two"))
    edit(runtime, clock, fleet("three"))

    runtime.config.rollback()
    assert workers(runtime) == ["two"]
    assert runtime.paths.fleet.read_text(encoding="utf-8").splitlines() == fleet("two").splitlines()
    assert runtime.paths.fleet.with_name("fleet.toml.rolled-back").exists()
    clock.advance(10)
    runtime.step()
    assert workers(runtime) == ["two"], "the restored file must not be seen as a new edit"

    runtime.config.rollback()
    assert workers(runtime) == ["one"]
    with pytest.raises(RollbackUnavailable):
        runtime.config.rollback()
    runtime.shutdown()


def test_rollback_under_stop_restores_the_file_and_applies_after(tmp_path, stop_file):
    runtime, clock = started(tmp_path / "home", "one")
    edit(runtime, clock, fleet("two"))
    stop_file.write_text("{}", encoding="utf-8")
    runtime.config.rollback()
    assert workers(runtime) == ["two"]
    assert runtime.adapter.latest("two-program").poll() is None
    stop_file.unlink()
    clock.advance(10)
    runtime.step()
    assert workers(runtime) == ["one"]
    runtime.shutdown()
