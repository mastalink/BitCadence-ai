"""A real runtime in its own process, for the fault-injection tests.

The only difference from ``bitcadence runtime run`` is a test-private singleton mutex, so a runtime already running
for this user is neither disturbed nor in the way. The caller points HOME / USERPROFILE at a temporary directory.

    python runtime_driver.py <mutex-suffix> <control-port> gateway <gateway-port>
    python runtime_driver.py <mutex-suffix> <control-port> stub <stub-script>
"""

from __future__ import annotations

import sys

from mco.agentd.platform.windows import WindowsAdapter, WindowsSupervisorLock
from mco.runtime.core import Runtime
from mco.runtime.paths import RuntimePaths
from mco.runtime.supervision import ServiceProcess, default_services


def main() -> int:
    suffix, control_port, kind, argument = sys.argv[1:5]
    paths = RuntimePaths()
    lock = WindowsSupervisorLock(paths.home / "agentd.lock")
    lock.name = f"Local\\BitCadence-runtime-test-{suffix}"
    if kind == "gateway":
        services = default_services(scheduler=False, gateway_port=int(argument))
    else:
        services = [ServiceProcess("stub", (sys.executable, argument, "service"))]
    runtime = Runtime(
        paths,
        adapter=WindowsAdapter(supervisor_lock=lock),
        services=services,
        control_port=int(control_port),
        heartbeat_interval=0.5,
        tick_interval=0.2,
    )
    return runtime.run()


if __name__ == "__main__":
    raise SystemExit(main())
