"""Live demo: an NVIDIA NIM worker that cannot act until a human approves.

What it proves, end to end over real HTTP with real token auth:
  1. An operator creates a job that requires approval. It parks at the gate.
  2. The NIM worker polls and sees nothing - the gate holds the work back.
  3. A human approver approves it.
  4. The worker leases it, streams the answer from NVIDIA NIM, and completes it.
  5. The job's append-only audit trail shows every step and who took it.

It runs a private gateway on 127.0.0.1 with a throwaway database, so it never
touches a live fleet board. Needs NIM_API_KEY or NVIDIA_API_KEY in the
environment (or NIM_BASE_URL pointing at a self-hosted NIM container).

    python scripts/demo_nim_gated.py [--model nvidia/...] [--out report.md]
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP = Path(tempfile.mkdtemp(prefix="bitcadence-nim-demo-"))
os.environ["MCO_LEASE_STORE_DIR"] = str(TMP / "leases")
os.environ["MCO_RESULT_SPOOL_DIR"] = str(TMP / "results")
# Keep NIM the only outbound call: no Jev triage, whatever the local config says.
os.environ["MCO_JEV_MODE"] = "disabled"

import mco.notifiers.ntfy as ntfy_mod  # noqa: E402
import mco.orchestrator.routes as routes_mod  # noqa: E402
from mco.localstore import LocalStore  # noqa: E402
from mco.orchestrator.client import GatewayClient  # noqa: E402
from mco.orchestrator.routes import agents_router, router  # noqa: E402
from mco.workers.nim import build_agent, nim_from_env  # noqa: E402

TOKENS = {
    # instance_id: (role, token) - throwaway, valid only for this demo's database
    "ops-dispatcher": ("dispatcher", "demo-dispatcher-token"),
    "human-approver": ("human", "demo-approver-token"),
    "nim-1": ("nim", "demo-nim-worker-token"),
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_gateway(db: LocalStore) -> str:
    routes_mod.get_db_client = lambda: db
    ntfy_mod.notify = lambda *a, **k: True  # keep the demo offline except for NIM
    for instance_id, (role, token) in TOKENS.items():
        db.table("agent_registry").upsert({
            "instance_id": instance_id, "role": role, "status": "online",
            "auth_token_hash": hashlib.sha256(token.encode()).hexdigest(),
        }).execute()
    app = FastAPI()
    app.include_router(router)
    app.include_router(agents_router)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(base + "/api/jobs", timeout=1)
            return base
        except httpx.HTTPError:
            time.sleep(0.1)
    raise SystemExit("gateway did not start")


def as_(base: str, instance_id: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=30,
                        headers={"Authorization": f"Bearer {TOKENS[instance_id][1]}"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    if args.model:
        os.environ["NIM_MODEL"] = args.model

    nim = nim_from_env()
    log: list[str] = []

    def say(line: str = "") -> None:
        print(line, flush=True)
        log.append(line)

    db = LocalStore(TMP / "demo.db")
    base = start_gateway(db)
    say(f"Gateway: {base} (throwaway database in a temp folder)")
    say(f"NIM: {nim.base_url}  model: {nim.model}")
    say()

    prompt = ("In three bullet points, list what a compliance officer should check before "
              "letting an AI agent push a change to a production system.")
    with as_(base, "ops-dispatcher") as ops:
        r = ops.post("/api/jobs", json={
            "title": "Draft a pre-deploy checklist for AI agents",
            "target_agent_role": "nim",
            "requires_approval": True,
            "input_payload": {"prompt": prompt},
        })
        r.raise_for_status()
        job = r.json()["job"]
    job_id = job["id"]
    say(f"1. ops-dispatcher created job {job_id}: status = {job['status']}")
    assert job["status"] == "needs_approval", job

    worker_client = GatewayClient(base_url=base, token=TOKENS["nim-1"][1], role="nim", instance_id="nim-1")
    agent = build_agent(nim, role="nim", instance_id="nim-1", client=worker_client)
    ran = agent.run_once()
    say(f"2. NIM worker polled before approval: jobs run = {ran}")
    assert ran == 0

    with as_(base, "human-approver") as human:
        r = human.post(f"/api/jobs/{job_id}/approve")
        r.raise_for_status()
        say(f"3. human-approver approved: status = {r.json()['job']['status']}")

    started = time.monotonic()
    ran = agent.run_once()
    elapsed = time.monotonic() - started
    say(f"4. NIM worker polled after approval: jobs run = {ran} ({elapsed:.1f}s including the NIM call)")

    with as_(base, "human-approver") as human:
        jobs = human.get("/api/jobs").json()
        record = next(j for j in jobs if j["id"] == job_id)
        events = human.get(f"/api/jobs/{job_id}/events").json()

    say(f"   final status = {record['status']}")
    if record["status"] != "completed":
        say(f"   error: {record.get('error_message')}")
        return 1
    output = record.get("output_payload") or {}
    handoff = output.get("handoff") or {}
    say(f"   model (from handoff) = {handoff.get('model')}  endpoint = {handoff.get('endpoint')}")
    say()
    say("NIM answer:")
    say(str(output.get("result", "")).strip())
    say()
    say("5. Audit trail (append-only):")
    for e in events:
        say(f"   {e.get('created_at', '')}  {e.get('event', ''):<22}  by {e.get('actor_id', '')}")

    if args.out:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        Path(args.out).write_text(
            f"# BitCadence + NVIDIA NIM: gated run ({stamp})\n\n"
            "Output of `python scripts/demo_nim_gated.py`, unedited.\n\n```text\n"
            + "\n".join(log) + "\n```\n",
            encoding="utf-8",
        )
        print(f"\nReport written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
