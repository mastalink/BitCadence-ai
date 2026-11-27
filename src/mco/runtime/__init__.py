"""The BitCadence runtime: the independent, windowless owner of execution (ADR 0004).

It deploys :mod:`mco.agentd` - supervisor, platform adapter and fleet configuration - and adds the lifecycle
contract around it: exit codes, heartbeat, STOP quiesce, maintenance, gateway and scheduler as supervised children,
last-known-good configuration and the ``/v1/runtime`` control API.
"""

from .core import EXIT_ALREADY_RUNNING, EXIT_CRASH, EXIT_STOPPED, Runtime
from .paths import RuntimePaths
from .supervision import RuntimeSupervisor, ServiceProcess, default_services

__all__ = [
    "EXIT_ALREADY_RUNNING",
    "EXIT_CRASH",
    "EXIT_STOPPED",
    "Runtime",
    "RuntimePaths",
    "RuntimeSupervisor",
    "ServiceProcess",
    "default_services",
]
