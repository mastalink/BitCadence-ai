"""Review fan-out: send one piece of work to independent reviewers from different model families.

The fleet already has reviewers that are good at this (Codex/GPT, the Antigravity/Gemini pool, Grok, Claude).
Instead of each pipeline hand-rolling who reviews what, :func:`fanout` posts one board job per reviewer and
enforces the two rules every pipeline keeps re-implementing:

* **no self-review** - a family that built the work (``exclude_families``) is never asked to judge it;
* **two families** - at least ``min_families`` distinct families must be asked, or nothing is posted.

Each job asks for the same machine-readable verdict, so whatever consumes the results can compare them:
``{"verdict": "pass" | "fail", "findings": [{"where", "problem", "fix"}], "notes": "..."}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence

try:  # pragma: no cover - exercised by interpreter version
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


@dataclass(frozen=True)
class Reviewer:
    role: str
    instance: Optional[str]
    family: str


# The generic default panel, in preference order. Every entry addresses a ROLE (``instance=None``: any free worker
# of that role takes it), so product code carries no deployment's instance names. A deployment pins specific
# instances in its private ``~/.mco/review_panel.toml`` (see :func:`load_panel`).
DEFAULT_PANEL: tuple[Reviewer, ...] = (
    Reviewer("codex", None, "gpt"),
    Reviewer("antigravity", None, "gemini"),
    Reviewer("grok", None, "grok"),
    Reviewer("claude", None, "claude"),
)
PANEL_CONFIG_PATH = Path.home() / ".mco" / "review_panel.toml"


def load_panel(path: Path = PANEL_CONFIG_PATH) -> tuple[Reviewer, ...]:
    """The deployment's panel from ``[[reviewer]] role / instance / family`` entries, or the generic default."""
    if not path.exists():
        return DEFAULT_PANEL
    with path.open("rb") as handle:
        try:
            raw = tomllib.load(handle).get("reviewer", [])
        except tomllib.TOMLDecodeError as exc:
            raise ReviewFanoutError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise ReviewFanoutError(f"{path}: [[reviewer]] must be an array of tables")
    panel = []
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            raise ReviewFanoutError(f"{path}: reviewer #{i + 1} must be a table")
        role, family = r.get("role"), r.get("family")
        if not (isinstance(role, str) and role and isinstance(family, str) and family):
            raise ReviewFanoutError(f"{path}: reviewer #{i + 1} needs role and family")
        panel.append(Reviewer(role, r.get("instance") or None, family))
    return tuple(panel) or DEFAULT_PANEL

VERDICT_CONTRACT = (
    "Reply with ONE JSON object and nothing else:\n"
    '{"verdict": "pass" | "fail", "findings": [{"where": "...", "problem": "...", "fix": "..."}], '
    '"notes": "what you actually inspected"}\n'
    "Fail only for a concrete problem you can point at. Never claim to have watched, listened to or opened "
    "something you did not."
)


class _Sender(Protocol):
    def send(self, to_role: str, title: str, instructions: str, to_instance: Optional[str] = None,
             **kwargs: Any) -> dict: ...


class ReviewFanoutError(ValueError):
    pass


def pick_reviewers(panel: Sequence[Reviewer] = DEFAULT_PANEL, exclude_families: Iterable[str] = (),
                   per_family: int = 1, min_families: int = 2) -> list[Reviewer]:
    """One reviewer per family (first in panel order), skipping excluded families."""
    excluded = {f.lower() for f in exclude_families}
    chosen: list[Reviewer] = []
    counts: dict[str, int] = {}
    for r in panel:
        fam = r.family.lower()
        if fam in excluded or counts.get(fam, 0) >= per_family:
            continue
        chosen.append(r)
        counts[fam] = counts.get(fam, 0) + 1
    if len(counts) < min_families:
        raise ReviewFanoutError(
            f"only {len(counts)} reviewer famil{'y' if len(counts) == 1 else 'ies'} left after excluding "
            f"{sorted(excluded) or 'none'}; need {min_families}")
    return chosen


def review_prompt(subject: str, rubric: str, files: Sequence[str]) -> str:
    listing = "\n".join(f"- {f}" for f in files) or "- (no files; the subject describes the work)"
    return (f"REVIEW: {subject}\n\nFiles to review:\n{listing}\n\nPass bar:\n{rubric.strip()}\n\n"
            f"{VERDICT_CONTRACT}\n")


def fanout(client: _Sender, subject: str, rubric: str, files: Sequence[str] = (),
           panel: Optional[Sequence[Reviewer]] = None, exclude_families: Iterable[str] = (),
           min_families: int = 2, priority: int = 0, dry_run: bool = False) -> list[dict]:
    """Post one review job per chosen reviewer. Returns ``[{reviewer, family, job_id}]``."""
    if not subject.strip() or not rubric.strip():
        raise ReviewFanoutError("subject and rubric are required")
    reviewers = pick_reviewers(load_panel() if panel is None else panel, exclude_families,
                               min_families=min_families)
    prompt = review_prompt(subject, rubric, files)
    posted = []
    for r in reviewers:
        who = r.instance or r.role
        title = f"[review] {subject} ({r.family})"
        if dry_run:
            posted.append({"reviewer": who, "family": r.family, "job_id": None})
            continue
        job = client.send(r.role, title, prompt, to_instance=r.instance, priority=priority)
        posted.append({"reviewer": who, "family": r.family, "job_id": (job or {}).get("id")})
    return posted
