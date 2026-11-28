from pathlib import Path
import json
import subprocess
import sys

import pytest

from ops.watchdog.monitor import check


NOW = 1000


def write(runtime_dir, filename, **document):
    (runtime_dir / filename).write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(("state", "timestamp", "reported", "alert"), [
    ("stopped", NOW - 600, "stopped", None),
    ("maintenance", NOW - 600, "maintenance", None),
    ("quiesced", NOW, "quiesced", None),
    ("upgrading", NOW - 30, "upgrading", None),
    ("upgrading", NOW - 91, "stale", "heartbeat-stale"),
    ("running", NOW - 90, "running", None),
    ("running", NOW - 91, "stale", "heartbeat-stale"),
    ("running", NOW + 100, "unavailable", "heartbeat-unavailable"),
    ("crashed", NOW - 91, "stale", "heartbeat-stale"),
    ("unknown", NOW, "unknown", "runtime-state-unknown"),
])
def test_heartbeat_age_and_lifecycle(tmp_path, state, timestamp, reported, alert):
    write(tmp_path, "heartbeat.json", state=state, timestamp=timestamp)
    result = check(tmp_path, now=NOW)
    assert (result.state, result.alert) == (reported, alert)


def test_missing_heartbeat_is_unavailable(tmp_path):
    result = check(tmp_path, now=NOW)
    assert (result.state, result.alert) == ("unavailable", "heartbeat-unavailable")


def test_four_crashes_exhaust_three_bootstrap_retries(tmp_path):
    write(tmp_path, "heartbeat.json", state="stopped", timestamp=NOW - 200)
    for restarts in range(1, 4):
        write(tmp_path, "bootstrap-status.json", state="retrying", timestamp=NOW - 1, restarts=restarts)
        result = check(tmp_path, now=NOW)
        assert (result.state, result.alert) == ("recovering", None)
    write(tmp_path, "bootstrap-status.json", state="exhausted", timestamp=NOW - 1, restarts=3)
    result = check(tmp_path, now=NOW)
    assert (result.state, result.alert) == ("down", "recovery-failed")
    assert result.restarts == 3


def test_a_newer_heartbeat_clears_an_old_exhaustion(tmp_path):
    write(tmp_path, "bootstrap-status.json", state="exhausted", timestamp=NOW - 60, restarts=3)
    write(tmp_path, "heartbeat.json", state="running", timestamp=NOW)
    assert check(tmp_path, now=NOW).alert is None


def test_a_newer_clean_stop_clears_an_old_exhaustion(tmp_path):
    write(tmp_path, "bootstrap-status.json", state="exhausted", timestamp=NOW - 60, restarts=3)
    write(tmp_path, "heartbeat.json", state="stopped", timestamp=NOW)
    result = check(tmp_path, now=NOW)
    assert (result.state, result.alert) == ("stopped", None)


def test_a_crash_in_maintenance_stays_silent(tmp_path):
    write(tmp_path, "heartbeat.json", state="crashed", timestamp=NOW - 2, maintenance=True)
    write(tmp_path, "bootstrap-status.json", state="exhausted", timestamp=NOW - 1, restarts=3)
    result = check(tmp_path, now=NOW)
    assert (result.state, result.alert) == ("maintenance", None)


def test_abandoned_bootstrap_retry_alerts_after_grace_period(tmp_path):
    write(tmp_path, "heartbeat.json", state="crashed", timestamp=NOW - 92)
    write(tmp_path, "bootstrap-status.json", state="retrying", timestamp=NOW - 91, restarts=2)
    assert check(tmp_path, now=NOW).alert == "heartbeat-stale"


def test_drill_reports_all_three_faults():
    result = subprocess.run(
        [sys.executable, "-m", "ops.watchdog", "drill", "--dry-run"],
        cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "deliberate stop: state=stopped alert=none" in result.stdout
    assert "4 crashes: state=down alert=recovery-failed restarts=3" in result.stdout
    assert "stale heartbeat: state=stale alert=heartbeat-stale age=91s" in result.stdout

@pytest.mark.parametrize(("state", "expected_calls"), [("stopped", 0), ("unknown", 1)])
def test_webhook_sends_alerts_only(tmp_path, monkeypatch, capsys, state, expected_calls):
    import httpx
    from time import time

    from ops.watchdog.__main__ import run_check

    runtime_dir = tmp_path / "snapshot"
    runtime_dir.mkdir()
    write(runtime_dir, "heartbeat.json", state=state, timestamp=time())
    config_path = tmp_path / "watchdog.json"
    config_path.write_text(json.dumps({
        "runtime_dir": "snapshot", "transport": "webhook", "webhook_url": "https://example.invalid/alerts",
    }), encoding="utf-8")

    received = []

    def handle(request):
        received.append(json.loads(request.content))
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        assert run_check(config_path) == expected_calls
    observed = json.loads(capsys.readouterr().out)
    assert received == [observed] * expected_calls
    assert observed["state"] == state


def test_delivery_error_keeps_private_config_out_of_output(tmp_path, monkeypatch, capsys):
    import httpx

    from ops.watchdog.__main__ import run_check

    config_path = tmp_path / "watchdog.json"
    private_url = "https://example.invalid/private-endpoint"
    config_path.write_text(json.dumps({
        "runtime_dir": "snapshot", "transport": "webhook", "webhook_url": private_url,
    }), encoding="utf-8")

    def fail(url, **kwargs):
        raise httpx.ConnectError(f"Cannot reach {url}")

    monkeypatch.setattr(httpx, "post", fail)
    assert run_check(config_path) == 2
    captured = capsys.readouterr()
    assert "Watchdog alert delivery failed." in captured.err
    assert private_url not in captured.out + captured.err
