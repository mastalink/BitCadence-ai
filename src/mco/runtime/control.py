"""``/v1/runtime`` on top of the agentd loopback control API, and the client the CLI uses to call it."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
import httpx
from pydantic import BaseModel

from mco.agentd import control as agentd_control
from mco.agentd import tokens as agentd_tokens

from .config import RollbackUnavailable

if TYPE_CHECKING:  # pragma: no cover
    from .core import Runtime


class MaintenanceRequest(BaseModel):
    enabled: bool


def default_control_port() -> int:
    resolve = getattr(agentd_control, "control_port", None)
    return int(resolve() if resolve is not None else agentd_control.CONTROL_PORT)


def create_runtime_app(runtime: "Runtime") -> FastAPI:
    @contextmanager
    def mutation() -> Iterator[None]:
        with runtime._state_lock:
            if runtime._stop.is_set():
                raise HTTPException(status_code=409, detail="runtime is stopping")
            yield

    app = agentd_control.create_control_app(
        runtime.supervisor,
        runtime.manager,
        runtime.config,  # type: ignore[arg-type]  # same surface as FleetWatcher
        runtime.logs,
        token_store=runtime.token_store,
        mutation=mutation,
    )
    # Exactly the authentication the agentd routes use: no second, weaker door.
    protected = next(
        route.dependencies
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/v1/status"
    )

    @app.get("/v1/runtime", dependencies=protected)
    def get_runtime() -> dict[str, object]:
        return runtime.document()

    @app.post("/v1/runtime/stop", dependencies=protected)
    def stop_runtime() -> dict[str, object]:
        runtime.request_stop("control API")
        return {"stopping": True}

    @app.post("/v1/runtime/maintenance", dependencies=protected)
    def set_maintenance(request: MaintenanceRequest) -> dict[str, object]:
        with mutation():
            runtime.set_maintenance(request.enabled)
            return runtime.document()

    @app.post("/v1/runtime/rollback", dependencies=protected)
    def rollback() -> dict[str, object]:
        with mutation():
            try:
                runtime.config.rollback()
            except RollbackUnavailable as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return runtime.document()

    return app


class ControlError(RuntimeError):
    """The running runtime could not be reached, or refused the request."""


def call_runtime(
    method: str,
    path: str,
    *,
    port: int,
    token: str,
    json: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    base = f"http://{agentd_control.CONTROL_HOST}:{port}"
    try:
        with httpx.Client(base_url=base, timeout=30.0, trust_env=False, transport=transport) as client:
            sign = getattr(agentd_tokens, "sign_control_request", None)
            if sign is not None:
                # Prove knowledge of the token without sending it to whatever holds the port.
                challenge = client.get("/v1/auth-challenge").raise_for_status().json()["challenge"]
                proof = sign(token, challenge, method, path.encode("ascii"))
                authorization = f"Agentd-HMAC {challenge}:{proof}"
            else:
                authorization = f"Bearer {token}"
            response = client.request(method, path, json=json, headers={"Authorization": authorization})
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise ControlError(f"could not reach the runtime on {base}: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise ControlError(f"the runtime refused the request ({response.status_code}): {detail}")
    return response.json()
