# BitCadence architecture and decision log

Status: **accepted for implementation** (2026-11-24). Companion records: [ADR 0004](../adr/0004-runtime-bootstrap-supervision.md)
(runtime, bootstrap, supervision, migration), [ADR 0005](../adr/0005-external-publishing.md) (external publishing),
and the build DAG in [`docs/plan/packages.toml`](../plan/packages.toml).

Guiding rule: **design for the next five years, build for the next ninety days.** Every subsystem below exists for a
named reason; everything else is an extension point, not an implementation.

---

## 1. Shape

```
 Desktop app (Tauri) · Web console · CLI · API clients          <- clients only; never supervise
                         |
 Gateway  (identity · jobs · approvals · governance · audit)    <- the engine; unchanged in kind
                         |
 Runtime  (execution · scheduling · supervision · recovery)     <- independent of any client
                         |
 Plugins  (declarative manifests; external commands)            <- domain logic lives here
                         |
 Configured execution resources (local node + configured node list, read-only capabilities)

 OS bootstrap (one entry) starts the runtime;  an external watchdog on another node observes it.
 Private deployment configuration and the credential store sit outside the product.
```

## 2. Execution context (decided)

* v1 runs in the **signed-in user's Windows session**: a detached, windowless runtime. Existing workloads already run
  there; storage mappings, credentials, interactive sign-in and GPU/terminal workloads all work unchanged.
* v1 does **not** guarantee execution before sign-in or after sign-out. We do not enable automatic sign-in or any other
  exposure to compensate. A machine-level service remains possible later if a real requirement appears.
* Independence from the *desktop app* is required; independence from the *user session* is not. They are different
  requirements.

## 3. Who supervises what

| Component | Started by | Supervised by | On crash | On deliberate stop |
|---|---|---|---|---|
| Runtime | One OS bootstrap entry (per-user Scheduled Task, at sign-in, restart-on-failure, single instance) | OS bootstrap + external watchdog | Bootstrap restarts it (non-zero exit) | Exits 0; not restarted |
| Gateway, scheduler, workers, plugin processes | Runtime | Runtime (backoff, crash-loop detection, cooldown) | Restarted per policy | Stopped with the runtime |
| Plugin schedules | Runtime (durable records) | Runtime | Missed fire runs once on recovery (catch-up) | Held |
| Desktop app | User / sign-in | Nobody (a client) | Restart by the user; nothing else is affected | — |
| Terminal host (herdr) | Runtime, optional | Runtime | Pane-hosted processes fall back to hidden | — |
| External watchdog | Its own node | Its own OS | — | — |

Exactly one supervisor runs per user (a user-SID mutex). A second instance starts in **observer mode**.

## 4. Runtime lifecycle

| Event | Runtime | Exit code | Bootstrap | Watchdog |
|---|---|---|---|---|
| Unexpected crash | — | non-zero | launcher retries up to 3 times, one minute apart | alerts only if recovery fails (restarted 3x and still down) |
| Duplicate start | existing runtime continues | 3 for manual start, 0 at task boundary | no retry | existing runtime remains authoritative |
| Deliberate stop (`runtime stop`) | stops children gracefully | 0 | nothing | silent (state = stopped) |
| Maintenance | stays up, children stopped, schedules held | — | nothing | silent (state = maintenance) |
| Global STOP | stays up, quiesced: no spawns, no schedule fires, no external side effects | — | nothing | reports STOP, never "fixes" it |
| Upgrade | records `upgrading`, exits 0 | 0 | installer restarts | silent during the window |
| Hang (heartbeat older than 90 s) | — | — | cannot see it | alerts; may end and re-run the bootstrap only if state is not stopped/maintenance |

**Last-known-good configuration:** an edit that fails validation leaves the running generation in place; the last
five validated generations are kept; `runtime rollback` restores the previous one.

## 5. The agentd decision

The repository already contains a per-user supervisor (`src/mco/agentd/`, ADR 0002). Evidence gathered before
deciding:

| Requirement | Present today | Gap |
|---|---|---|
| Windowless spawn of children and grandchildren | `CREATE_NO_WINDOW`, `CREATE_NEW_PROCESS_GROUP`, `CREATE_SUSPENDED` (platform/windows.py) | verify grandchildren |
| Ownership / no orphans | Job Object with `KILL_ON_JOB_CLOSE`; orphan reaping from a pidfile | reaping checks command name only; add identity (create time) |
| Graceful shutdown | `CTRL_BREAK`, then deadline | verify |
| Crash-loop handling | backoff, crash-loop latch, persisted state, 900 s cooldown | — |
| Reconcile on config change | generation-based reconcile | last-known-good retention |
| Control surface | loopback `/v1` API with separate tokens | extend for plugins, schedules, runtime |
| Entry point / deployment | none (never deployed; a desktop tool runs it in-process) | add `runtime run`; decouple the desktop |

