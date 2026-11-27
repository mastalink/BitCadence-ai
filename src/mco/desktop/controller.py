"""Read-only client for the local BitCadence runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from mco.runtime.control import ControlError, call_runtime
from mco.runtime.paths import RuntimePaths, observed_state, read_heartbeat


@dataclass
class RuntimeView:
    state: str
    rows: list[tuple[str, str, str, str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    config_error: str = ""
    error: str = ""


class DesktopController:
    def __init__(self, *, paths: RuntimePaths | None = None, port: int = 18789):
        self.paths = paths if paths is not None else RuntimePaths()
        self.runtime_dir = self.paths.home / "desktop"
        self.url = f"http://127.0.0.1:{port}"

    def _read(self, port: int, path: str, token_file: str) -> dict:
        # Tokens are exclusively created by the runtime. Opening the desktop
        # during startup or while offline must never create or rotate one.
        token = (self.paths.home / token_file).read_text(encoding="utf-8").strip()
        return call_runtime("GET", path, port=port, token=token, timeout=2.0)

    def snapshot(self, selected: str | None = None) -> RuntimeView:
        beat = read_heartbeat(self.paths)
        state = observed_state(beat)
        if beat is None or state not in {"running", "quiesced", "maintenance"}:
            return RuntimeView(state)
        try:
            port = int(beat["control_port"])
            if not 1 <= port <= 65535:
                raise ValueError("invalid runtime control port")
            document = self._read(port, "/v1/runtime", "agentd.token")
            if (document.get("schema") != "bitcadence.runtime/1"
                    or document.get("pid") != beat.get("pid")
                    or document.get("process_created") != beat.get("process_created")):
                raise ControlError("Runtime identity differs from its heartbeat.")
            view = RuntimeView(
                state=document["state"],
                rows=[
                    (name, child["state"], str(child.get("pid") or ""), child.get("last_error") or "")
                    for name, child in sorted(document["children"].items())
                ],
                config_error=document.get("config_error") or "",
            )
        except (ControlError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return RuntimeView("unreachable", error=str(exc))
        params = {"tail": 100}
        if selected is not None:
            params["worker"] = selected
        try:
            view.lines = self._read(port, "/v1/logs?" + urlencode(params), "agentd.logs.token")["lines"]
        except (ControlError, OSError, ValueError, KeyError, TypeError) as exc:
            view.error = f"Logs unavailable. {exc}"
        return view
