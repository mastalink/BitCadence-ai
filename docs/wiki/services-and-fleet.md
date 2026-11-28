# OS Services & Fleet Configuration

## Goal
Configure BitCadence processes (gateway, scheduler, wakers) as boot-persistent operating system services, declare worker fleet topologies in `fleet.toml`, and reconcile services via `mco fleet apply`.

> **See Also:** For in-depth coverage of background deputies, the `cio-agent` Chief sidecar, elevated admin-pack flows, health verification levels, and locked-log/duplicate-waker troubleshooting, see [**Sidecars & Autonomous Fleet Daemons**](sidecars-and-fleet-daemons.md).

---

## Step-by-Step Instructions

### 1. Declarative Fleet Configuration (`fleet.toml`)
BitCadence defines worker deployment topologies in `~/.mco/fleet.toml`:

```toml
[workers.codex-builder]
role = "codex"
instance = "codex-primary"
mode = "waker"                 # "waker" (event-driven), "poll" (timer), or "off"
exec = "C:/work/bitcadence/scripts/workers/codex-worker-run.ps1"
min_interval = 10
poll_interval = 1800
background = false             # Set true for unattended S4U Windows Task

[workers.cio-agent]
role = "chief"
instance = "cio-agent"
mode = "waker"
exec = "python -m mco.orchestrator.cio_runner"
min_interval = 10
background = true              # Survives user sign-out
```

### 2. Applying Fleet Run Modes
Apply the configuration to spawn and align worker processes:

```powershell
# Apply declarative fleet settings
mco fleet apply

# Inspect current fleet status
mco fleet status

# Change an individual worker mode
mco fleet set codex-builder mode=off
```

### 3. Installing OS Services
To survive system restarts and user logouts, install BitCadence components as native OS services:

**On Windows:**
Installs via Windows Task Scheduler or Windows Service Control Manager:
```powershell
# Install the gateway service
mco service install

# Install the background scheduler service
mco service install-scheduler

# Install the event waker service
mco service install-waker codex --exec "python run_worker.py"
```

**On Linux:**
Creates and enables `systemd` user units (`~/.config/systemd/user/bitcadence.service`):
```bash
mco service install
systemctl --user daemon-reload
systemctl --user enable --now bitcadence
```

### 4. Service Diagnostics and Lifecycle
```powershell
# Check service execution status
mco service status

# Restart services after code updates
mco service restart

# View unified service logs
mco service logs
```

---

## What You'll See

- **Self-Healing Supervision:** If a worker crashes, the supervisor restarts it with exponential backoff (up to 5 crashes in 5 minutes before pausing to avoid rapid crash loops).
- **Background Execution:** Services run without open terminal windows or user session dependencies.

---

## If It Goes Wrong

### 1. "Access is denied when installing service"
- **Cause:** Installing system-wide services requires administrative privileges.
- **Fix:** Open PowerShell as Administrator, or use user-level task installation.

### 2. "Workers crash repeatedly on startup"
- **Cause:** Missing environment variables or Python dependencies in the service execution context.
- **Fix:** Inspect logs with `mco service logs` to view standard error traces.

## External runtime watchdog

On a separate host, give the watchdog read-only access to the runtime directory
or a consistently refreshed copy of its evidence. Expose both `heartbeat.json`
and `bootstrap-status.json`, preserve their contents, and keep the hosts'
clocks synchronized. Install the repository's Python dependencies and run the
following commands from a checkout.

Copy the synthetic `ops/watchdog/config.example.json` to a private location
outside the repository. Set `runtime_dir` to the exposed runtime directory.
Relative paths are resolved from the private config's parent directory.

```powershell
python -m ops.watchdog check --config <private-config>
python -m ops.watchdog drill --dry-run
```

Use `stdout` transport for JSON output from each check, or set `transport` to
`webhook` and supply an HTTPS `webhook_url` in the private config. The webhook
receives the same JSON observation by POST when a check raises an alert.
Schedule `check` with the watchdog host's scheduler. Each invocation evaluates
the current evidence independently, so a persistent alert is sent on each
poll. Exit codes are 0 for a quiet check, 1 for an alert, and 2 for a config or
delivery failure.

A heartbeat older than 90 seconds raises an alert. A stopped or maintenance
runtime stays quiet, and a quiesced runtime reports its state. After four
crashes exhaust the bootstrap's three retries, the watchdog raises
`recovery-failed` if the runtime is still down. The watchdog only observes and
notifies. The dry-run drill uses temporary fixtures and prints the outcomes
for a deliberate stop, four crashes, and a stale heartbeat.