**Verdict (R0-01, 2026-11-24; re-run 2026-11-26 with the stricter review-driven checks, same verdict): deploy agentd as the runtime; no rewrite.** `scripts/assess_agentd.py` ran every
check against the real supervisor and Windows adapter in an isolated home (own home directory, STOP file and mutex):

| Check | Requirement | Result | Evidence |
|---|---|---|---|
| no-window | children and grandchildren never show a window | PASS | sampled every 50 ms from before the spawn: no new visible window; grandchild spawned |
| stop-kills-tree | stopping one worker also stops what it spawned | **GAP** | child stopped; grandchild survived |
| crash-loop | a worker that keeps dying is latched and stops restarting | PASS | crashlooped after 5 starts / 5 failures |
| crash-loop-cooldown | after the cooldown a crash-looped worker gets a fresh set of attempts | PASS | held just before 900 s; retried just after |
| log-drain | a child that floods its output never blocks | PASS | 25 MB writer exited |
| log-rotation | logs rotate at 10 MB x 3 backups | **GAP** | a 26 MB log; writes bypass the handler's rollover |
| graceful-then-deadline | stop asks politely, then kills after a deadline | PASS | break ignored; killed at 5.0 s |
| reconcile | config changes restart only what changed; `off` stops | PASS | two workers: only the changed one restarted (old pid dead), the other kept its pid; stopped on `off` |
| stop-quiesce | with STOP set nothing new is spawned | PASS | no process |
| reap-identity | orphan reaping never kills an unrelated process that reused a recorded pid | **GAP** | an unrelated bystander with the same executable name was killed; records lack create time |
| no-orphans-on-supervisor-death | a hard supervisor death takes the whole tree | PASS | child and grandchild gone |
| restart-no-stale | a restarted supervisor finds nothing stale | PASS | nothing left for reap_orphans to kill; pidfile cleared |
| generic-processes | can supervise the gateway, scheduler and plugin processes | **GAP** | argv only derives from worker modes |

Three gaps are small, contained fixes (kill the process tree on stop; route log writes through the rotating
handler; record and verify process create time before reaping). The fourth is the planned R0-02 feature (a generic
process spec). That meets the rule "a few small fixes -> deploy agentd". Still to run before activation (R0-05): a
2-hour soak of one real worker plus the gateway under the runtime against an isolated gateway. The fallback - a
standalone runtime in the desktop's Rust workspace behind the same contract - is not needed. The runtime contract
(lifecycle table, `/v1` API, plugin contract) stays language-neutral.

## 6. Plugin contract (summary)

For deployment, schemas and the loopback API, see [Runtime plugin contract v1](PLUGIN-CONTRACT.md).

Plugins are declarative manifests (`bitcadence-plugin.toml`) that point at **external commands**; no plugin code runs
inside the runtime, the gateway, the desktop process or the webview.

* Declares: identity (mandatory), long-running processes, schedules (durable; overlap and catch-up rules), a status
  command returning `bitcadence.status/1`, job title prefixes, typed actions (each parameter becomes exactly one argv
  element; never a shell), declarative panels (`bitcadence.view/1`, fixed components, no expressions), required
  secrets (presence and expiry only), health check, kill-switch scopes.
* `[resources]` is **advisory** in v1 (for example `gpu = "exclusive"`, `node = "compute-node"`): reported, never
  enforced, and the schema says so. Existing workload-level coordination stays where it is.
* **Trust:** first use requires owner approval. The lock pins the manifest hash **and** the hash of every resolved
  executable and script argument; any change pauses the plugin for review with a diff.
* **Owner actions:** a native confirmation (plus platform user verification for dangerous actions) yields a
  short-lived grant that the runtime verifies before running the action under the owner identity. Every run is
  recorded in a hash-chained audit log.
* **Kill switch:** global STOP, per-plugin pause, per-channel pause - re-checked before every side effect.

## 7. Machine capabilities (extension point, not a scheduler)

* `nodes.toml` in private deployment configuration lists node identities, endpoints and capabilities; the versioned
  `bitcadence.node/1` structure is reported read-only (health, capabilities) in the desktop.
* **Explicitly not in this release:** distributed dispatch, GPU allocation algorithms, preemption, multi-node
  placement, workload migration, cluster consensus.

## 8. Failure domains

