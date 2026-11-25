"""Reconcile the build DAG against durable board jobs and GitHub PRs."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid5

from mco import reviews

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib


DEFAULT_CONFIG = Path.home() / ".mco" / "deployment" / "handoff.toml"
_STOPPED = {"failed", "cancelled", "rejected", "halted"}
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"(?:[a-zA-Z0-9.-]+/)?[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+")


def _toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


@dataclass(frozen=True)
class Package:
    id: str
    title: str
    repo: str
    depends: tuple[str, ...]
    reviewers: tuple[str, ...]
    risk: str
    activation: bool
    scope: tuple[str, ...]
    non_goals: tuple[str, ...]
    acceptance: tuple[str, ...]
    evidence: tuple[str, ...]
    fault_injection: tuple[str, ...]
    rollback: str


@dataclass(frozen=True)
class Program:
    name: str
    namespace: UUID
    builder_role: str
    builder_family: str
    gate_role: str
    min_families: int
    max_fix_rounds: int
    packages: dict[str, Package]

    def initial_ready(self) -> list[Package]:
        return [package for package in self.packages.values() if not package.depends]


def load_program(path: Path) -> Program:
    raw = _toml(path)
    settings = raw["program"]
    commands = settings.get("commands", {})
    packages = {}
    for item in raw["wp"]:
        package = Package(
            id=item["id"], title=item["title"], repo=item["repo"],
            depends=tuple(item.get("depends", ())),
            reviewers=tuple(f.lower() for f in (item.get("reviewers") or settings["default_reviewers"])),
            risk=item.get("risk", "low"), activation=item.get("activation", False),
            scope=tuple(item.get("scope", ())), non_goals=tuple(item.get("non_goals", ())),
            acceptance=tuple(re.sub(r"^\{(\w+)\}", lambda m: commands[m[1]], command)
                             for command in item.get("acceptance", ())),
            evidence=tuple(item.get("evidence", ())), fault_injection=tuple(item.get("fault_injection", ())),
            rollback=item.get("rollback", ""),
        )
        if package.id in packages:
            raise ValueError(f"duplicate package ID: {package.id}")
        if package.risk not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"{package.id}: unknown risk {package.risk}")
        packages[package.id] = package
    for package in packages.values():
        missing = set(package.depends) - packages.keys()
        if missing:
            raise ValueError(f"{package.id}: unknown dependencies {', '.join(sorted(missing))}")
    # Detect cycles up front, before any jobs can be posted.
    TopologicalSorter({wp.id: wp.depends for wp in packages.values()}).prepare()
    minimum = settings["min_families"]
    rounds = settings["max_fix_rounds"]
    if type(minimum) is not int or minimum < 2 or type(rounds) is not int or rounds < 0:
        raise ValueError("min_families must be at least 2 and max_fix_rounds must be nonnegative")
    if settings["merge_gate"].get("requires_approval") is not True:
        raise ValueError("program.merge_gate.requires_approval must be true")
    return Program(settings["name"], UUID(settings["namespace"]), settings["builder"]["role"],
                   settings["builder"]["family"].lower(), settings["merge_gate"]["role"],
                   minimum, rounds, packages)


@dataclass(frozen=True)
class Deployment:
    builders: dict[str, str]
    reviewers: tuple[reviews.Reviewer, ...]
    repositories: dict[str, str]
    bases: dict[str, str] = field(default_factory=dict)

    def builder(self, role: str) -> str:
        instance = self.builders.get(role)
        if not isinstance(instance, str) or not instance.strip():
            raise ValueError(f"no pinned builder instance for role {role}")
        return instance

    def repository(self, name: str) -> str:
        slug = self.repositories.get(name)
        if not isinstance(slug, str) or not _REPO.fullmatch(slug):
            raise ValueError(f"no valid GitHub slug for repository alias {name}")
        return slug

    def base(self, name: str) -> str:
        return self.bases.get(name, "main")


def load_deployment(path: Path) -> Deployment:
    raw = _toml(path)
    entries = _toml(path.with_name("repos.toml"))["repos"]
    repositories = {name: entry["github"] for name, entry in entries.items()}
    bases = {name: entry.get("base", "main") for name, entry in entries.items()}
    return Deployment(
        raw["builders"],
        tuple(reviews.Reviewer(entry["role"], entry["instance"], family.lower())
              for family, entry in raw["reviewers"].items()),
        repositories, bases,
    )


@dataclass(frozen=True)
class PullRequest:
    number: int
    head: str
    base: str
    state: str
    draft: bool
    green: bool


class GitHub:
    def pull_request(self, repo: str, number: int) -> PullRequest:
        try:
            response = subprocess.run(
                ["gh", "pr", "view", str(number), "--repo", repo, "--json",
                 "number,headRefOid,baseRefName,isDraft,state,statusCheckRollup"],
                check=True, capture_output=True, encoding="utf-8", timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise RuntimeError(f"could not read PR #{number} in {repo}: {detail.strip()}") from exc
        data = json.loads(response.stdout)
        outcomes = [_check_outcome(check) for check in (data["statusCheckRollup"] or [])]
        green = "SUCCESS" in outcomes and all(
            outcome in {"SUCCESS", "SKIPPED", "NEUTRAL"} for outcome in outcomes
        )
        return PullRequest(data["number"], data["headRefOid"], data["baseRefName"],
                           data["state"], data["isDraft"], green)


def _check_outcome(check: dict) -> Optional[str]:
    if check.get("__typename") == "StatusContext":
        return check.get("state")
    if check.get("__typename") == "CheckRun" and check.get("status") == "COMPLETED":
        return check.get("conclusion")
    return None


@dataclass(frozen=True)
class Revision:
    pr: int
    head: str
    evidence: tuple[str, ...] = ()

    @classmethod
    def parse(cls, data: dict) -> Optional[Revision]:
        number, head = data.get("pr"), data.get("head")
        if type(number) is not int or number < 1 or not isinstance(head, str) or not _SHA.fullmatch(head):
            return None
        evidence = data.get("evidence", [])
        if not isinstance(evidence, list) or any(not isinstance(line, str) or not line.strip() for line in evidence):
            return None
        return cls(number, head, tuple(evidence))


def _result(job: dict) -> dict:
    data = (job.get("output_payload") or {}).get("result")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- None listed"


class Harness:
    def __init__(self, program: Program, deployment: Deployment, client, github: GitHub):
        self.program = program
        self.deployment = deployment
        self.client = client
        self.github = github
        self._jobs: dict[str, Optional[dict]] = {}
        self._prs: dict[tuple[str, int], PullRequest] = {}

    def job_id(self, package: str, phase: str) -> str:
        return str(uuid5(self.program.namespace, f"{package}:{phase}"))

    def _job(self, job_id: str) -> Optional[dict]:
        if job_id not in self._jobs:
            job = self.client.get_job(job_id)
            if job is not None and job.get("id") != job_id:
                raise RuntimeError(f"board returned a different job for {job_id}")
            self._jobs[job_id] = job
        return self._jobs[job_id]

    def _pr(self, repo: str, number: int) -> PullRequest:
        key = (repo, number)
        if key not in self._prs:
            self._prs[key] = self.github.pull_request(repo, number)
        return self._prs[key]

    def _send(self, wp: Package, phase: str, role: str, prompt: str, depends_on: list[str], **kwargs) -> None:
        job_id = self.job_id(wp.id, phase)
        if self._job(job_id) is not None:
            return
        if any((self._job(dep) or {}).get("status") != "completed" for dep in depends_on):
            raise RuntimeError(f"{wp.id} {phase} has a missing or incomplete dependency")
        response = self.client.send(
            role, f"[{self.program.name}] {wp.id} {phase}: {wp.title}", prompt,
            job_id=job_id, depends_on=depends_on, **kwargs,
        )
        job = (response or {}).get("job")
        if not isinstance(job, dict) or job.get("id") != job_id:
            raise RuntimeError(f"board did not confirm the requested job ID for {wp.id} {phase}")
        self._jobs[job_id] = job

    def _panel(self, wp: Package) -> list[reviews.Reviewer]:
        builder = self.deployment.builder(self.program.builder_role)
        configured = {r.family: r for r in self.deployment.reviewers if r.family != self.program.builder_family}
        requested = list(dict.fromkeys(f for f in wp.reviewers if f != self.program.builder_family))
        risk_minimum = 3 if wp.risk != "low" or len(set(wp.reviewers)) >= 3 else 2
        minimum = max(self.program.min_families, len(requested), risk_minimum)
        for family in requested:
            if family not in configured:
                raise ValueError(f"{wp.id}: no pinned reviewer for requested family {family}")
        families = list(dict.fromkeys([*requested, *configured]))[:minimum]
        panel = [configured[family] for family in families]
        if len(panel) < minimum:
            raise ValueError(f"{wp.id}: need {minimum} distinct non-builder reviewer families")
        instances = [r.instance for r in panel]
        if any(not isinstance(r.instance, str) or not r.instance.strip() or not r.role.strip() for r in panel):
            raise ValueError(f"{wp.id}: every reviewer needs a role and pinned instance")
        if builder in instances or len(set(instances)) != len(instances):
            raise ValueError(f"{wp.id}: review requires independent pinned instances")
        return panel

    def _gate_state(self, wp: Package, gate: dict) -> str:
        status = gate["status"]
        if status != "completed":
            return f"{'activation' if wp.activation else 'merge'} gate {status}"
        if not gate.get("requires_approval") or not gate.get("approved_by"):
            return "blocked: missing recorded approval"
        evidence = (gate.get("input_payload") or {}).get("handoff") or {}
        revision = Revision.parse(evidence)
        if revision is None:
            return "blocked: missing gate PR revision"
        repo = self.deployment.repository(wp.repo)
        base = self.deployment.base(wp.repo)
        if evidence.get("repo") != repo or evidence.get("base") != base:
            return "blocked: configured repository or base differs from approved PR"
        pr = self._pr(repo, revision.pr)
        if pr.state == "MERGED" and pr.head == revision.head and pr.base == base:
            return "merged"
        return "awaiting verified PR merge"

    def _post_build(self, wp: Package, write: bool) -> str:
        deps = [self.job_id(dep, "merge") for dep in wp.depends]
        for name, dep_id in zip(wp.depends, deps):
            gate = self._job(dep_id)
            if gate is None or self._gate_state(self.program.packages[name], gate) != "merged":
                return f"waiting for {name} to merge"
        if not write:
            return "ready to post"
        repo = self.deployment.repository(wp.repo)
        self._panel(wp)
        self._send(wp, "build", self.program.builder_role, self._build_prompt(wp, repo), deps,
                   to_instance=self.deployment.builder(self.program.builder_role))
        return "build posted"

    def _build_prompt(self, wp: Package, repo: str) -> str:
        activation = ("Prepare evidence for owner sign-off. Leave production activation to the owner."
                      if wp.activation else "")
        return (
            f"Build {wp.id}: {wp.title} in {repo}. Use an isolated branch/worktree and open a PR "
            f"against {self.deployment.base(wp.repo)}. Leave the PR open for review. {activation}\n\n"
            f"Scope\n{_bullets(wp.scope)}\n\nNon-goals\n{_bullets(wp.non_goals)}\n\n"
            f"Acceptance\n{_bullets(wp.acceptance)}\n\nEvidence\n{_bullets(wp.evidence)}\n\n"
            f"Fault injection\n{_bullets(wp.fault_injection)}\n\nRollback\n{wp.rollback}\n\n"
            'Complete with strict JSON {"pr": 123, "head": "<full PR head SHA>", '
            '"evidence": ["test results and evidence references for this revision"]}.'
        )

    def _review_round(self, wp: Package, panel: list[reviews.Reviewer], round_number: int,
                      parent_id: str, revision: Revision, repo: str,
                      write: bool) -> tuple[str, list[str], list[dict]]:
        suffix = f"-fix-{round_number}" if round_number else ""
        ids = {r.family: self.job_id(wp.id, f"review-{r.family}{suffix}") for r in panel}
        jobs = {family: self._job(job_id) for family, job_id in ids.items()}
        subject = f"{wp.id} {repo} PR #{revision.pr} at {revision.head}"
        for reviewer in panel:
            job = jobs[reviewer.family]
            if job is not None and (
                job.get("target_agent_role") != reviewer.role
                or job.get("target_agent_id") != reviewer.instance
                or job.get("depends_on") != [parent_id]
                or job.get("title") != f"[review] {subject} ({reviewer.family})"
            ):
                return f"blocked: existing {reviewer.family} review does not match this round", [], []
        missing = [r for r in panel if jobs[r.family] is None]
        if missing:
            if write:
                reviews.fanout(
                    self.client, subject,
                    f"Review precisely PR head {revision.head} for {wp.title}. "
                    "If the head changed, report that instead of reviewing another commit.\n"
                    f"Acceptance\n{_bullets(wp.acceptance)}\n"
                    f"Evidence\n{_bullets(revision.evidence)}",
                    panel=missing, min_families=len(missing),
                    depends_on=[parent_id], job_ids={r.family: ids[r.family] for r in missing},
                )
            return ("reviews posted" if write else "ready to post reviews"), [], []

        completed, failures = [], []
        for family, job in jobs.items():
            status = job["status"]
            if status == "completed":
                completed.append(ids[family])
                result = _result(job)
                if result.get("verdict") != "pass" or result.get("findings") != []:
                    failures.append({"family": family, "result": result or "invalid review result"})
            elif status in _STOPPED:
                failures.append({"family": family, "status": status, "error": job.get("error_message")})
            else:
                return "awaiting reviews", [], []
        return "", completed, failures

    def _next_round(self, wp: Package, round_number: int, depends: list[str], revision: Revision,
                    repo: str, failures: list[dict], write: bool) -> str:
        findings = json.dumps(failures, ensure_ascii=False, indent=2)
        subject = f"{wp.id} {repo} PR #{revision.pr} (reviewed head {revision.head})"
        if round_number >= self.program.max_fix_rounds:
            if write:
                self._send(wp, "escalate", self.program.gate_role,
                           f"Manual triage required for {subject}. Review fix rounds are exhausted. "
                           f"Leave the PR open for the owner.\n\nReview outcomes\n{findings}",
                           depends, requires_approval=True)
            return "escalated" if write else "ready to escalate"
        next_round = round_number + 1
        if write:
            self._send(
                wp, f"fix-{next_round}", self.program.builder_role,
                f"Fix the review findings for {subject}. Update the same PR and rerun acceptance checks.\n"
                f"{_bullets(wp.acceptance)}\n\nReview outcomes\n{findings}\n\n"
                'Complete with strict JSON {"pr": 123, "head": "<new full PR head SHA>", '
                '"evidence": ["test results and evidence references for this revision"]}.',
                depends, to_instance=self.deployment.builder(self.program.builder_role),
            )
        return f"fix round {next_round} {'posted' if write else 'ready'}"

    def _post_gate(self, wp: Package, revision: Revision, repo: str, depends: list[str], write: bool) -> str:
        if wp.activation and not revision.evidence:
            return "blocked: activation evidence is required"
        if not write:
            return "ready for owner approval"
        subject = f"{wp.id} {repo} PR #{revision.pr} at {revision.head}"
        base = self.deployment.base(wp.repo)
        if wp.activation:
            prompt = (f"Owner activation decision for {subject}. Assess the attached evidence. "
                      "After approval, record the owner's handoff for manual activation. "
                      "This job grants no worker authority to activate or merge.\n\n"
                      f"Required evidence\n{_bullets(wp.evidence)}")
        else:
            prompt = (
                f"After owner approval, verify {subject} is open, targets {base}, "
                "is not a draft, and still has passing checks for the exact reviewed head. "
                "Stop if the head moved, any check is red or pending, or branch protection blocks the merge. "
                "Do not bypass branch protection, auto-merge, or continue on a different commit. "
                f"Merge using gh pr merge {revision.pr} --repo {repo} --match-head-commit {revision.head} "
                "with the repository's allowed merge method, then report the merge."
            )
        self._send(
            wp, "merge", self.program.gate_role,
            f"{prompt}\n\nReviewed evidence\n{_bullets(revision.evidence)}",
            depends, requires_approval=True,
            extra_payload={"handoff": {"pr": revision.pr, "head": revision.head, "repo": repo, "base": base}},
        )
        return "activation approval posted" if wp.activation else "merge approval posted"

    def _advance_package(self, wp: Package, build: dict, write: bool) -> str:
        gate = self._job(self.job_id(wp.id, "merge"))
        if gate is not None:
            return self._gate_state(wp, gate)
        if self._job(self.job_id(wp.id, "escalate")) is not None:
            return "escalated"
        repo = self.deployment.repository(wp.repo)
        panel = self._panel(wp)
        previous = None
        parent = build
        for round_number in range(self.program.max_fix_rounds + 1):
            if parent["status"] != "completed":
                return f"{'build' if not round_number else f'fix round {round_number}'} {parent['status']}"
            revision = Revision.parse(_result(parent))
            if revision is None:
                return "blocked: build or fix must report PR number, full head SHA, and valid evidence"
            if previous and (revision.pr != previous.pr or revision.head == previous.head):
                return "blocked: fix must update the same PR with a different head"
            next_fix = (self._job(self.job_id(wp.id, f"fix-{round_number + 1}"))
                        if round_number < self.program.max_fix_rounds else None)
            if next_fix is not None:
                previous, parent = revision, next_fix
                continue
            pr = self._pr(repo, revision.pr)
            if pr.head != revision.head or pr.base != self.deployment.base(wp.repo):
                return "blocked: PR head or base differs from the reported revision"
            if pr.state != "OPEN" or pr.draft:
                return "blocked: PR must be open and ready for review"
            status, completed, failures = self._review_round(wp, panel, round_number, parent["id"], revision, repo, write)
            if status:
                return status
            depends = [parent["id"], *completed]
            if failures:
                return self._next_round(wp, round_number, depends, revision, repo, failures, write)
            if not pr.green:
                return "waiting for green PR checks"
            return self._post_gate(wp, revision, repo, depends, write)
        raise RuntimeError(f"{wp.id}: could not reconcile fix rounds")

    def _reconcile(self, write: bool, builds_only: bool) -> dict[str, str]:
        self._jobs.clear()
        self._prs.clear()
        statuses = {}
        for wp in self.program.packages.values():
            build = self._job(self.job_id(wp.id, "build"))
            if build is None:
                statuses[wp.id] = self._post_build(wp, write)
            elif builds_only:
                statuses[wp.id] = f"build already posted ({build['status']})"
            else:
                statuses[wp.id] = self._advance_package(wp, build, write)
        return statuses

    def post(self, dry_run: bool = False) -> dict[str, str]:
        return self._reconcile(write=not dry_run, builds_only=True)

    def advance(self) -> dict[str, str]:
        return self._reconcile(write=True, builds_only=False)

    def status(self) -> dict[str, str]:
        return self._reconcile(write=False, builds_only=False)
