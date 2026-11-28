import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import pytest

from mco.agentd.tokens import sign_control_request
from mco.runtime.control import create_runtime_app
from mco.runtime.plugins.contract import schema_documents
from mco.runtime.plugins.grants import Grant, grant_message
from mco.runtime.plugins.runner import PluginRunner, PluginRefused
from mco.runtime.plugins.trust import inspect_plugin, trust_text
from tests.runtime_fakes import make_runtime


ROOT = Path(__file__).resolve().parents[1]


def test_committed_schemas_equal_generated():
    directory = ROOT / "schemas" / "plugins"
    assert {path.name: path.read_bytes() for path in directory.glob("*.json")} == schema_documents()


class RecordingAdapter:
    def __init__(self):
        self.calls = []
        self.processes = []

    def spawn(self, argv, cwd, env, stdout, stderr):
        self.calls.append((argv, env))
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr)
        self.processes.append(process)
        return process


def approve(home, manifest):
    candidate = inspect_plugin(manifest)
    (home / "plugins.lock.toml").write_text(trust_text(candidate), encoding="utf-8")
    return candidate


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("MCO_STOP_FILE", str(tmp_path / "STOP"))
    monkeypatch.setenv("MCO_LOCAL_TOKEN", "synthetic-test-credential")
    folder = tmp_path / "plugins" / "example"
    folder.mkdir(parents=True)
    script = folder / "example.py"
    script.write_text("pass\n", encoding="utf-8")
    manifest = folder / "bitcadence-plugin.toml"
    manifest.write_text(f'''
schema = "bitcadence.plugin/1"
id = "example"
[identity]
instance = "example-agent"
role = "plugin"
[status]
argv = [{json.dumps(sys.executable.replace(os.sep, "/"))}, "example.py", "status"]
interval_seconds = 100
[schedules.tick]
argv = [{json.dumps(sys.executable.replace(os.sep, "/"))}, "example.py", "schedule"]
interval_seconds = 10
[actions.echo]
argv = [{json.dumps(sys.executable.replace(os.sep, "/"))}, "example.py", "action"]
channel = "example-channel"
[[actions.echo.parameters]]
name = "value"
type = "string"
''', encoding="utf-8")
    candidate = approve(tmp_path, manifest)
    adapter = RecordingAdapter()
    wall = [100.0]
    owner = Ed25519PrivateKey.generate()

    def runner():
        return PluginRunner(tmp_path, adapter, blocked=lambda: False,
                            wall=lambda: wall[0], owner_key=owner.public_key(),
                            owner_instance="example-owner")

    def grant(value, **overrides):
        fields = dict(nonce="012345678901234567890123456789ab", owner="example-owner",
                      plugin="example", action="echo", parameters={"value": value},
                      trust=candidate.fingerprint, audience=runtime.audience,
                      issued_at=wall[0], expires_at=wall[0] + 30, verification="confirmed")
        claims = Grant(**(fields | overrides))
        return {"claims": claims.model_dump(), "signature": owner.sign(grant_message(claims)).hex()}

    runtime = runner()
    yield runtime, runner, adapter, wall, script, grant
    runtime.close()


def test_changed_script_pauses_before_any_spawn(deployment):
    runtime, _, adapter, _, script, _ = deployment
    script.write_text("# changed\n", encoding="utf-8")
    runtime.tick()
    state = runtime.document()["example"]
    assert state["state"] == "review"
    assert any("example.py" in key for key in state["changes"])
    assert adapter.calls == []


def test_changed_executable_pauses_before_any_spawn(deployment):
    runtime, _, adapter, _, script, _ = deployment
    executable = script.with_name("example-binary.exe")
    shutil.copyfile(sys.executable, executable)
    manifest = script.with_name("bitcadence-plugin.toml")
    original = json.dumps(sys.executable.replace(os.sep, "/"))
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(original, '"example-binary.exe"'),
                        encoding="utf-8")
    approve(runtime.home, manifest)
    with executable.open("ab") as stream:
        stream.write(b"changed")
    runtime.tick()
    report = runtime.document()["example"]
    assert report["state"] == "review"
    assert "status/0:example-binary.exe" in report["changes"]
    assert adapter.calls == []


