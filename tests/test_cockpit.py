import json
import subprocess
from pathlib import Path

import pytest

from mco import cockpit


class FakeHerdr:
    """Stateful stand-in for the herdr CLI (JSON shapes copied from herdr 0.9.5)."""

    def __init__(self, running=True):
        self.running = running
        self.workspaces = []      # [{"workspace_id", "label"}]
        self.panes = []           # [{"pane_id", "workspace_id", "label"?}]
        self.ran = []             # [(pane_id, command)]
        self.calls = []
        self.spawned = []
        self.idle = set()         # pane ids restored as idle shells (simulated reboot)

    def runner(self, argv):
        assert argv[1:3] == ["--session", "bitcadence"], argv
        args = list(argv[3:])
        self.calls.append(args)

        def ok(obj=None, text=None):
            out = text if text is not None else (json.dumps(obj) if obj is not None else "")
            return subprocess.CompletedProcess(argv, 0, out, "")

        if args[:2] == ["status", "server"]:
            if not self.running:
                return subprocess.CompletedProcess(argv, 1, "", "server not running")
            return ok(text="server:\n  status: running")
        if args[:2] == ["workspace", "list"]:
            return ok({"result": {"workspaces": self.workspaces}})
        if args[:2] == ["workspace", "create"]:
            n = len(self.workspaces) + 1
            ws = {"workspace_id": f"w{n}", "label": args[args.index("--label") + 1]}
            self.workspaces.append(ws)
            pane = {"pane_id": f"w{n}:p1", "workspace_id": ws["workspace_id"]}
            self.panes.append(pane)
            return ok({"result": {"workspace": ws, "root_pane": pane}})
        if args[:2] == ["pane", "list"]:
            panes = self.panes
            if "--workspace" in args:
                panes = [p for p in panes if p["workspace_id"] == args[args.index("--workspace") + 1]]
            return ok({"result": {"panes": panes}})
        if args[:2] == ["pane", "split"]:
            ws_id = args[2].split(":")[0]
            n = sum(1 for p in self.panes if p["workspace_id"] == ws_id) + 1
            pane = {"pane_id": f"{ws_id}:p{n}", "workspace_id": ws_id}
            self.panes.append(pane)
            return ok({"result": {"pane": pane}})
        if args[:2] == ["pane", "rename"]:
            next(p for p in self.panes if p["pane_id"] == args[2])["label"] = args[3]
            return ok({"result": {}})
        if args[:2] == ["pane", "run"]:
            self.ran.append((args[2], args[3]))
            return ok()
        if args[:2] == ["pane", "read"]:
            return ok(text="line1\nline2")
        if args[:2] == ["pane", "process-info"]:
            pane_id = args[args.index("--pane") + 1]
            return ok({"result": {"process_info": {"shell_pid": self.shell_pid(pane_id)}}})
        raise AssertionError(f"unexpected herdr call {args}")

    def shell_pid(self, pane_id):
        return 1000 + [p["pane_id"] for p in self.panes].index(pane_id)

    def is_busy(self, pid):
        """A pane is busy once something was run in it, unless it was 'rebooted' back to an idle shell."""
        pane_id = self.panes[pid - 1000]["pane_id"]
        return any(p == pane_id for p, _ in self.ran) and pane_id not in self.idle

    def spawn(self, argv):
        self.spawned.append(list(argv))
        self.running = True

    def client(self):
        return cockpit.Herdr(exe="herdr.exe", runner=self.runner, spawn=self.spawn)


@pytest.fixture(autouse=True)
def _fake_process_table(monkeypatch, request):
    """Route the OS child-process check to whichever FakeHerdr the test builds."""
    holder = {}
    original_init = FakeHerdr.__init__

    def init(self, *a, **k):
        original_init(self, *a, **k)
        holder["fake"] = self

    monkeypatch.setattr(FakeHerdr, "__init__", init)
    monkeypatch.setattr(cockpit, "_has_child_process", lambda pid: holder["fake"].is_busy(pid))


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_run_puts_each_job_in_its_own_labelled_pane():
    fake = FakeHerdr()
    h = fake.client()
    p1 = cockpit.run("render-ep-12", ["python", "render.py", "--ep", "12"], herdr=h)
    p2 = cockpit.run("review-ep-07", "python review.py", herdr=h)

    assert p1 == "w1:p1" and p2 == "w1:p2"
    assert fake.workspaces == [{"workspace_id": "w1", "label": "jobs"}]
    assert [p.get("label") for p in fake.panes] == ["render-ep-12", "review-ep-07"]
    assert fake.ran[1] == ("w1:p2", "python review.py")
    assert "render.py" in fake.ran[0][1]


def test_run_refuses_a_live_label_unless_reuse():
    fake = FakeHerdr()
    h = fake.client()
    cockpit.run("job", "echo 1", herdr=h)
    with pytest.raises(cockpit.HerdrError, match="already exists"):
        cockpit.run("job", "echo 2", herdr=h)
    assert cockpit.run("job", "echo 2", herdr=h, reuse=True) == "w1:p1"
    assert fake.ran[-1] == ("w1:p1", "echo 2")


