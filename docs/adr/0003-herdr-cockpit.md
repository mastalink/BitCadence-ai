---
status: accepted
---

# The cockpit: an optional herdr session for terminal-hosted processes and jobs

**Builds on:** [ADR 0002](0002-one-supervisor-per-machine.md) (one supervisor, no console windows).
**Related:** ADR 0004 (independent runtime and single bootstrap) owns lifecycle; the cockpit is one *host* it may
use. **herdr is optional:** without it, processes run hidden and nothing in BitCadence depends on herdr being present.
**Implementation status:** built - `src/mco/cockpit.py`, `fleet.toml` `mode = "pane"`, `src/mco/reviews.py`,
`bitcadence cockpit ...` and `bitcadence review fanout`.

## Problem

ADR 0002 covered long-running *workers*. It did not cover **one-off jobs**: a typical deployment accumulates
throwaway OS-scheduler entries (one per render, retry or fix), each starting `python.exe` or `cmd.exe` directly -
a console window flashing on screen per run, a stale task left behind afterwards, and output nobody can see without
opening a log file. Operators want every terminal program framed inside one place they can watch, not popping up.

## Decision

Adopt **[herdr](https://github.com/herdrdev/herdr)** (Rust, Apache-2.0, native Windows via ConPTY) as an optional
host for anything that runs in a terminal: a persistent, **headless** session of workspaces and labelled panes that
can be attached from any terminal (or over SSH), read, and waited on through a JSON CLI. It recognises common coding
agents, so a pane shows idle / working / blocked.

We do **not** build our own terminal multiplexer; herdr already does the hard parts (PTYs on Windows, persistence,
agent state).

### The pieces

| Piece | What it is |
|---|---|
| `mco.cockpit.run(label, command)` | **The** way to start a one-off job: its own labelled pane in the `jobs` workspace of the configured session. Refuses a label that is already live. Never opens a window. |
| `fleet.toml` `mode = "pane"` | Opt-in for workers an operator wants to *watch*: the waker runs in the `agents` workspace; `fleet apply` removes that worker's waker/poll OS services so nothing runs it twice. |
| `~/.mco/cockpit.toml` `[[pane]]` | Other long-running terminal processes. `bitcadence cockpit up` starts them; re-running starts only what is missing. |
| `herdr` key / `BITCADENCE_HERDR` / PATH | Where herdr lives is **deployment configuration**. Product code has no machine-specific default. |

`bitcadence review fanout` is the matching function for reviews: one board job per model family, never the
builder's own family, never fewer than two families. The default panel addresses **roles** only; a deployment pins
specific worker instances in its private `~/.mco/review_panel.toml`.

### Relationship to agentd (ADR 0002) and the runtime (ADR 0004)

* The runtime (agentd-based) remains the **supervisor** for hidden processes: restart backoff, crash loops, logs.
* `mode = "pane"` is the **visible** host. Its restart policy comes from the runtime, not from herdr.
* If herdr is absent or unhealthy, pane-hosted processes fall back to hidden hosting.

### Reboots: herdr restores layout, not processes

herdr's snapshot restore brings back workspaces, panes, labels and cwd after a server restart or reboot, but every
pane comes back as a **fresh idle shell**. A live label therefore does not prove its command is running.
`cockpit.up()` checks each declared pane:

* busy (the pane's shell has a child process) -> leave it alone;
* restored as an idle shell -> run its command again in that same pane;
* missing -> create it.

On Windows `pane process-info` reports only the shell (no foreground process groups), so "busy" is decided by asking
the OS whether `shell_pid` has a child (Toolhelp32; `pgrep -P` elsewhere). Starting the cockpit at sign-in is the
runtime's job (ADR 0004); the cockpit registers nothing with the OS scheduler itself.

### herdr on Windows: what to know

* Native Windows is generally available (ConPTY bundled). The binary is **unsigned**; SmartScreen or endpoint
  protection may flag it. Elevated servers need admin clients unless `allow_unelevated_clients = true`; run the
  cockpit unelevated.
* Not every agent integration supports Windows; unsupported agents still run, with screen-based state only.

## Verification performed (herdr 0.9.5, Windows 11, isolated test sessions)

* Headless server started with `CREATE_NO_WINDOW | DETACHED_PROCESS`; two jobs run via `cockpit.run`; enumerating
  visible top-level windows before and during the run showed **no new windows**.
* Output read back through `pane read`; `wait-output` matched; a duplicate label was refused; the test session was
  stopped and deleted afterwards.
* Busy detection on a real pane: idle shell `False`, while a command runs `True`, after it exits `False`.

## Adoption (operator steps)

1. Install herdr; point `cockpit.toml` `herdr = ...` at it, or put it on PATH.
2. Move long-running terminal processes into `cockpit.toml`, run `bitcadence cockpit up`, then disable each old OS
   task - one at a time, while it is idle.
3. Change job launchers to `bitcadence cockpit run <label> -- <command>` instead of registering an OS task per job.
4. Optional: `bitcadence fleet set <worker> mode=pane` + `fleet apply` for workers you want on screen.

Attach any time with `bitcadence cockpit attach`.
