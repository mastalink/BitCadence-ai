# External watchdog

On a separate watchdog host, share the runtime directory or sync it to that
host. Set `runtime_dir` in the watchdog configuration to the shared or synced
directory so the watchdog can read `heartbeat.json` and `bootstrap-status.json`.

If the bootstrap exhausts its retries, the watchdog may briefly report
`recovery-failed` before Windows Task Scheduler's restart-on-failure policy
relaunches the bootstrap.
