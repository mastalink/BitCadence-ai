import pytest

from mco import reviews


class FakeClient:
    def __init__(self):
        self.sent = []

    def send(self, to_role, title, instructions, to_instance=None, **kwargs):
        self.sent.append({"role": to_role, "instance": to_instance, "title": title, "prompt": instructions,
                          **kwargs})
        return {"id": f"job-{len(self.sent)}"}


def test_one_reviewer_per_family_in_panel_order():
    picked = reviews.pick_reviewers()
    assert [(r.role, r.family) for r in picked] == [
        ("codex", "gpt"), ("antigravity", "gemini"), ("grok", "grok"), ("claude", "claude")]


def test_builder_family_never_reviews_its_own_work():
    client = FakeClient()
    posted = reviews.fanout(client, "Character sheet", "style bar", files=["assets/sheet.png"],
                            exclude_families=["claude", "grok"])
    assert [p["family"] for p in posted] == ["gpt", "gemini"]
    assert all(s["role"] != "claude" for s in client.sent)
    assert client.sent[0]["role"] == "codex" and client.sent[1]["role"] == "antigravity"
    assert "assets/sheet.png" in client.sent[0]["prompt"] and '"verdict"' in client.sent[0]["prompt"]
    assert posted[0]["job_id"] == "job-1"


def test_refuses_when_fewer_than_two_families_remain():
    with pytest.raises(reviews.ReviewFanoutError, match="need 2"):
        reviews.fanout(FakeClient(), "x", "bar", exclude_families=["gpt", "grok", "claude"])


def test_dry_run_posts_nothing():
    client = FakeClient()
    posted = reviews.fanout(client, "x", "bar", dry_run=True)
    assert client.sent == [] and all(p["job_id"] is None for p in posted)


def test_subject_and_rubric_required():
    with pytest.raises(reviews.ReviewFanoutError):
        reviews.fanout(FakeClient(), " ", "bar")


@pytest.fixture(autouse=True)
def _no_private_panel(monkeypatch, tmp_path):
    """Tests never read the developer's private ~/.mco/review_panel.toml."""
    monkeypatch.setattr(reviews, "PANEL_CONFIG_PATH", tmp_path / "absent.toml")
    monkeypatch.setattr(reviews, "load_panel", lambda path=None: reviews.DEFAULT_PANEL)


def test_load_panel_reads_deployment_config(tmp_path):
    cfg = tmp_path / "review_panel.toml"
    cfg.write_text('[[reviewer]]\nrole = "codex"\ninstance = "worker-a"\nfamily = "gpt"\n'
                   '[[reviewer]]\nrole = "antigravity"\nfamily = "gemini"\n', encoding="utf-8")
    import importlib
    fresh = importlib.reload(reviews)
    panel = fresh.load_panel(cfg)
    assert panel == (fresh.Reviewer("codex", "worker-a", "gpt"), fresh.Reviewer("antigravity", None, "gemini"))
    assert fresh.load_panel(tmp_path / "missing.toml") == fresh.DEFAULT_PANEL


@pytest.mark.parametrize("text, match", [
    ("[[reviewer]\nrole = ", "Invalid TOML"),
    ('reviewer = "codex"\n', "array of tables"),
    ("reviewer = [1, 2]\n", "must be a table"),
])
def test_load_panel_rejects_bad_config_clearly(tmp_path, text, match):
    cfg = tmp_path / "review_panel.toml"
    cfg.write_text(text, encoding="utf-8")
    import importlib
    fresh = importlib.reload(reviews)
    with pytest.raises(fresh.ReviewFanoutError, match=match):
        fresh.load_panel(cfg)


def test_default_panel_names_no_instances():
    assert all(r.instance is None for r in reviews.DEFAULT_PANEL)