def test_server_is_started_hidden_when_not_running():
    fake = FakeHerdr(running=False)
    cockpit.run("job", "echo hi", herdr=fake.client())
    assert fake.spawned == [["herdr.exe", "--session", "bitcadence", "server"]]


def test_missing_herdr_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("BITCADENCE_HERDR", "Z:/nope/herdr.exe")
    with pytest.raises(cockpit.HerdrError, match="not installed"):
        cockpit.Herdr().call("workspace", "list")


def test_pinned_missing_herdr_never_falls_back_to_path(monkeypatch):
    """A pinned herdr that is gone must not silently become a different herdr found on PATH."""
    monkeypatch.delenv("BITCADENCE_HERDR", raising=False)
    monkeypatch.setattr(cockpit.shutil, "which", lambda name: "C:/other/herdr.exe")
    h = cockpit.herdr_for(cockpit.CockpitConfig(herdr="Z:/pinned/missing/herdr.exe"))
    assert h.exe is None
    with pytest.raises(cockpit.HerdrError, match="not installed"):
        h.call("workspace", "list")
    assert cockpit.Herdr().exe == "C:/other/herdr.exe"   # nothing pinned: PATH discovery still works


def test_pinned_herdr_that_is_a_directory_counts_as_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("BITCADENCE_HERDR", raising=False)
    assert cockpit.find_herdr(str(tmp_path)) is None
    exe = tmp_path / "herdr.exe"
    exe.write_text("", encoding="utf-8")
    assert cockpit.find_herdr(str(exe)) == str(exe)


def test_status_reports_missing_herdr_instead_of_all_panes_stopped(tmp_path):
    cfg = _write(tmp_path / "cockpit.toml", '[[pane]]\nworkspace = "w"\nlabel = "a"\ncommand = "echo a"\n')
    with pytest.raises(cockpit.HerdrError, match="not installed"):
        cockpit.status(cfg, fleet_path=tmp_path / "none.toml", herdr=cockpit.Herdr(exe=None))


def test_command_line_refuses_empty_and_line_breaks():
    with pytest.raises(cockpit.HerdrError, match="empty"):
        cockpit.command_line([])
    for bad in ("a\nRemove-Item x", "a\rb"):
        with pytest.raises(cockpit.HerdrError, match="line break"):
            cockpit.command_line(["python", bad])
    with pytest.raises(cockpit.HerdrError, match="one non-empty line"):
        cockpit.run("job", "echo 1\necho 2", herdr=FakeHerdr().client())


def test_config_refuses_multi_line_pane_command():
    with pytest.raises(cockpit.CockpitConfigError, match="single line"):
        cockpit.parse_cockpit_data({"pane": [{"workspace": "w", "label": "a", "command": "echo 1\necho 2"}]})


def test_new_pane_in_workspace_with_no_panes_is_a_clear_error():
    fake = FakeHerdr()
    fake.workspaces.append({"workspace_id": "w9", "label": "jobs"})
    with pytest.raises(cockpit.HerdrError, match="no panes"):
        fake.client().new_pane("jobs", "x")


def test_real_spawn_and_runner_pass_no_window_flags_on_windows(monkeypatch):
    """The console pop-up rule: the real spawn/run paths must ask Windows for no console window."""
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen["popen"] = kwargs

    def fake_run(argv, **kwargs):
        seen["run"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cockpit.os, "name", "nt")
    monkeypatch.setattr(cockpit.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cockpit.subprocess, "run", fake_run)
    cockpit.Herdr._spawn_hidden(["herdr.exe", "server"])
    cockpit._default_runner(["herdr.exe", "status", "server"])
    assert seen["popen"]["creationflags"] & 0x08000000 and seen["popen"]["creationflags"] & 0x00000008
    assert seen["popen"]["stdin"] is subprocess.DEVNULL and seen["popen"]["stdout"] is subprocess.DEVNULL
    assert seen["run"]["creationflags"] & 0x08000000


@pytest.mark.skipif(cockpit.os.name != "nt", reason="Toolhelp32 path is Windows-only")
def test_has_child_process_real_toolhelp32(monkeypatch):
    monkeypatch.undo()   # the autouse fixture stubs _has_child_process; exercise the real one here
    child = subprocess.Popen([__import__("sys").executable, "-c", "import time; time.sleep(5)"],
                             creationflags=0x08000000)
    try:
        assert cockpit._has_child_process(cockpit.os.getpid()) is True
    finally:
        child.kill()
        child.wait()
    lone = subprocess.Popen([__import__("sys").executable, "-c", "import time; time.sleep(5)"],
                            creationflags=0x08000000)
    try:
        assert cockpit._has_child_process(lone.pid) is False
    finally:
        lone.kill()
        lone.wait()