def test_durable_schedule_catches_up_once_after_restart(deployment):
    runtime, runner, adapter, wall, script, _ = deployment
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    approve(runtime.home, script.with_name("bitcadence-plugin.toml"))
    runtime.tick()  # records first schedule for 110
    wall[0] = 110
    runtime.tick()  # runtime is stopped with the scheduled invocation in flight
    assert adapter.processes[-1].poll() is None
    runtime.close()
    wall[0] = 149
    other = runner()
    try:
        other.tick()
        other.tick()
        assert [argv[-1] for argv, _ in adapter.calls].count("schedule") == 2
    finally:
        other.close()


def test_due_schedule_skips_overlap_and_keeps_phase(deployment):
    runtime, _, adapter, wall, script, _ = deployment
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    approve(runtime.home, script.with_name("bitcadence-plugin.toml"))
    for now in (100, 110, 120):
        wall[0] = now
        runtime.tick()
    assert [argv[-1] for argv, _ in adapter.calls].count("schedule") == 1
    adapter.processes[-1].kill()
    adapter.processes[-1].wait(timeout=5)
    wall[0] = 121
    runtime.tick()
    assert [argv[-1] for argv, _ in adapter.calls].count("schedule") == 1
    wall[0] = 130
    runtime.tick()
    assert [argv[-1] for argv, _ in adapter.calls].count("schedule") == 2


def test_stop_mid_action_is_recorded_as_refused(deployment):
    runtime, _, adapter, _, script, grant = deployment
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    candidate = approve(runtime.home, script.with_name("bitcadence-plugin.toml"))
    run = runtime.start_action("example", "echo", {"value": "value"}, grant("value", trust=candidate.fingerprint))
    assert adapter.processes[-1].poll() is None
    Path(os.environ["MCO_STOP_FILE"]).touch()
    runtime.tick()
    assert runtime.action_result(run)["outcome"] == "refused"
    assert adapter.processes[-1].poll() is not None
    assert runtime.state.audit_valid()
    assert len(adapter.calls) == 1


def test_action_passes_one_literal_element(deployment):
    runtime, _, adapter, _, script, grant = deployment
    script.write_text(
        "import sys\nraise SystemExit(0 if sys.argv[2:] == ['space & \"quoted\" ; $()'] else 42)\n",
        encoding="utf-8",
    )
    candidate = approve(runtime.home, script.with_name("bitcadence-plugin.toml"))
    value = 'space & "quoted" ; $()'
    run = runtime.start_action("example", "echo", {"value": value}, grant(value, trust=candidate.fingerprint))
    argv, _ = adapter.calls[0]
    assert argv[-2:] == ["action", value]
    assert adapter.processes[-1].wait(timeout=5) == 0
    runtime.tick()
    assert runtime.action_result(run)["outcome"] == "ok"


def test_every_plugin_spawn_has_explicit_identity_and_no_client_token(deployment):
    runtime, _, adapter, wall, _, grant = deployment
    runtime.tick()
    wall[0] += 10
    runtime.tick()
    runtime.start_action("example", "echo", {"value": "value"}, grant("value"))
    assert [(argv[3], env["AGENT_INSTANCE_ID"]) for argv, env in adapter.calls] == [
        ("status", "example-agent"), ("schedule", "example-agent"), ("action", "example-owner")
    ]
    assert all("MCO_LOCAL_TOKEN" not in env for _, env in adapter.calls)
    with pytest.raises(PluginRefused):
        runtime.start_action("example", "echo", {"value": "value"}, grant("value"))


@pytest.mark.parametrize("fault", ["wrong-parameters", "bad-signature", "wrong-audience", "expired"])
def test_unverified_grants_do_not_spawn(deployment, fault):
    runtime, _, adapter, wall, _, grant = deployment
    overrides = {
        "wrong-audience": {"audience": "a" * 32},
        "expired": {"issued_at": wall[0] - 31, "expires_at": wall[0] - 1},
    }.get(fault, {})
    signed = grant("value", **overrides)
    if fault == "bad-signature":
        signed["signature"] = "00" * 64
    parameters = {"value": "changed" if fault == "wrong-parameters" else "value"}
    with pytest.raises(PluginRefused):
        runtime.start_action("example", "echo", parameters, signed)
    assert adapter.calls == []


def test_dangerous_action_requires_platform_verification(deployment):
    runtime, _, adapter, _, script, grant = deployment
    path = script.with_name("bitcadence-plugin.toml")
    path.write_text(path.read_text(encoding="utf-8").replace(
        "[actions.echo]", "[actions.echo]\ndangerous = true"
    ), encoding="utf-8")
    candidate = approve(runtime.home, path)
    with pytest.raises(PluginRefused, match="platform user verification"):
        normal = grant("value", trust=candidate.fingerprint)
        runtime.start_action("example", "echo", {"value": "value"}, normal)
    assert adapter.calls == []
    strong = grant("value", trust=candidate.fingerprint, verification="platform-user-verified")
    assert runtime.start_action("example", "echo", {"value": "value"}, strong)


