"""``bitcadence runtime run | stop | status | maintenance | rollback``."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any, Optional

import typer


runtime_app = typer.Typer(
    no_args_is_help=True,
)

@runtime_app.callback()
def runtime_group() -> None:
    """The background runtime that owns the gateway, the scheduler and the workers."""


# States in which a runtime process is there to be talked to.
LIVE_STATES = {"running", "quiesced", "maintenance", "stale"}


def _paths() -> Any:
    from .paths import RuntimePaths

    return RuntimePaths()


def _observed() -> tuple[Any, dict[str, Any] | None, str]:
    from .paths import observed_state, read_heartbeat

    paths = _paths()
    beat = read_heartbeat(paths)
    return paths, beat, observed_state(beat)


def _call(method: str, path: str, beat: dict[str, Any], body: Any = None) -> dict[str, Any]:
    from mco.agentd.tokens import AgentdTokenStore

    from .control import call_runtime, default_control_port

    home = _paths().home
    store = AgentdTokenStore(home / "agentd.token", home / "agentd.logs.token")
    port = int(beat.get("control_port") or default_control_port())
    return call_runtime(method, path, port=port, token=store.control_token(), json=body)


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


@runtime_app.command("run")
def run(
    gateway: bool = typer.Option(True, help="Run the gateway as a child of the runtime."),
    scheduler: bool = typer.Option(True, help="Run the scheduler as a child of the runtime."),
    gateway_port: int = typer.Option(18789, help="Loopback port for the supervised gateway."),
    control_port: int = typer.Option(0, help="Loopback port for the control API (0: this user's default)."),
) -> None:
    """Run the runtime in the foreground. Exit 0 only after a deliberate stop; non-zero means it crashed."""
    from .core import Runtime
    from .supervision import default_services

    runtime = Runtime(
        services=default_services(gateway=gateway, scheduler=scheduler, gateway_port=gateway_port),
        control_port=control_port or None,
    )
    raise typer.Exit(code=runtime.run())


@runtime_app.command("install-bootstrap")
def install_bootstrap(
    xml: Optional[Path] = typer.Option(None, "--xml", help="Output path for the UTF-16 Scheduled Task XML."),
) -> None:
    """Register the current user's Windows sign-in task and export its XML as UTF-16."""
    from .bootstrap import install_bootstrap as install

    try:
        path = install(_paths(), xml)
    except (OSError, RuntimeError) as exc:
        _fail(str(exc))
    typer.echo(f"Bootstrap registered. Task XML: {path}")


@runtime_app.command("uninstall-bootstrap")
def uninstall_bootstrap() -> None:
    """Remove the current user's Windows sign-in task."""
    from .bootstrap import uninstall_bootstrap as uninstall

    try:
        uninstall()
    except (OSError, RuntimeError) as exc:
        _fail(str(exc))
    typer.echo("Bootstrap removed.")


@runtime_app.command("stop")
def stop(timeout: float = typer.Option(60.0, help="Seconds to wait for it to finish stopping.")) -> None:
    """Stop the runtime and its children deliberately (it exits 0 and is not restarted)."""
    paths, beat, state = _observed()
    if state not in LIVE_STATES or beat is None:
        typer.echo(f"The runtime is not running (state: {state}).")
        return
    from .control import ControlError
    from .paths import heartbeat_process_alive, read_heartbeat

    try:
        _call("POST", "/v1/runtime/stop", beat)
    except ControlError as exc:
        _fail(str(exc))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = read_heartbeat(paths)
        if (latest or {}).get("state") == "stopped" or not heartbeat_process_alive(latest):
            typer.echo("The runtime stopped.")
            return
        time.sleep(0.25)
    _fail(f"The runtime is still stopping after {timeout:g}s.")


@runtime_app.command("status")
def status(as_json: bool = typer.Option(False, "--json", help="Print the heartbeat as JSON.")) -> None:
    """Show the runtime's state. Exit 1 when it should be running and is not (crashed, dead or silent)."""
    _, beat, state = _observed()
    if as_json:
        typer.echo(json.dumps({**(beat or {}), "state": state}, indent=2))
    else:
        typer.echo(f"Runtime: {state}")
        if beat and state in LIVE_STATES:
            typer.echo(f"  pid {beat.get('pid')}, control port {beat.get('control_port')}")
            if beat.get("config_error"):
                typer.echo(f"  configuration: {beat['config_error']}")
            for name, child in sorted((beat.get("children") or {}).items()):
                typer.echo(f"  {name}: {child.get('state')} (pid {child.get('pid') or '-'})")
    if state in {"crashed", "dead", "stale"}:
        raise typer.Exit(code=1)


@runtime_app.command("maintenance")
def maintenance(action: str = typer.Argument(..., help="on or off")) -> None:
    """Maintenance mode: the runtime stays up, its children are stopped and schedules are held."""
    if action not in {"on", "off"}:
        _fail("Say 'on' or 'off'.")
    enabled = action == "on"
    paths, beat, state = _observed()
    if state in LIVE_STATES and beat is not None:
        from .control import ControlError

        try:
            _call("POST", "/v1/runtime/maintenance", beat, {"enabled": enabled})
        except ControlError as exc:
            _fail(str(exc))
        typer.echo(f"Maintenance is {action}.")
        return
    from .paths import atomic_write

    if enabled:
        atomic_write(paths.maintenance, b"maintenance\n")
    else:
        paths.maintenance.unlink(missing_ok=True)
    typer.echo(f"Maintenance is {action}; the runtime is not running, so it applies at the next start.")


@runtime_app.command("rollback")
def rollback() -> None:
    """Restore the previous last-known-good fleet configuration."""
    paths, beat, state = _observed()
    if state in LIVE_STATES and beat is not None:
        from .control import ControlError

        try:
            result = _call("POST", "/v1/runtime/rollback", beat)
        except ControlError as exc:
            _fail(str(exc))
        generation = str(result.get("config_generation", ""))
    else:
        import hashlib

        from mco.agentd.config import FleetConfigManager

        from .config import rollback_files
        from .lkg import LkgStore

        try:
            content = rollback_files(FleetConfigManager(paths.fleet), LkgStore(paths.lkg))
        except Exception as exc:
            _fail(str(exc))
        generation = hashlib.sha256(content).hexdigest()
    typer.echo(f"Rolled back to configuration generation {generation[:12]}.")
