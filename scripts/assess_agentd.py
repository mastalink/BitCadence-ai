"""R0-01: executable assessment of the agentd supervisor as the BitCadence runtime on Windows.

Runs every check in an isolated home (a temp USERPROFILE/HOME, MCO_STOP_FILE pointed inside it, its own supervisor
mutex) so it never touches a live deployment. Prints a markdown evidence table and writes JSON next to it.

    python scripts/assess_agentd.py [--out DIR]

Each check reports PASS (requirement met), GAP (requirement not met - a fix is needed) or SKIP. The verdict rule
(docs/architecture/DECISIONS.md section 5): deploy agentd if the gaps are a few small fixes; otherwise fall back to a
standalone runtime behind the same contract.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PY = sys.executable
RESULTS: list[dict] = []


def record(check: str, requirement: str, status: str, detail: str) -> None:
    RESULTS.append({"check": check, "requirement": requirement, "status": status, "detail": detail})
    print(f"[{status}] {check}: {detail}", flush=True)


def isolate(root: Path) -> None:
    os.environ["USERPROFILE"] = str(root)
    os.environ["HOME"] = str(root)
    os.environ["MCO_STOP_FILE"] = str(root / ".mco" / "STOP")
    (root / ".mco").mkdir(parents=True, exist_ok=True)


def alive(pid: int) -> bool:
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def visible_windows() -> set[tuple[int, str]]:
    user32 = ctypes.windll.user32
    out: set[tuple[int, str]] = set()
    pid = wt.DWORD()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.add((pid.value, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


# Child programs (argv for `python -c`)
CHILD_WITH_GRANDCHILD = (
    "import subprocess,sys,time;"
    "g=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
    "print('GRANDCHILD',g.pid,flush=True); time.sleep(60)")
CRASH = "import sys; sys.exit(3)"
FLOOD = "import sys\nline='x'*1000\nfor _ in range(25000): sys.stdout.write(line+'\\n')\nsys.stdout.flush()"
IGNORE_BREAK = ("import signal,time; signal.signal(signal.SIGBREAK, signal.SIG_IGN); "
                "print('READY',flush=True); time.sleep(120)")
SLEEP = "import time; time.sleep(120)"


def make_supervisor(home: Path, programs: dict[str, str]):
    """A real agentd Supervisor + WindowsAdapter whose workers run the given `python -c` programs."""
    from mco.agentd.logs import LogAggregator
    from mco.agentd.platform.windows import WindowsAdapter, WindowsSupervisorLock
    from mco.agentd.supervisor import Supervisor

    class TestSupervisor(Supervisor):
        @staticmethod
        def _argv(config):
            return [PY, "-c", programs[config.worker]]

    lock = WindowsSupervisorLock(metadata_path=home / ".mco" / "agentd.lock")
    lock.name = f"{lock.name}-assess-{uuid.uuid4().hex[:8]}"   # never contend with a real supervisor's mutex
    adapter = WindowsAdapter(pidfile=home / ".mco" / "agentd.pids", supervisor_lock=lock)
    logs = LogAggregator(home / ".mco" / "logs")
    sup = TestSupervisor(adapter, logs=logs, pidfile=home / ".mco" / "agentd.pids",
                         state_file=home / ".mco" / "agentd.state.json", cwd=home)
    return sup, logs


def cfg(name: str, mode: str = "waker", min_interval: float = 10):
    from mco.fleet import WorkerConfig
    return WorkerConfig(worker=name, role="assess", instance=name, mode=mode, exec_command="unused.cmd",
                        min_interval=min_interval, poll_interval=1800)


def grandchild_pid(logs, worker: str, timeout: float = 10) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in logs.tail(worker, 50):
            if "GRANDCHILD" in line:
                return int(line.rsplit(" ", 1)[-1])
        time.sleep(0.2)
    return None


def check_no_window_and_stop_tree(home: Path) -> None:
    import threading
    sup, logs = make_supervisor(home, {"tree": CHILD_WITH_GRANDCHILD})
    before = visible_windows()
    new: set = set()
    done = threading.Event()

    def sample() -> None:
        # Sample continuously from before the spawn: a console that flashes open and shut during child or
        # grandchild start-up would be missed by sampling only after both have started.
        while not done.is_set():
            new.update(visible_windows() - before)
            time.sleep(0.05)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        sup.reconcile({"tree": cfg("tree")})
        gpid = grandchild_pid(logs, "tree")
        time.sleep(3)
    finally:
        done.set()
        sampler.join(5)
    record("no-window", "children and grandchildren never show a window",
           "PASS" if not new and gpid else ("GAP" if new else "SKIP"),
           f"new visible windows: {sorted(c for _, c in new) or 'none'}; grandchild pid {gpid}")
    child = sup.workers["tree"].process.pid
    sup.stop("tree")
    time.sleep(1)
    record("stop-kills-tree", "stopping one worker also stops what it spawned",
           "PASS" if gpid and not alive(gpid) else "GAP",
           f"child {child} alive={alive(child)}; grandchild {gpid} alive={alive(gpid) if gpid else 'n/a'}")
    if gpid and alive(gpid):
        import psutil
        psutil.Process(gpid).kill()
    sup.shutdown()


HOST = r'''
import sys, time, json
sys.path.insert(0, {src!r})
from mco.agentd.platform.windows import WindowsAdapter
import subprocess
a = WindowsAdapter(pidfile=__import__("pathlib").Path({pidfile!r}))
a.bind_to_parent_lifetime()
p = a.spawn([{py!r}, "-c", {code!r}], __import__("pathlib").Path({cwd!r}), dict(__import__("os").environ),
            subprocess.PIPE, subprocess.DEVNULL)
line = p.stdout.readline().decode().strip()
print(json.dumps({{"child": p.pid, "line": line}}), flush=True)
time.sleep(120)
'''


def check_supervisor_death(home: Path, src: str) -> None:
    pidfile = home / ".mco" / "host.pids"
    code = HOST.format(src=src, pidfile=str(pidfile), py=PY, code=CHILD_WITH_GRANDCHILD, cwd=str(home))
    host = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE, creationflags=0x08000000)
    try:
        info = json.loads(host.stdout.readline().decode())
        child = info["child"]
        gpid = int(info["line"].split()[-1])
    finally:
        host.kill()                   # hard death: no shutdown code runs (also on a setup failure)
        host.wait(5)
    time.sleep(2)
    record("no-orphans-on-supervisor-death", "if the supervisor dies hard, its whole tree dies",
           "PASS" if not alive(child) and not alive(gpid) else "GAP",
           f"child {child} alive={alive(child)}; grandchild {gpid} alive={alive(gpid)}")
    from mco.agentd.platform.windows import WindowsAdapter
    reaped = WindowsAdapter(pidfile=pidfile).reap_orphans(pidfile)
    leftover = pidfile.read_text(encoding="utf-8").strip() if pidfile.exists() else ""
    # PASS only if the Job Object already killed everything: anything reap_orphans had to kill was a leak.
    record("restart-no-stale", "a restarted supervisor finds nothing stale to adopt or leak",
           "PASS" if not reaped and not leftover else "GAP",
           f"reaped {reaped or 'nothing'}; pidfile {'cleared' if not leftover else 'NOT cleared'}")


def check_reap_identity(home: Path) -> None:
    """A recorded pid recycled by an unrelated process with the same exe name must NOT be killed."""
    from mco.agentd.platform.windows import WindowsAdapter
    bystander = subprocess.Popen([PY, "-c", SLEEP], creationflags=0x08000000)   # not ours to kill
    pidfile = home / ".mco" / "reap.pids"
    pidfile.write_text(json.dumps({"pid": bystander.pid, "command": Path(PY).name}) + "\n", encoding="utf-8")
    killed = WindowsAdapter(pidfile=pidfile).reap_orphans(pidfile)
    time.sleep(0.5)
    survived = bystander.poll() is None
    record("reap-identity", "orphan reaping never kills an unrelated process that reused a recorded pid",
           "PASS" if survived else "GAP",
           f"bystander {bystander.pid} {'survived' if survived else 'was KILLED'} (reaped {killed})"
           + ("" if survived else "; records hold exe name only, no create time"))
    if survived:
        bystander.kill()
    bystander.wait(5)


def check_crash_loop(home: Path) -> None:
    from mco.agentd.supervisor import WorkerState
    clock = [1000.0]
    sup, _ = make_supervisor(home, {"crash": CRASH})
    sup.clock = lambda: clock[0]
    sup.reconcile({"crash": cfg("crash")})
    for _ in range(40):
        time.sleep(0.3)
        clock[0] += 70            # fake monotonic clock: skip past every backoff step
        sup.tick()                # the latch itself uses real wall-clock failure times (all within seconds)
        if sup.workers["crash"].state == WorkerState.CRASHLOOPED:
            break
    st = sup.workers["crash"]
    latched = st.state == WorkerState.CRASHLOOPED
    record("crash-loop", "a worker that keeps dying is latched as crash-looped and stops restarting",
           "PASS" if latched else "GAP",
           f"state={st.state.value} starts={st.starts} failures={len(st.failure_timestamps)}")
    if latched:
        from mco.agentd.supervisor import CRASHLOOP_COOLDOWN_SECONDS
        last = max(st.failure_timestamps)
        sup.wall_clock = lambda: last + CRASHLOOP_COOLDOWN_SECONDS - 5     # just before the cooldown ends
        sup.tick()
        held = st.state == WorkerState.CRASHLOOPED
        sup.wall_clock = lambda: last + CRASHLOOP_COOLDOWN_SECONDS + 5     # just after
        sup.tick()
        left = st.state != WorkerState.CRASHLOOPED
        record("crash-loop-cooldown", "after the cooldown a crash-looped worker gets a fresh set of attempts",
               "PASS" if held and left else "GAP",
               f"held before cooldown={held}; retried after={left} (state={st.state.value})")
    sup.shutdown()


def check_flood_and_rotation(home: Path) -> None:
    sup, logs = make_supervisor(home, {"flood": FLOOD})
    sup.reconcile({"flood": cfg("flood")})
    proc = sup.workers["flood"].process
    try:
        proc.wait(timeout=60)
        exited = True
    except Exception:
        exited = False
    logdir = home / ".mco" / "logs"

    def sizes_now() -> dict[str, int]:
        return {p.name: p.stat().st_size for p in logdir.glob("*.log*")}

    # The drain threads keep writing after the child exits: wait until the log stops growing (not a fixed sleep,
    # which on slow disks inspects a half-written log and can report a false PASS).
    sizes, stable_since, deadline = sizes_now(), time.time(), time.time() + 60
    while time.time() < deadline and time.time() - stable_since < 1.5:
        time.sleep(0.25)
        now = sizes_now()
        if now != sizes:
            sizes, stable_since = now, time.time()
    biggest = max(sizes.values(), default=0)
    total = sum(sizes.values())
    record("log-drain", "a child that floods its output never blocks", "PASS" if exited else "GAP",
           f"25 MB writer exited={exited}")
    rotated = biggest <= 10 * 1024 * 1024 + 4096
    drained = total >= 20 * 1024 * 1024     # most of the 25 MB arrived, so the size check means something
    record("log-rotation", "logs rotate at 10 MB x3 backups",
           "PASS" if rotated and drained else ("SKIP" if not drained else "GAP"),
           f"largest log {biggest / 1e6:.1f} MB of {total / 1e6:.1f} MB drained; files {sorted(sizes)}"
           + ("" if rotated else " (handler.stream.write bypasses rollover)"))
    sup.shutdown()


def check_graceful_then_kill(home: Path) -> None:
    sup, logs = make_supervisor(home, {"stubborn": IGNORE_BREAK})
    sup.reconcile({"stubborn": cfg("stubborn")})
    time.sleep(1.5)
    pid = sup.workers["stubborn"].process.pid
    t0 = time.time()
    sup.stop("stubborn")
    took = time.time() - t0
    record("graceful-then-deadline", "stop asks politely, then kills after a deadline",
           "PASS" if not alive(pid) and took < 15 else "GAP", f"stopped in {took:.1f}s; alive={alive(pid)}")
    sup.shutdown()


def check_reconcile(home: Path) -> None:
    sup, _ = make_supervisor(home, {"w": SLEEP, "v": SLEEP})
    sup.reconcile({"w": cfg("w", min_interval=10), "v": cfg("v", min_interval=10)})
    first, other = sup.workers["w"].process.pid, sup.workers["v"].process.pid
    sup.reconcile({"w": cfg("w", min_interval=20), "v": cfg("v", min_interval=10)})   # only w changed
    time.sleep(1)
    second, other_after = sup.workers["w"].process.pid, sup.workers["v"].process.pid
    restarted_changed = first != second and not alive(first)
    kept_unchanged = other == other_after and alive(other)
    sup.reconcile({"w": cfg("w", mode="off"), "v": cfg("v", min_interval=10)})
    time.sleep(1)
    off_stopped = not alive(second)
    record("reconcile", "config changes restart only what changed; mode=off stops",
           "PASS" if restarted_changed and kept_unchanged and off_stopped else "GAP",
           f"changed w: {first} -> {second} (old alive={alive(first)}); unchanged v: {other} -> {other_after}; "
           f"w off -> alive={alive(second)}")
    sup.shutdown()


def check_stop_quiesce(home: Path) -> None:
    stop = Path(os.environ["MCO_STOP_FILE"])
    stop.write_text("assessment", encoding="utf-8")
    try:
        sup, _ = make_supervisor(home, {"q": SLEEP})
        sup.reconcile({"q": cfg("q")})
        proc = sup.workers["q"].process
        record("stop-quiesce", "with STOP set nothing new is spawned", "PASS" if proc is None else "GAP",
               f"process={'none' if proc is None else proc.pid}")
        sup.shutdown()
    finally:
        stop.unlink(missing_ok=True)


def check_generic_process_spec() -> None:
    from mco.agentd.supervisor import Supervisor
    try:
        Supervisor._argv(cfg("x", mode="pane"))
        ok = True
    except Exception:
        ok = False
    record("generic-processes", "can supervise non-worker processes (gateway, scheduler, plugin processes)",
           "PASS" if ok else "GAP",
           "the supervisor accepts a non-waker/poll process spec" if ok else
           "argv is derived only from worker modes waker/poll; gateway/plugins need a generic process spec")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "bc-agentd-assessment")
    a = ap.parse_args()
    if os.name != "nt":
        print("Windows only")
        return 2
    src = str(Path(__file__).resolve().parents[1] / "src")
    sys.path.insert(0, src)
    root = Path(tempfile.mkdtemp(prefix="bc-assess-"))
    isolate(root)
    checks = [check_no_window_and_stop_tree, check_crash_loop, check_flood_and_rotation, check_graceful_then_kill,
              check_reconcile, check_stop_quiesce, check_reap_identity]
    for i, fn in enumerate(checks):
        home = root / f"c{i}"
        (home / ".mco").mkdir(parents=True)
        try:
            fn(home)
        except Exception as exc:
            record(fn.__name__, "-", "SKIP", f"{type(exc).__name__}: {exc}")
    try:
        home = root / "death"
        (home / ".mco").mkdir(parents=True)
        check_supervisor_death(home, src)
    except Exception as exc:
        record("check_supervisor_death", "-", "SKIP", f"{type(exc).__name__}: {exc}")
    check_generic_process_spec()

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "evidence.json").write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")
    lines = ["| Check | Requirement | Result | Evidence |", "|---|---|---|---|"]
    lines += [f"| {r['check']} | {r['requirement']} | **{r['status']}** | {r['detail']} |" for r in RESULTS]
    (a.out / "evidence.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    gaps = [r for r in RESULTS if r["status"] == "GAP"]
    print(f"\n{len(gaps)} gap(s); evidence in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
