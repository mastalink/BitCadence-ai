---
status: accepted
---

# 0004 - Independent runtime, single bootstrap, desktop as a client

**Supersedes on Windows desktops:** ADR 0002 sections on per-machine services and the gateway as a separate OS
service. ADR 0002's supervision policy (backoff, crash loop, reconcile) is kept.
**Related:** ADR 0003 (optional terminal host), ADR 0005 (external publishing), `docs/architecture/DECISIONS.md`.

## Context

Execution on a typical Windows deployment is spread across many OS scheduler entries: one per worker, one per watcher,
and one per job. Several start console programs directly, so windows flash on screen; stale entries accumulate; and
nothing owns their lifecycle. A per-user supervisor already exists in the codebase but has never been deployed - a
desktop tool runs it inside its own process, coupling supervision to a window.

## Decision

1. **Execution context.** A detached, windowless **runtime** runs in the signed-in user's session. v1 does not run
   before sign-in or after sign-out, and does not enable automatic sign-in to compensate.
2. **One bootstrap.** A single per-user Scheduled Task, created and removed by `bitcadence runtime
   install-bootstrap | uninstall-bootstrap`: trigger at sign-in of this user; action runs the runtime windowless;
   restart on failure (3 attempts, 1 minute apart); do not start a second instance; no execution time limit. The
   exported task definition is kept for rollback. No other BitCadence workload registers OS scheduler entries.
3. **The runtime owns execution.** It supervises the gateway, the scheduler, fleet workers, plugin processes and
   plugin schedules (durable). It holds the per-user supervisor mutex; a second instance observes.
4. **Lifecycle semantics are explicit** (see the table in DECISIONS.md §4): crash -> non-zero exit -> bootstrap
   restart; deliberate stop, upgrade -> exit 0 -> no restart; maintenance and STOP -> stay up, quiesced. The runtime
   writes a heartbeat every 15 s; an **external watchdog** on another node alerts on a stale heartbeat or failed
   recovery and never restarts a runtime that was stopped on purpose.
5. **Gateway as a runtime child.** Workers and the gateway are separate children, so worker restarts never drop the
   API; only a runtime restart (rare) restarts the gateway, and workers already retry. This removes a second
   bootstrap entry.
6. **Clients never supervise.** The desktop app, the web console and the CLI talk to the runtime's loopback control
   API and to the gateway. Closing or crashing a client changes nothing in execution.
7. **Implementation.** Deploy the existing supervisor (`mco.agentd`) as the runtime after the bounded R0-01
   assessment confirms it; a standalone Rust runtime is the fallback only if the assessment finds major gaps.
8. **Last-known-good configuration.** Invalid edits never replace a running generation; five validated generations
   are retained; rollback is one command.

## Windows bootstrap commands

Run `bitcadence runtime install-bootstrap` as the user who will run the runtime. It creates
`BitCadence Runtime <Windows user SID>` and exports the registered XML as UTF-16 to
`~/.mco/runtime/bootstrap.xml`. Pass `--xml <path>` to choose another export location.
Registration uses the current Python installation and package directory, so keep those paths
available at sign-in. An existing task of that name must be uninstalled before reinstalling.

The Scheduled Task starts a windowless launcher, which runs `python.exe` inside the
hidden-console adapter and waits for its exit. A manual duplicate start returns 3. The
launcher maps that result to 0 and ends without consuming a retry. A deliberate stop also
ends the launcher with 0. The Windows drill found that returning a runtime crash code
through a successfully started action did not trigger a retry. The launcher handles
runtime process crashes with up to three retries, with a 60-second delay before each.
A runtime that runs for more than 10 minutes resets the retry budget. The native task
settings remain three attempts one minute apart for action launch failures.
The launcher records crash codes and retry decisions with UTC timestamps in `~/.mco/runtime/bootstrap.log`.
The launcher also atomically publishes its latest state, timestamp and retry count
in the runtime directory's `bootstrap-status.json` for the external watchdog.
The runtime process writes its standard output and standard error to `~/.mco/runtime/bootstrap-child.log`.

Use `bitcadence runtime stop` for a graceful stop. To roll back bootstrap registration,
run `bitcadence runtime uninstall-bootstrap`. This removes only the current user's
managed task, ending its active action if needed, and keeps the XML export and runtime
data. Reinstall from an available Python environment and sign in again to relaunch.

The Tkinter desktop shows runtime state, processes and logs through the loopback
`/v1` API. Open it with the desktop shortcut or `bitcadence-desktop`. Desktop exit
closes the observer. The optional Startup shortcut also opens only the observer.

## Migration rules

A production workload moves under the runtime only when all of these hold:

* the runtime runs it correctly (compatibility evidence recorded);
* its OS task definition is exported;
* the OS task is **disabled before** the runtime starts the workload (no duplicate execution);
* an equivalence check passes (heartbeat, health endpoint, or produced artifact matches the old path);
* the watchdog covers it;
* a workload-specific rollback has been written down;
* the owner (or the delegated chief role) authorized that step.

Order: one low-risk worker, then remaining workers one at a time, then watchers and portals, then one-off job tasks
(deleted only after the owner confirms the list). Disable, observe, verify, then retire. Unknown tasks are never
deleted. Third-party tasks are out of scope.

## Consequences

* One OS registration instead of dozens; no console windows from BitCadence workloads.
* Execution survives desktop crashes and upgrades; runtime upgrades briefly restart the gateway.
* Nothing runs while the user is signed out (documented limitation).
* The supervision policy is shared with headless/Linux deployments of the same supervisor.
