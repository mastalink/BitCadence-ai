
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from typer.testing import CliRunner

from mco.cli import app
from mco.handoff import Deployment, GitHub, Harness, PullRequest, load_program
from mco.reviews import Reviewer


PROGRAM = """
[program]
name = "test"
namespace = "8b3c1f0e-5d0a-4c7e-9b1e-2f6a4d8c9e10"
builder = { role = "builder", family = "gpt" }
merge_gate = { role = "gate", requires_approval = true }
default_reviewers = ["alpha", "beta"]
min_families = 2
max_fix_rounds = 3
commands = { py = "python -m pytest" }

[[wp]]
id = "A"
title = "First"
repo = "bitcadence"
risk = "medium"
acceptance = ["{py} tests/test_api.py"]
evidence = ["readout"]

[[wp]]
id = "B"
title = "Second"
repo = "bitcadence"
depends = ["A"]
"""

FIRST_SHA = "a" * 40


class FakeClient:
    def __init__(self):
        self.jobs = {}
        self.posts = []
        self.drop_after_create = None

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def send(self, role, title, instructions, *, job_id, depends_on=(), extra_payload=None,
             to_instance=None, **kwargs):
        assert job_id not in self.jobs
        assert all(self.jobs[dep]["status"] == "completed" for dep in depends_on)
        job = {
            "id": job_id, "target_agent_role": role, "target_agent_id": to_instance,
            "title": title, "prompt": instructions,
            "status": "needs_approval" if kwargs.get("requires_approval") else "pending",
            "depends_on": list(depends_on), "input_payload": extra_payload or {}, **kwargs,
        }
        self.jobs[job_id] = job
        self.posts.append(job_id)
        if self.drop_after_create == job_id:
            self.drop_after_create = None
            raise RuntimeError("lost response")
        return {"success": True, "job": job}

    def finish(self, job_id, result=None, status="completed"):
        self.jobs[job_id].update(status=status, output_payload={"result": json.dumps(result)})


class FakeGitHub:
    def __init__(self):
        self.pr = PullRequest(17, FIRST_SHA, "main", "OPEN", False, True)

    def pull_request(self, repo, number):
        assert (repo, number) == ("example-labs/demo", 17)
        return self.pr


def setup_harness(tmp_path, activation=False):
    path = tmp_path / "packages.toml"
    path.write_text(PROGRAM.replace('risk = "medium"', 'risk = "medium"\nactivation = true')
                    if activation else PROGRAM, encoding="utf-8")
    deployment = Deployment(
        {"builder": "example-builder"},
        tuple(Reviewer(f"role-{family}", f"example-{family}", family)
              for family in ("alpha", "beta", "gamma")),
        {"bitcadence": "example-labs/demo"},
    )
    return Harness(load_program(path), deployment, FakeClient(), FakeGitHub())


def start_reviews(harness):
    harness.post()
    harness.client.finish(harness.job_id("A", "build"), {"pr": 17, "head": FIRST_SHA})
    harness.advance()


def review_ids(harness, round_number=0):
    suffix = f"-fix-{round_number}" if round_number else ""
    return [harness.job_id("A", f"review-{family}{suffix}") for family in ("alpha", "beta", "gamma")]


def pass_reviews(h):
    for job in review_ids(h):
        h.client.finish(job, {"verdict": "pass", "findings": [], "notes": "inspected"})


def test_build_is_idempotent_and_next_package_requires_verified_merge(tmp_path):
    h = setup_harness(tmp_path)
    build = h.job_id("A", "build")
    assert build == str(uuid5(UUID("8b3c1f0e-5d0a-4c7e-9b1e-2f6a4d8c9e10"), "A:build"))
    start_reviews(h)
    h.post()
    assert h.client.posts == [build, *review_ids(h)]
    pass_reviews(h)
    h.advance()
    gate = h.job_id("A", "merge")
    assert h.client.jobs[gate]["requires_approval"] is True
    assert "gh pr merge" in h.client.jobs[gate]["prompt"]
    h.client.finish(gate)
    h.advance()
    assert h.job_id("B", "build") not in h.client.jobs
    h.client.jobs[gate]["approved_by"] = "example-approver"
    h.advance()
    assert h.job_id("B", "build") not in h.client.jobs
    h.github.pr = replace(h.github.pr, state="MERGED")
    h.advance()
    assert h.client.jobs[h.job_id("B", "build")]["depends_on"] == [gate]


