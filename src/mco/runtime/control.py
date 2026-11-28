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
from .plugins.runner import PluginRefused

if TYPE_CHECKING:  # pragma: no cover
    from .core import Runtime


class MaintenanceRequest(BaseModel):
    enabled: bool


class PluginActionRequest(BaseModel):
    parameters: dict[str, Any]
    grant: dict[str, Any]


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
        return runtime.document(include_worker_errors=True)

    @app.get("/v1/plugins", dependencies=protected)
    def get_plugins() -> dict[str, object]:
        with runtime._state_lock:
            if runtime.plugins is None:
                raise HTTPException(status_code=503, detail="plugin runner unavailable")
            return {"audience": runtime.plugins.audience, "plugins": runtime.plugins.document()}

    @app.post("/v1/plugins/{plugin}/actions/{action}", dependencies=protected, status_code=202)
    def run_plugin_action(plugin: str, action: str, request: PluginActionRequest) -> dict[str, str]:
        with mutation():
            if runtime.plugins is None:
                raise HTTPException(status_code=503, detail="plugin runner unavailable")
            try:
                run = runtime.plugins.start_action(plugin, action, request.parameters, request.grant)
            except PluginRefused as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            return {"run": run}

    @app.get("/v1/plugins/actions/{run}", dependencies=protected)
    def get_plugin_action(run: str) -> dict:
        with runtime._state_lock:
            if runtime.plugins is None:
                raise HTTPException(status_code=503, detail="plugin runner unavailable")
            result = runtime.plugins.action_result(run)
            if result is None:
                raise HTTPException(status_code=404, detail="action run not found")
            return result

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
    timeout: float = 30.0,
) -> dict[str, Any]:
    base = f"http://{agentd_control.CONTROL_HOST}:{port}"
    try:
        with httpx.Client(base_url=base, timeout=timeout, trust_env=False, transport=transport) as client:
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