| Domain | Contained by | Recovery |
|---|---|---|
| A worker or plugin process | runtime policy | restart with backoff; crash-loop latch + cooldown; visible in the desktop |
| The gateway | runtime child; separate from workers | restarted; workers retry; job leases expire and are reclaimed |
| The runtime | OS bootstrap | restart on failure; watchdog alerts on failed recovery or hang |
| The desktop | nothing depends on it | restart by the user |
| The terminal host | optional | hidden hosting fallback |
| Bad configuration | validation | last-known-good generation |
| An external side effect (publishing) | ADR 0005 | idempotency key + platform reconciliation; never a blind retry |
| The whole node | external watchdog | alert; manual recovery |

Atomic job claims already exist (the gateway's `lease_next` uses an atomic lease arbiter; a lost race falls through to
the next job) and are covered by a fault test rather than new code.

## 9. Threat model (summary)

| Asset | Threat | Control |
|---|---|---|
| Gateway and runtime tokens | theft from the UI or logs | tokens never reach the webview; Rust holds them; credential store; log redaction |
| Owner authority | a process acting as owner by default | every spawn carries an explicit identity; owner actions require a verified grant |
| Plugin execution | swapped executable or script | executable-integrity pinning; review on change |
| Command injection | crafted parameters | typed parameters, one argv element each, no shell |
| Desktop webview | XSS -> privileged calls | strict CSP, isolation, minimal capabilities, method+path allowlist |
| External publishing | wrong or duplicate post | ADR 0005: authorization, consent, kill switches, idempotency, reconciliation |
| Local control API | other local users | loopback only, per-user tokens, SID-restricted mutex and pipes |
| Deployment privacy | private details in the public repo | invariant 13; CI neutrality check plus a private denylist |

## 10. Architectural invariants

1. One controlled bootstrap; no workload-specific OS scheduler entries.
2. The runtime is independent of every client; clients never supervise.
3. Exactly one supervisor per user; others observe.
4. Fail-closed identity; explicit, verified owner authorization.
5. Executable integrity, not only manifest integrity.
6. Scoped permissions and explicit trust boundaries (no tokens in the webview; allowlists; minimal capabilities).
7. Durable schedules and execution records.
8. Atomic job claims; idempotent, reconciled external side effects.
9. Independent STOP and recovery paths (never through the desktop).
10. The terminal host is optional.
11. Evidence-based cross-model review before merge; production activation gated by evidence.
12. Reversible migration: export, disable, observe, verify, then retire.
13. **Deployment neutrality:** no private identities, hostnames, addresses, account identities, project names or
    machine-specific paths in the public product - source, defaults, fixtures, docs, ADRs, mockups, screenshots, CI,
    release artifacts. Synthetic names only (`primary-node`, `watchdog-node`, `compute-node`, `example-project`).
    Deployment specifics live in private configuration; secrets live in the credential store.

## 11. Rollback

* **Code:** every package merges as one squashed PR; revert is one commit.
* **Activation:** every migrated workload keeps its exported OS-task definition; rollback re-enables it and stops the
  runtime-hosted copy. The bootstrap entry is removable with one command.
* **Configuration:** last-known-good generations.
* **Publishing:** channel modes return to `dry-run`; posts already made are reconciled, never silently repeated.

## 12. Consensus review: accepted, modified, rejected

| Recommendation | Outcome | Rationale |
|---|---|---|
| Runtime in the user session; document limits | Accepted | matches every current workload |
| One bootstrap entry with restart-on-failure | Accepted | more reliable than an unsupervised registry entry |
| External watchdog | Accepted | the OS scheduler cannot see a hung process |
| Evidence-based agentd decision | Accepted | provisional verdict recorded in §5; R0-01 confirms |
| Move migration earlier | Accepted | R2, with full safety gates, before publishing |
| Capability config, no distributed scheduling | Accepted | extension point only |
| Deployment neutrality | Accepted, **strengthened** | the repository is public and already carries deployment detail; remediation is a present task |
| Three planning artifacts | Accepted | this log, ADRs 0004/0005, the DAG |
| Retain all safety properties | Accepted | listed as invariants |
| herdr optional | Accepted | hidden-hosting fallback |
| Atomic job claims | Modified | already implemented; fault test only |
| Gateway as a separate OS service (ADR 0002) | Modified | gateway becomes a runtime child; one bootstrap; worker restarts never touch it |
| Restart-on-failure as complete recovery | Modified | covers crashes only; heartbeat + watchdog cover hangs |
| Desktop hosting the supervisor | Modified | today a desktop tool runs it in-process; decoupling is required in R0 |
| Publishing code in the public repo | Modified | generic contract public (ADR 0005); deployment-specific publishing lives in private plugins |
| A standalone Rust runtime | Deferred | kept only as the R0-01 fallback |
