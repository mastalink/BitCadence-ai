"""The scheduler as a runtime child: ``python -m mco.runtime.scheduler_child``.

The same loop as ``bitcadence schedule run``, except that nothing fires while the global STOP is set, and a missing
``schedules.yaml`` is simply "nothing to do" instead of an error. Due schedules are not consumed while held, so each
one runs once when the hold is lifted.
"""

from __future__ import annotations

import argparse

from mco.killswitch import stop_active


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url")
    parser.add_argument("--interval", type=float, default=30.0)
    arguments = parser.parse_args(argv)

    from mco import launcher
    from mco.cli import _gateway_client

    # Loguru's stderr sink is captured by the runtime alongside the other child output.
    client = _gateway_client(gateway_url=arguments.gateway_url)
    launcher.run_forever(client, interval=arguments.interval, hold=stop_active)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