def test_failure_rounds_escalate_after_three_fixes(tmp_path):
    h = setup_harness(tmp_path)
    start_reviews(h)
    for round_number in range(4):
        first, second, third = review_ids(h, round_number)
        h.client.finish(first, {"verdict": "pass", "findings": []})
        h.client.finish(second, {"verdict": "pass", "findings": [], "notes": "inspected"})
        h.advance()
        assert h.job_id("A", f"fix-{round_number + 1}") not in h.client.jobs
        h.client.finish(third, {"verdict": "fail", "findings": [{"problem": "regression"}]})
        h.advance()
        if round_number < 3:
            fix_id = h.job_id("A", f"fix-{round_number + 1}")
            assert first in h.client.jobs[fix_id]["depends_on"]
            assert "regression" in h.client.jobs[fix_id]["prompt"]
            sha = str(round_number + 1) * 40
            h.github.pr = replace(h.github.pr, head=sha)
            h.client.finish(fix_id, {"pr": 17, "head": sha})
            h.advance()
    escalation = h.client.jobs[h.job_id("A", "escalate")]
    assert "regression" in escalation["prompt"]
    assert escalation["requires_approval"] is True
    assert h.job_id("A", "fix-4") not in h.client.jobs
    assert h.job_id("A", "merge") not in h.client.jobs


def test_red_or_changed_pr_cannot_get_gate(tmp_path):
    h = setup_harness(tmp_path)
    start_reviews(h)
    pass_reviews(h)
    h.github.pr = replace(h.github.pr, green=False)
    assert "checks" in h.advance()["A"]
    assert h.job_id("A", "merge") not in h.client.jobs
    h.github.pr = replace(h.github.pr, green=True, head="9" * 40)
    assert "head" in h.advance()["A"]
    assert h.job_id("A", "merge") not in h.client.jobs


def test_restart_resumes_partially_posted_review_round(tmp_path):
    h = setup_harness(tmp_path)
    h.post()
    h.client.finish(h.job_id("A", "build"), {"pr": 17, "head": FIRST_SHA})
    h.client.drop_after_create = review_ids(h)[0]
    with pytest.raises(RuntimeError, match="lost response"):
        h.advance()
    resumed = Harness(h.program, h.deployment, h.client, h.github)
    resumed.advance()
    resumed.advance()
    assert h.client.posts == [h.job_id("A", "build"), *review_ids(h)]


def test_status_is_read_only_and_wrong_reviewer_cannot_supply_quorum(tmp_path):
    h = setup_harness(tmp_path)
    assert h.status()["A"] == "ready to post"
    assert h.client.posts == []
    start_reviews(h)
    pass_reviews(h)
    assert h.status()["A"] == "ready for owner approval"
    assert h.job_id("A", "merge") not in h.client.jobs
    h.client.jobs[review_ids(h)[0]]["target_agent_id"] = "example-builder"
    assert "does not match" in h.advance()["A"]
    assert h.job_id("A", "merge") not in h.client.jobs


def test_activation_gate_requires_evidence_and_is_for_owner_handoff(tmp_path):
    h = setup_harness(tmp_path, activation=True)
    start_reviews(h)
    pass_reviews(h)
    assert "evidence" in h.advance()["A"]
    gate = h.job_id("A", "merge")
    assert gate not in h.client.jobs
    h.client.finish(h.job_id("A", "build"), {"pr": 17, "head": FIRST_SHA, "evidence": ["synthetic readout"]})
    h.advance()
    assert h.client.jobs[gate]["requires_approval"] is True
    assert "synthetic readout" in h.client.jobs[gate]["prompt"]
    assert "gh pr merge" not in h.client.jobs[gate]["prompt"]


