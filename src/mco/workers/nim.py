"""A governed BitCadence worker backed by NVIDIA NIM.

NIM serves models behind an OpenAI-compatible API, whether the model runs on
NVIDIA's hosted API catalog (https://integrate.api.nvidia.com/v1) or in a NIM
container on your own GPUs (http://<host>:8000/v1). This worker is the same
code for both: point NIM_BASE_URL at either one.

The worker never decides on its own whether work happens. It only sees jobs
the gateway hands it, so a job created with `requires_approval` (or sent to a
gated role) reaches this worker only after a human approves it. While it
works, it streams the model's answer and calls `agent.checkpoint()` between
chunks, so a cancelled job or a lost lease stops it mid-generation instead of
letting it run to completion.

Run it:

    set MCO_GATEWAY_URL=http://127.0.0.1:18789
    set MCO_AGENT_TOKEN=mco_tok_...          (from `mco register`)
    set NIM_API_KEY=nvapi-...                (hosted catalog; optional for self-hosted NIM)
    python -m mco.workers.nim

Environment:
    NIM_BASE_URL      default https://integrate.api.nvidia.com/v1
    NIM_API_KEY       falls back to NVIDIA_API_KEY; required for the hosted catalog
    NIM_MODEL         default nvidia/nemotron-3.5-lightning-30b-a3b
    NIM_MAX_TOKENS    default 2048
    NIM_TEMPERATURE   default 0.2
    AGENT_ROLE        default "nim"
    AGENT_INSTANCE_ID default "<role>-1"
    POLL_INTERVAL     default 15 (seconds)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Iterator, List, Optional
from urllib.parse import urlparse

import httpx

from mco.sdk import BitCadenceAgent

logger = logging.getLogger("mco.workers.nim")

HOSTED_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_ROLE = "nim"

SYSTEM_PROMPT = (
    "You are the '{role}' worker in a governed BitCadence fleet, running on NVIDIA NIM. "
    "Be concrete and brief. If the task asks you to take an action in an external system, "
    "describe exactly what you would do and stop: the governance runtime decides whether it happens."
)


class NimError(RuntimeError):
    """NIM refused or failed the request. The message never contains the API key."""


def _is_hosted(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "").endswith("api.nvidia.com")


class NimClient:
    """Minimal OpenAI-compatible client for a NIM endpoint (hosted or self-hosted)."""

    def __init__(
        self,
        base_url: str = HOSTED_BASE_URL,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        timeout: float = 120.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = (base_url or HOSTED_BASE_URL).rstrip("/")
        if _is_hosted(self.base_url) and not api_key:
            raise ValueError("NIM_API_KEY (or NVIDIA_API_KEY) is required for NVIDIA's hosted API catalog")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        kwargs: dict = {"base_url": self.base_url, "headers": headers, "timeout": self.timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    @staticmethod
    def _error(resp: httpx.Response) -> NimError:
        try:
            body = resp.json()
            detail = body.get("detail") or (body.get("error") or {}).get("message") or body.get("title") or ""
        except Exception:
            detail = (resp.text or "")[:200]
        return NimError(f"NIM HTTP {resp.status_code}: {detail}".strip())

    def list_models(self) -> List[str]:
        with self._client() as c:
            resp = c.get("/models")
        if resp.status_code != 200:
            raise self._error(resp)
        return [m.get("id", "") for m in resp.json().get("data", [])]

    def stream_chat(self, messages: List[dict]) -> Iterator[str]:
        """Yield the answer text as it streams. Reasoning tokens are not yielded."""
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        with self._client() as c:
            with c.stream("POST", "/chat/completions", json=body,
                          headers={"Accept": "text/event-stream"}) as resp:
                if resp.status_code != 200:
                    resp.read()
                    raise self._error(resp)
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield text


def make_handler(agent: BitCadenceAgent, nim: NimClient,
                 role: str = DEFAULT_ROLE) -> Callable[[dict, str], tuple]:
    """Build the job handler: stream from NIM, checkpointing between chunks."""

    def handle(job: dict, prompt: str):
        # Confirm we still own the job before spending any GPU time.
        agent.checkpoint()
        parts: List[str] = []
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(role=role)},
            {"role": "user", "content": prompt},
        ]
        for text in nim.stream_chat(messages):
            parts.append(text)
            agent.checkpoint()
        result = "".join(parts).strip()
        if not result:
            raise NimError(f"NIM model {nim.model} returned an empty answer")
        return result, {
            "summary": result[:240],
            "worker": agent.instance_id,
            "model": nim.model,
            "endpoint": "nvidia-hosted" if _is_hosted(nim.base_url) else "self-hosted-nim",
            "decisions": [],
            "gotchas": [],
        }

    return handle


def build_agent(
    nim: NimClient,
    role: str = DEFAULT_ROLE,
    instance_id: str = "",
    token: str = "",
    gateway: str = "",
    poll_interval: float = 15.0,
    client=None,
) -> BitCadenceAgent:
    agent = BitCadenceAgent(role=role, instance_id=instance_id or f"{role}-1", token=token,
                            gateway=gateway, poll_interval=poll_interval, client=client)
    agent.handler(make_handler(agent, nim, role))
    return agent


def nim_from_env() -> NimClient:
    return NimClient(
        base_url=os.environ.get("NIM_BASE_URL") or HOSTED_BASE_URL,
        api_key=os.environ.get("NIM_API_KEY") or os.environ.get("NVIDIA_API_KEY") or "",
        model=os.environ.get("NIM_MODEL") or DEFAULT_MODEL,
        max_tokens=int(os.environ.get("NIM_MAX_TOKENS") or 2048),
        temperature=float(os.environ.get("NIM_TEMPERATURE") or 0.2),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    nim = nim_from_env()
    role = os.environ.get("AGENT_ROLE") or DEFAULT_ROLE
    agent = build_agent(
        nim,
        role=role,
        instance_id=os.environ.get("AGENT_INSTANCE_ID") or "",
        token=os.environ.get("MCO_AGENT_TOKEN") or "",
        gateway=os.environ.get("MCO_GATEWAY_URL") or "",
        poll_interval=float(os.environ.get("POLL_INTERVAL") or 15),
    )
    logger.info("NIM worker %s (%s) -> %s, model %s", agent.instance_id, role, nim.base_url, nim.model)
    agent.run()


if __name__ == "__main__":
    main()