def test_up_starts_declared_panes_once(tmp_path):
    cfg = _write(tmp_path / "cockpit.toml", """
[[pane]]
workspace = "fleet"
label = "dispatcher"
cwd = "~/projects/example-project"
command = "python dispatcher.py --watch"

[[pane]]
workspace = "fleet"
label = "review-portal"
command = "python run_review_service.py"
""")
    fake = FakeHerdr()
    first = cockpit.up(cfg, fleet_path=tmp_path / "missing-fleet.toml", herdr=fake.client())
    second = cockpit.up(cfg, fleet_path=tmp_path / "missing-fleet.toml", herdr=fake.client())

    assert [line.split(":")[0] for line in first] == ["dispatcher", "review-portal"]
    assert all("already running" in line for line in second)
    assert len(fake.ran) == 2  # nothing started twice


def test_up_reruns_commands_in_panes_restored_as_idle_shells(tmp_path):
    """herdr restores layout and labels after a reboot, but not processes: up() must notice and re-run."""
    cfg = _write(tmp_path / "cockpit.toml",
                 '[[pane]]\nworkspace = "fleet"\nlabel = "dispatcher"\ncommand = "python dispatcher.py --watch"\n')
    fake = FakeHerdr()
    cockpit.up(cfg, fleet_path=tmp_path / "none.toml", herdr=fake.client())
    fake.idle.add("w1:p1")                      # reboot: same pane and label, fresh idle shell

    lines = cockpit.up(cfg, fleet_path=tmp_path / "none.toml", herdr=fake.client())

    assert lines == ["dispatcher: restarted in its restored pane (w1:p1)"]
    assert fake.ran == [("w1:p1", "python dispatcher.py --watch")] * 2
    assert len(fake.panes) == 1                 # reused the restored pane, no duplicate


def test_fleet_pane_workers_become_agent_panes(tmp_path, monkeypatch):
    fleet_toml = _write(tmp_path / "fleet.toml", """
[workers.worker-a]
role = "codex"
instance = "worker-a"
mode = "pane"
exec = "~/.mco/bin/worker-a-run.cmd"
min_interval = 10

[workers.worker-b]
role = "grok"
instance = "worker-b"
mode = "waker"
exec = "~/.mco/bin/worker-b-run.cmd"
""")
    specs = cockpit.fleet_panes(fleet_toml)
    assert [(s.workspace, s.label) for s in specs] == [("agents", "worker-a")]
    assert "wake" in specs[0].command and "worker-a-run.cmd" in specs[0].command


def test_label_declared_twice_is_rejected(tmp_path):
    cfg = _write(tmp_path / "cockpit.toml", """
[[pane]]
workspace = "agents"
label = "worker-a"
command = "x"
""")
    fleet_toml = _write(tmp_path / "fleet.toml", """
[workers.worker-a]
role = "codex"
mode = "pane"
exec = "run.cmd"
""")
    with pytest.raises(cockpit.CockpitConfigError, match="both"):
        cockpit.declared(cfg, fleet_toml)


@pytest.mark.parametrize("bad, message", [
    ('[[pane]]\nworkspace = "a"\nlabel = "x"\n', "command"),
    ('[[pane]]\nworkspace = "a"\nlabel = "x"\ncommand = "c"\nshell = "sh"\n', "unsupported"),
    ('[[pane]]\nworkspace = "a"\nlabel = "x"\ncommand = "c"\n[[pane]]\nworkspace = "a"\nlabel = "x"\ncommand = "d"\n',
     "duplicate"),
])
def test_bad_cockpit_config(tmp_path, bad, message):
    with pytest.raises(cockpit.CockpitConfigError, match=message):
        cockpit.load_cockpit(_write(tmp_path / "cockpit.toml", bad))


def test_status_lists_declared_and_one_off_panes(tmp_path):
    cfg = _write(tmp_path / "cockpit.toml", '[[pane]]\nworkspace = "fleet"\nlabel = "dispatcher"\ncommand = "c"\n')
    fake = FakeHerdr()
    h = fake.client()
    cockpit.run("render-ep-12", "echo", herdr=h)
    rows = cockpit.status(cfg, fleet_path=tmp_path / "none.toml", herdr=h)
    assert {(r["label"], r["running"]) for r in rows} == {("dispatcher", False), ("render-ep-12", True)}


def test_windows_command_line_quotes_for_powershell(monkeypatch):
    monkeypatch.setattr(cockpit.os, "name", "nt")
    assert cockpit.command_line(["C:/a b/python.exe", "it's.py"]) == "& 'C:/a b/python.exe' 'it''s.py'"


def test_herdr_location_comes_from_config_not_code(tmp_path, monkeypatch):
    exe = tmp_path / "herdr.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.delenv("BITCADENCE_HERDR", raising=False)
    cfg = _write(tmp_path / "cockpit.toml", f'herdr = "{exe.as_posix()}"\n')
    assert cockpit.find_herdr(cockpit.load_cockpit(cfg).herdr) == exe.as_posix()
    monkeypatch.setattr(cockpit.shutil, "which", lambda name: None)
    assert cockpit.find_herdr(None) is None          # no hidden machine-specific fallback