def test_post_dry_run_uses_real_plan_without_private_config(tmp_path):
    result = CliRunner().invoke(app, [
        "handoff", "post", "--program", str(Path("docs/plan/packages.toml")),
        "--config", str(tmp_path / "missing"), "--dry-run",
    ])
    assert result.exit_code == 0, result.output
    assert all(package in result.output for package in ("N-01", "H-00", "R0-01"))
    assert "N-02" not in result.output


def test_github_requires_a_visible_green_check(monkeypatch):
    import subprocess

    payload = {"number": 17, "headRefOid": FIRST_SHA, "baseRefName": "main",
               "isDraft": False, "state": "OPEN", "statusCheckRollup": []}

    def run(args, **kwargs):
        assert args[:6] == ["gh", "pr", "view", "17", "--repo", "example-labs/demo"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    gh = GitHub()
    assert not gh.pull_request("example-labs/demo", 17).green
    payload["statusCheckRollup"] = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "StatusContext", "state": "FAILURE"},
    ]
    assert not gh.pull_request("example-labs/demo", 17).green
    payload["statusCheckRollup"][1]["state"] = "SUCCESS"
    assert gh.pull_request("example-labs/demo", 17).green


@pytest.mark.parametrize("outcome", ["failed", "malformed", "silent"])
def test_no_review_verdict_escalates_without_creating_a_fix(tmp_path, outcome):
    from datetime import datetime, timedelta, timezone
    h = setup_harness(tmp_path)
    start_reviews(h)
    first, second, third = review_ids(h)
    for review in (second, third):
        h.client.finish(review, {"verdict": "pass", "findings": []})
    if outcome == "failed":
        h.client.finish(first, status="failed")
    elif outcome == "malformed":
        h.client.jobs[first].update(status="completed", output_payload={"result": "invalid"})
    else:
        created = datetime.now(timezone.utc) - timedelta(days=2)
        h.client.jobs[first].update(created_at=created.isoformat())
    assert "escalate" in h.status()["A"]
    assert h.advance()["A"] == "escalated"
    escalation = h.client.jobs[h.job_id("A", "escalate")]
    assert escalation["requires_approval"]
    assert "alpha" in escalation["prompt"]
    assert h.job_id("A", "fix-1") not in h.client.jobs
    h.advance()
    assert h.client.posts.count(escalation["id"]) == 1


def test_string_output_and_pass_without_findings_can_reach_gate(tmp_path):
    h = setup_harness(tmp_path)
    start_reviews(h)
    for review in review_ids(h):
        h.client.jobs[review].update(status="completed", output_payload='{"verdict":"pass"}')
    assert h.advance()["A"] == "merge approval posted"


def test_watch_retries_a_transient_failure(monkeypatch, tmp_path):
    from mco import cli, handoff
    import time
    h = setup_harness(tmp_path)
    attempts = []

    def advance():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("temporary gateway failure")
        return {"A": "build posted"}

    def sleep(_):
        if len(attempts) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(h, "advance", advance)
    monkeypatch.setattr(handoff, "Harness", lambda *args: h)
    monkeypatch.setattr(handoff, "load_deployment", lambda *args: h.deployment)
    monkeypatch.setattr(cli, "_gateway_client", lambda: h.client)
    monkeypatch.setattr(time, "sleep", sleep)
    assert cli._run_handoff("watch", tmp_path / "packages.toml", None) is None
    assert len(attempts) == 2


def test_invalid_package_does_not_stop_independent_packages(tmp_path, monkeypatch):
    h = setup_harness(tmp_path)
    h.program.packages["B"] = replace(h.program.packages["B"], depends=())
    original = h._panel

    def panel(package):
        if package.id == "A":
            raise ValueError("reviewer setup is missing")
        return original(package)

    monkeypatch.setattr(h, "_panel", panel)
    result = h.advance()
    assert "reviewer setup is missing" in result["A"]
    assert result["B"] == "build posted"