def test_channel_pause_refuses_actions_without_pausing_status(deployment):
    runtime, _, adapter, _, _, grant = deployment
    switch = runtime.home / "plugin-pauses" / "channels" / "example-channel"
    switch.parent.mkdir(parents=True)
    switch.touch()
    with pytest.raises(PluginRefused, match="stopped or paused"):
        runtime.start_action("example", "echo", {"value": "value"}, grant("value"))
    runtime.tick()
    assert [argv[-1] for argv, _ in adapter.calls] == ["status"]


@pytest.mark.parametrize("sensitive", [False, True])
def test_status_and_structured_events_publish_only_validated_output(deployment, sensitive):
    runtime, _, adapter, _, script, _ = deployment
    doc = {"schema": "bitcadence.status/1", "health": "ok", "metrics": {"count": 2}}
    if sensitive:
        doc["message"] = "synthetic-test-credential"
    stdout = '@@bcd {"type":"sample","state":"ok"}\n' + json.dumps(doc)
    script.write_text(f"print({stdout!r})\n", encoding="utf-8")
    approve(runtime.home, script.with_name("bitcadence-plugin.toml"))
    runtime.tick()
    assert adapter.processes[-1].wait(timeout=5) == 0
    runtime.tick()
    result = runtime.document()["example"]
    assert result["events"] == [{"command": "status", "type": "sample", "state": "ok"}]
    if sensitive:
        assert result["status"] == {"error": "invalid or sensitive status output"}
    else:
        assert result["status"]["metrics"] == {"count": 2}


def test_runtime_loopback_controls_plugin_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("MCO_STOP_FILE", str(tmp_path / "STOP"))
    home = tmp_path / "home"
    folder = home / "plugins" / "example-hello"
    shutil.copytree(ROOT / "plugins" / "example-hello", folder)
    manifest = folder / "bitcadence-plugin.toml"
    executable = json.dumps(Path(sys.executable).as_posix())
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"python"', executable), encoding="utf-8")
    candidate = approve(home, manifest)
    owner = Ed25519PrivateKey.generate()
    key = owner.public_key().public_bytes_raw().hex()
    (home / "plugin-owner.toml").write_text(
        f'instance = "example-owner"\npublic_key = "{key}"\n', encoding="utf-8"
    )
    runtime = make_runtime(home, services=[])
    runtime.start()
    try:
        runtime.step()
        assert runtime.adapter.spawns[0][1]["AGENT_INSTANCE_ID"] == "example-hello-agent"
        with TestClient(create_runtime_app(runtime)) as client:
            def request(method, path, **kwargs):
                challenge = client.get("/v1/auth-challenge").json()["challenge"]
                proof = sign_control_request(runtime.token_store.control_token(), challenge, method, path.encode("ascii"))
                headers = {"Authorization": f"Agentd-HMAC {challenge}:{proof}"}
                return client.request(method, path, headers=headers, **kwargs)

            assert client.get("/v1/plugins").status_code == 401
            listed = request("GET", "/v1/plugins").json()
            assert listed["plugins"]["example-hello"]["state"] == "ready"
            now = runtime.plugins.wall()
            parameters = {"name": "Example"}
            claims = Grant(
                nonce="012345678901234567890123456789ab", owner="example-owner",
                plugin="example-hello", action="greet", parameters=parameters, trust=candidate.fingerprint,
                audience=listed["audience"], issued_at=now, expires_at=now + 30, verification="confirmed",
            )
            response = request("POST", "/v1/plugins/example-hello/actions/greet", json={
                "parameters": parameters,
                "grant": {"claims": claims.model_dump(), "signature": owner.sign(grant_message(claims)).hex()},
            })
            assert response.status_code == 202
            assert runtime.adapter.spawns[-1][1]["AGENT_INSTANCE_ID"] == "example-owner"
            runtime.adapter.spawns[-1][2].returncode = 0
            runtime.step()
            run = response.json()["run"]
            assert request("GET", f"/v1/plugins/actions/{run}").json()["outcome"] == "ok"
    finally:
        runtime.shutdown()
