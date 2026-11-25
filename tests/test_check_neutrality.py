import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_neutrality", Path(__file__).resolve().parents[1] / "scripts" / "check_neutrality.py")
cn = importlib.util.module_from_spec(_SPEC)
sys.modules["check_neutrality"] = cn      # dataclasses resolve their module through sys.modules
_SPEC.loader.exec_module(cn)


@pytest.mark.parametrize("line, kind", [
    (r"exec = 'C:\Users\jdoe\.mco\bin\run.cmd'", "user-path"),
    ("open /Users/jdoe/projects/x", "user-path"),
    ("cd /home/jdoe/work", "user-path"),
    ("gateway at 192.168.40.12:8080", "private-ip"),
    ("ssh 10.0.0.5", "private-ip"),
    ("node 100.101.5.6 over the overlay", "private-ip"),
    ("contact jane.doe@gmail.com", "email"),
])
def test_each_generic_class_is_caught(line, kind):
    assert kind in cn.scan_line(line, [])


@pytest.mark.parametrize("line", [
    r"C:\Users\<user>\.mco\fleet.toml",
    "/home/runner/work/repo",
    "bind 127.0.0.1:18789",
    "docs use 192.0.2.10 and 203.0.113.7",
    "mail ops@example.com",
    "security@bitcadence.ai and admin@corp.example and a@svc.local",
    "noreply 12345+bot@users.noreply.github.com",
    "version 1.2.3.4 is not an address in the private ranges",
])
def test_placeholders_and_public_values_pass(line):
    assert cn.scan_line(line, []) == []


def test_private_denylist_terms_are_whole_word_and_case_insensitive():
    deny = cn.compile_denylist(["primarybox", "secret-project"])
    assert cn.scan_line("runs on PrimaryBox tonight", deny) == ["deployment-term"]
    assert cn.scan_line("the secret-project plugin", deny) == ["deployment-term"]
    assert cn.scan_line("primaryboxes are plural, not the term", deny) == []


def test_suppression_marker():
    assert cn.scan_line("ssh 10.0.0.5  # neutrality: allow (documented lab range)", []) == []


def test_denylist_file_ignores_comments_and_blanks(tmp_path):
    f = tmp_path / "denylist.txt"
    f.write_text("# private terms\nprimarybox\n\nsecret-project  # project\n", encoding="utf-8")
    assert cn.load_denylist(f) == ["primarybox", "secret-project"]
    assert cn.load_denylist(tmp_path / "missing.txt") == []


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_diff_mode_only_sees_added_lines(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "old.md").write_text("legacy 10.0.0.9\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "switch", "-qc", "feature")
    (repo / "new.md").write_text("clean line\nleak 192.168.1.20\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "feature")

    findings = cn.check(cn.added_lines("main", repo), [])
    assert [(f.path, f.line, f.kind) for f in findings] == [("new.md", 2, "private-ip")]
    assert cn.main(["--diff", "main", "--repo", str(repo), "--denylist", str(tmp_path / "none.txt")]) == 1
    all_findings = cn.check(cn.tracked_lines(repo), [])
    assert {f.path for f in all_findings} == {"old.md", "new.md"}
