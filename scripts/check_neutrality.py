"""Deployment-neutrality guard: keep private deployment details out of the public repository.

BitCadence product code, docs, fixtures and examples must not carry any one deployment's identity: personal user
paths, private network addresses, personal e-mail addresses, or a deployment's own host / project / account names.
Those belong in private configuration (``~/.mco/deployment/``) or private plugin repositories.

Two layers:

* **Generic patterns** (public, below): user-profile paths, private IPv4 ranges, personal e-mail addresses.
* **A private denylist** of deployment-specific terms. It is never committed: by default it is read from
  ``~/.mco/deployment/denylist.txt`` (one term per line, ``#`` comments), or from ``--denylist``.

Modes:

* ``--diff BASE`` (CI default): check only lines **added** relative to ``BASE``, so existing debt does not block
  every pull request while nothing new can slip in.
* ``--all``: check every tracked file (audit; prints a summary per class).

Suppress a deliberate, reviewed occurrence with a trailing ``neutrality: allow`` comment on that line.

Exit code: 0 clean, 1 findings, 2 usage error.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

DEFAULT_DENYLIST = Path.home() / ".mco" / "deployment" / "denylist.txt"
SUPPRESS = "neutrality: allow"
# Placeholder user names that are fine in examples.
PLACEHOLDER_USERS = {"user", "username", "you", "your-name", "yourname", "example", "runner", "me", "name",
                     "default", "public", "<user>", "%username%", "$user", "{user}", "alice", "bob", "someone",
                     "mco"}  # the product's own container/service account, not a person
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip", ".gz",
                 ".exe", ".dll", ".lock", ".svgz", ".mp4", ".mp3", ".wav"}
SKIP_PATHS = ("scripts/check_neutrality.py", "tests/test_check_neutrality.py")

USER_PATH = re.compile(
    r"(?i)(?:[a-z]:[\\/]+users[\\/]+|/users/|/home/)(?P<user>[^\\/\s'\"`<>:;,)]+)")
EMAIL = re.compile(r"(?i)\b[a-z0-9._%+-]+@(?P<domain>[a-z0-9.-]+\.[a-z]{2,})\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com",
                         "noreply.github.com", "anthropic.com", "localhost",
                         "bitcadence.ai"}            # the product's own public domain
RESERVED_TLDS = (".example", ".local", ".test", ".invalid", ".localhost")   # RFC 2606 / 6761
_CGNAT = ipaddress.ip_network("100.64.0.0/10")   # carrier-grade NAT: overlay/VPN node addresses
_DOCS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.kind}] {self.text.strip()[:160]}"


def load_denylist(path: Optional[Path]) -> list[str]:
    if not path or not path.exists():
        return []
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.split("#", 1)[0].strip()
        if term:
            terms.append(term)
    return terms


def _private_ip(text: str) -> Optional[str]:
    for m in IPV4.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(0))
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified or any(ip in n for n in _DOCS):
            continue
        if ip.is_private or ip in _CGNAT:
            return m.group(0)
    return None


def scan_line(text: str, deny: list[re.Pattern]) -> list[str]:
    """Kinds of private detail on one line (empty when clean)."""
    if SUPPRESS in text:
        return []
    kinds = []
    for m in USER_PATH.finditer(text):
        if m.group("user").lower() not in PLACEHOLDER_USERS:
            kinds.append("user-path")
            break
    if _private_ip(text):
        kinds.append("private-ip")
    for m in EMAIL.finditer(text):
        domain = m.group("domain").lower()
        if domain not in ALLOWED_EMAIL_DOMAINS and not domain.endswith(RESERVED_TLDS):
            kinds.append("email")
            break
    if any(p.search(text) for p in deny):
        kinds.append("deployment-term")
    return kinds


def compile_denylist(terms: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(rf"(?i)(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])") for t in terms]


def _skip(path: str) -> bool:
    p = path.replace("\\", "/")
    return p in SKIP_PATHS or Path(p).suffix.lower() in SKIP_SUFFIXES


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def added_lines(base: str, cwd: Path) -> Iterator[tuple[str, int, str]]:
    """(path, new line number, text) for every line added since ``base``."""
    diff = _git(["diff", "--unified=0", "--no-color", "--no-ext-diff", f"{base}...HEAD"], cwd)
    path, lineno = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++") and path:
            yield path, lineno, raw[1:]
            lineno += 1


def tracked_lines(cwd: Path) -> Iterator[tuple[str, int, str]]:
    for path in _git(["ls-files"], cwd).splitlines():
        if _skip(path):
            continue
        try:
            content = (cwd / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, text in enumerate(content.splitlines(), 1):
            yield path, i, text


def check(lines: Iterable[tuple[str, int, str]], deny: list[re.Pattern]) -> list[Finding]:
    out = []
    for path, lineno, text in lines:
        if _skip(path):
            continue
        for kind in scan_line(text, deny):
            out.append(Finding(path, lineno, kind, text))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--diff", metavar="BASE", help="check only lines added since BASE (e.g. origin/main)")
    mode.add_argument("--all", action="store_true", help="check every tracked file")
    ap.add_argument("--denylist", type=Path, default=None,
                    help=f"private term list (default {DEFAULT_DENYLIST} if it exists)")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--summary", action="store_true", help="print counts per kind instead of every finding")
    a = ap.parse_args(argv)
    deny = compile_denylist(load_denylist(a.denylist or DEFAULT_DENYLIST))
    lines = added_lines(a.diff, a.repo) if a.diff else tracked_lines(a.repo)
    findings = check(lines, deny)
    if a.summary:
        kinds: dict[str, set[str]] = {}
        for f in findings:
            kinds.setdefault(f.kind, set()).add(f.path)
        for kind, paths in sorted(kinds.items()):
            print(f"{kind}: {len(paths)} files")
    else:
        for f in findings:
            print(f.render())
    if findings:
        print(f"\n{len(findings)} neutrality finding(s). Move deployment details into private configuration "
              f"(~/.mco/deployment/) or mark a reviewed line with '{SUPPRESS}'.", file=sys.stderr)
        return 1
    print("neutrality check: clean" + ("" if deny else " (generic patterns only; no private denylist found)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
