"""NIM worker: streaming, checkpointing between chunks, error reporting, config."""

import json

import httpx
import pytest

from mco.workers import nim as nim_mod
from mco.workers.nim import NimClient, NimError, build_agent

from tests.test_sdk import FakeClient

JOB = {"id": "j1", "title": "Summarize", "input_payload": {"prompt": "say hi"}}


def _sse(*chunks, done=True) -> bytes:
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps(c))
        lines.append("")
    if done:
        lines.append("data: [DONE]")
        lines.append("")
    return "\n".join(lines).encode()


def _delta(content=None, reasoning=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "delta": delta}]}


class Recorder:
    def __init__(self, status=200, body=b"", json_body=None):
        self.requests = []
        self.status, self.body, self.json_body = status, body, json_body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.json_body is not None:
            return httpx.Response(self.status, json=self.json_body)
        return httpx.Response(self.status, content=self.body,
                              headers={"content-type": "text/event-stream"})


def _nim(recorder, **kw) -> NimClient:
    kw.setdefault("api_key", "nvapi-test")
    return NimClient(transport=httpx.MockTransport(recorder), **kw)


class CheckpointClient(FakeClient):
    """FakeClient whose renew() counts calls and can be made to fail."""

    def __init__(self, fail_after=None, **kw):
        super().__init__(**kw)
        self.renews = 0
        self.fail_after = fail_after

    def renew(self, task_id, estimated_seconds=None):
        self.renews += 1
        if self.fail_after is not None and self.renews > self.fail_after:
            raise RuntimeError("lease lost")
        return {"success": True}


class TestNimClient:
    def test_streams_answer_and_skips_reasoning(self):
        rec = Recorder(body=_sse(_delta(reasoning="thinking..."), _delta("Hello"), _delta(", world")))
        assert list(_nim(rec).stream_chat([{"role": "user", "content": "hi"}])) == ["Hello", ", world"]

    def test_request_shape_and_auth(self):
        rec = Recorder(body=_sse(_delta("ok")))
        list(_nim(rec, model="nvidia/test-model").stream_chat([{"role": "user", "content": "hi"}]))
        req = rec.requests[0]
        assert req.url == "https://integrate.api.nvidia.com/v1/chat/completions"
        assert req.headers["Authorization"] == "Bearer nvapi-test"
        body = json.loads(req.content)
        assert body["model"] == "nvidia/test-model" and body["stream"] is True

    def test_http_error_is_reported_without_the_key(self):
        rec = Recorder(status=410, json_body={"title": "Gone", "detail": "model reached end of life"})
        with pytest.raises(NimError) as exc:
            list(_nim(rec).stream_chat([{"role": "user", "content": "hi"}]))
        assert "410" in str(exc.value) and "end of life" in str(exc.value)
        assert "nvapi-test" not in str(exc.value)

    def test_hosted_catalog_requires_a_key(self):
        with pytest.raises(ValueError):
            NimClient(api_key="")

    def test_self_hosted_nim_needs_no_key(self):
        rec = Recorder(body=_sse(_delta("local")))
        client = NimClient(base_url="http://gpu-box:8000/v1", api_key="",
                           transport=httpx.MockTransport(rec))
        assert list(client.stream_chat([{"role": "user", "content": "hi"}])) == ["local"]
        assert "Authorization" not in rec.requests[0].headers
        assert str(rec.requests[0].url) == "http://gpu-box:8000/v1/chat/completions"

    def test_list_models(self):
        rec = Recorder(json_body={"data": [{"id": "nvidia/a"}, {"id": "nvidia/b"}]})
        assert _nim(rec).list_models() == ["nvidia/a", "nvidia/b"]


class TestWorker:
    def test_completes_job_with_answer_and_handoff(self):
        client = CheckpointClient()
        rec = Recorder(body=_sse(_delta("Hi "), _delta("there")))
        agent = build_agent(_nim(rec, model="nvidia/m"), client=client)
        assert agent.process_job(JOB) is True
        done = next(c for c in client.calls if c[0] == "complete")
        assert done[2] == "Hi there"
        assert done[3]["model"] == "nvidia/m"
        assert done[3]["endpoint"] == "nvidia-hosted"

    def test_prompt_reaches_nim_with_role_system_prompt(self):
        client = CheckpointClient()
        rec = Recorder(body=_sse(_delta("ok")))
        agent = build_agent(_nim(rec), role="nim-review", client=client)
        agent.process_job(JOB)
        messages = json.loads(rec.requests[0].content)["messages"]
        assert "nim-review" in messages[0]["content"]
        assert messages[1]["content"].endswith("say hi")

    def test_checkpoints_before_and_between_chunks(self):
        client = CheckpointClient()
        rec = Recorder(body=_sse(_delta("a"), _delta("b"), _delta("c")))
        build_agent(_nim(rec), client=client).process_job(JOB)
        assert client.renews >= 4  # once before the request, once per chunk

    def test_lost_lease_stops_generation_and_withholds_result(self):
        client = CheckpointClient(fail_after=2)
        rec = Recorder(body=_sse(*[_delta(str(i)) for i in range(10)]))
        agent = build_agent(_nim(rec), client=client)
        assert agent.process_job(JOB) is True
        assert not any(c[0] == "complete" for c in client.calls)
        assert not any(c[0] == "fail" for c in client.calls)

    def test_nim_error_fails_the_job_for_governance(self):
        client = CheckpointClient()
        rec = Recorder(status=401, json_body={"detail": "Unauthorized"})
        build_agent(_nim(rec), client=client).process_job(JOB)
        failed = next(c for c in client.calls if c[0] == "fail")
        assert "401" in failed[2]

    def test_empty_answer_fails_instead_of_completing(self):
        client = CheckpointClient()
        rec = Recorder(body=_sse(_delta(reasoning="only thoughts")))
        build_agent(_nim(rec), client=client).process_job(JOB)
        assert any(c[0] == "fail" for c in client.calls)
        assert not any(c[0] == "complete" for c in client.calls)


class TestEnvConfig:
    def test_falls_back_to_nvidia_api_key_and_defaults(self, monkeypatch):
        for k in ("NIM_BASE_URL", "NIM_API_KEY", "NIM_MODEL", "NIM_MAX_TOKENS", "NIM_TEMPERATURE"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-env")
        client = nim_mod.nim_from_env()
        assert client.api_key == "nvapi-env"
        assert client.model == nim_mod.DEFAULT_MODEL
        assert client.base_url == nim_mod.HOSTED_BASE_URL
