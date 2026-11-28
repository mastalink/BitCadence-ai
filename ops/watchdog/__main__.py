"""One-shot external watchdog, suitable for scheduling on a separate host."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx

from mco.runtime.bootstrap import RESTART_COUNT, write_bootstrap_status
from mco.runtime.paths import HEARTBEAT_FILE, STALE_AFTER_SECONDS

from .monitor import Observation, check


def run_check(config_path: Path) -> int:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        runtime_dir = config["runtime_dir"]
        transport = config["transport"]
        if not isinstance(runtime_dir, str) or not runtime_dir:
            raise ValueError
        if transport == "webhook":
            webhook_url = config["webhook_url"]
            if not isinstance(webhook_url, str):
                raise ValueError
            url = urlsplit(webhook_url)
            if url.scheme != "https" or not url.hostname:
                raise ValueError
        elif transport != "stdout":
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError):
        print("Invalid watchdog config. Set runtime_dir and select stdout or an HTTPS webhook.", file=sys.stderr)
        return 2

    result = check(config_path.parent / runtime_dir)
    payload = asdict(result)
    print(json.dumps(payload))
    if result.alert and transport == "webhook":
        try:
            httpx.post(webhook_url, json=payload, timeout=10).raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL):
            print("Watchdog alert delivery failed.", file=sys.stderr)
            return 2
    return int(result.alert is not None)


def report(label: str, result: Observation) -> None:
    line = f"{label}: state={result.state} alert={result.alert or 'none'}"
    if result.restarts is not None:
        line += f" restarts={result.restarts}"
    if result.alert == "heartbeat-stale" and result.age is not None:
        line += f" age={result.age:g}s"
    print(line)


def drill() -> int:
    now = 1000.0
    with TemporaryDirectory(prefix="watchdog-drill-") as directory:
        runtime_dir = Path(directory)
        heartbeat = runtime_dir / HEARTBEAT_FILE
        heartbeat.write_text(json.dumps({"state": "stopped", "timestamp": now - 600}), encoding="utf-8")
        stopped = check(runtime_dir, now=now)

        for attempt in range(RESTART_COUNT + 1):
            timestamp = now - (RESTART_COUNT - attempt) * 60
            heartbeat.write_text(json.dumps({"state": "crashed", "timestamp": timestamp}), encoding="utf-8")
            if attempt < RESTART_COUNT:
                write_bootstrap_status(runtime_dir, "retrying", timestamp, attempt + 1)
                if check(runtime_dir, now=timestamp).alert:
                    return 1
            else:
                write_bootstrap_status(runtime_dir, "exhausted", timestamp, attempt)
        failed = check(runtime_dir, now=now)

        now += 600
        heartbeat.write_text(json.dumps({
            "state": "running", "timestamp": now - STALE_AFTER_SECONDS - 1,
        }), encoding="utf-8")
        stale = check(runtime_dir, now=now)

    report("deliberate stop", stopped)
    report(f"{RESTART_COUNT + 1} crashes", failed)
    report("stale heartbeat", stale)
    return int((stopped.alert, failed.alert, stale.alert) != (None, "recovery-failed", "heartbeat-stale"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="Evaluate the runtime and deliver an alert if warranted.")
    check_parser.add_argument("--config", type=Path, required=True, help="Private JSON deployment config.")
    drill_parser = commands.add_parser("drill", help="Run three synthetic fault scenarios.")
    drill_parser.add_argument("--dry-run", required=True, action="store_true", help="Use temporary fixtures only.")
    args = parser.parse_args()
    if args.command == "drill":
        return drill()
    return run_check(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
