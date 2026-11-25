# BitCadence Score v1

Status: implemented **offline contract, compiler and sandbox policy kernel**, the local Score bridge/conductor/dispatcher/sweep, and the contract for fixed-function cloud adapters. Score Cloud v2 (always-on Postgres conductor, git-native PR evidence, multi-lane workers) is design-only — see `docs/SCORE-CLOUD-V2.md` and `docs/SCORE-CLOUD-V2-PACKETS.md`. Historical source base for the v1 contract: cc01c60a7a44cb522c334ba42b886f3ce37cad90. Deployment-specific adapters keep their acceptance records privately; none of them make arbitrary Score packets executable.

## Decision

A score is a versioned JSON contract for an outcome, its dependency graph, ownership, authority requirements, evidence, retries, explicit human checkpoints and launch criteria. The conductor advances it automatically. The person chooses policy and checkpoints up front; they are not a recurring task dispatcher.

Reuse existing BitCadence workflows, routing, lease fencing, audit and job board. **Do not submit a rich score through the legacy workflow endpoint:** it does not enforce evidence acceptance, score-bound authority, independent authorship, resource locks or atomic whole-run submission. Silently dropping those rules would be worse than rejecting the score.

## What exists now

- `src/mco/orchestrator/workflows.py`: YAML dependency graphs and per-step job submissions, workflow context stamps, retries and approval fields. Submission can partially succeed; a retry generates another run ID.
- `client.py`: authenticated gateway job submission and role/instance targeting.
- `handlers.py`: job transitions and dependent-job unlock; completion is not a product acceptance certificate.
- `leases.py`: lease/epoch/store-incarnation fencing. Reuse it; do not invent a second worker lease system.
- `routes.py`: approval routes require configured approver roles/scopes. A score's human-only gate needs a verified human principal, not merely any configured agent role with approval scope.
- `audit.py`, `evidence.py`, `drumline.py`: audit/evidence and shared workflow context. These do not establish that arbitrary attached test reports are semantically valid.

These are inspected code capabilities, not assertions of the deployed gateway revision or production readiness. Existing governance/cloud acceptance issues remain separate.

## Deployed cloud adapters

Deployment-specific cloud adapters (for example a scheduled, fixed-function read-only audit Lambda or a signed-approval release lane) live in private deployment repositories, not in this repo. They follow the same rule: no model-supplied shell command, URL, credential or deployment request is ever consumed.

## New artifacts

`docs/score-v1.schema.json` is the structural JSON Schema. `load_score()` additionally checks duplicate JSON keys, whitespace-only values, unique IDs, known dependencies, acyclic graphs and distinct work/review roles. It accepts JSON text or objects, never a path supplied by a remote caller. The explicit offline CLI reads a file.

`compile_score()` produces deterministic, digest-bound work packets, preserving every task contract. Its output deliberately has no legacy `steps` property and declares `live_submission_supported: false`. No arbitrary command executor, network client or credential loader is included.

`SandboxRun` exercises the intended conductor policy without external effects: pending→running→review→accepted, bounded expiry/retry→blocked, dependency locks, shared resource locks through review, parallelism limits, external capability grants, conservative integer-cent budget reservations, explicit human gates, attempt tokens and score/run identity. Each event carries a sequence, run and score digest. Worker completion alone never unlocks a dependent task. Launch may be accepted while later expansion tasks remain planned.

**Sandbox trust boundary:** actor kinds, identities, grants, clock and evidence-verification results are caller-supplied trusted inputs. This is not authentication, cryptographic evidence verification or a production security boundary. Evidence values are structurally checked references, not fetched/hashes checked by this module. Events and state are in memory; durable restart recovery and concurrency require the adapter below. Do not expose these methods directly as worker-callable HTTP endpoints. Textual constraints are instructions until explicitly enforced by appropriate adapter policy.

## Usage

From a checkout with `src` on `PYTHONPATH` (or an editable install):

```bash
PYTHONPATH=src python -m mco.orchestrator.scores validate examples/scores/score-human-gate-canary.score.json
PYTHONPATH=src python -m mco.orchestrator.scores compile examples/scores/score-human-gate-canary.score.json
PYTHONPATH=src python -m mco.orchestrator.scores preview examples/scores/score-human-gate-canary.score.json
```

Windows PowerShell equivalent (adjust the checkout path):

```powershell
$env:PYTHONPATH = 'C:\path\to\BitCadence\src'
python -m mco.orchestrator.scores validate examples/scores/score-human-gate-canary.score.json
```

Output is JSON on stdout. Invalid files exit 2. There is intentionally no submit or launch command on this offline module surface.

## Example scores

`examples/scores/` holds small canary scores (human gate, resource lock, evidence binding, on-reject chain, conductor canary). Each loads with `load_score()` and is exercised by the test suite. Production scores for a specific deployment belong in that deployment's private repository.

`checkpoint: null` means no score-authored human gate; external authority is still required. A zero budget is not a prediction of free operation, and agents cannot label chargeable work free to bypass accounting.

## Remaining implementation, in dependency order

1. **S01: durable score store and version authority.** Add reviewed migrations using the actual migration head. Persist score document+digest, run ID, organization, authorized policy snapshot, task attempts and resource/budget reservations. Idempotency key unique per tenant/run/task/attempt. Commit planned dispatch and state transitions in one transaction. A changed score requires a new digest and explicit migration of unresolved work; old approvals cannot transfer automatically.
2. **S02: dispatcher/outbox bridge.** Only conductor-owned accepted states unlock tasks. Outbox writes existing job-board work with score/run/task/attempt stamps; deduplicate retries across timeout and uncertain acknowledgements. Reconcile created jobs after restart instead of resubmitting blindly. Existing job leases fence worker updates; score leases fence competing conductors. Resolve capabilities to online eligible identities, observe lease/presence, reassign only after safe expiry. No worker can advance the score by writing `completed` alone.
3. **S03: verified evidence and non-author review.** Fetch artifacts through allowed locations, verify digests, bind tests to exact head/build/deployment, reject skipped mandatory suites and author/reviewer overlap including re-applied commits. Server selects reviewers from identity/contribution records. A claimed `verified_evidence=true` in worker output is never sufficient. Ingest code review and test receipts as separate authenticated events.
4. **S04: policy and human gate UI.** Signed/authenticated owner-approved grants scoped to run digest, actions, resources, environments, time and budget. One explicit human gate view shows decision/evidence and approve/reject. No artificial per-task approval queue. Agent approvers cannot impersonate human principals. Missing authority pauses only the affected path. Authorizations and checkpoint approvals are separate records.
5. **S05: launch/recovery adapters and quiet operation.** Reuse repository/build/deploy tools through allowlisted adapters; protected isolated worktrees; before/after receipts; rollback and compensate known partial effects. Differentiate uncertain effect from retry-safe failure. Recheck desired deployment state before replaying deploy/notify operations. Conductor runs in approved cloud infrastructure. Notify only for chosen human gates, irrecoverable owner-action blockers or milestone completion; dashboard holds routine updates.
6. **S06: executable decomposition and cloud canary.** Resolve G01 facts, pin/upload the accompanying plan artifacts, replace coarse capabilities/locks with narrow work packets, validate actual role availability and budget. Execute a no-production-effects score with a worker crash, duplicate delivery, rejected review, human gate, restart and completed evidence trail. Then a scoped cloud canary. Only after independent review and acceptance enable authorized production work through launch.

Each item should be one bounded AI work packet with a different reviewer. Database/policy interfaces land before concurrent adapter implementation. No new agent is claimed assigned or running by this document. Expected next delivery is S01+S02 on the existing job board, not a second orchestration product.

## Adapter acceptance matrix

Required before live use: unknown fields/cycles rejected; worker cannot forge actor or human status; cross-tenant and stale-digest receipts rejected; author aliases cannot self-review; expired grant fails closed; concurrent budget/resource reservations atomic; duplicate dispatch reconciled; lease expiry rejects stale writes; crash-before/after side-effect recoverable; restore invalidates old incarnation; no unsupported evidence accepts; gate rejection remains terminal until an explicit new decision; changed score invalidates scoped approvals; no launch on mere merged code; production rollback and notification idempotence tested.

The current unit tests cover only the policy subset stated above. They cannot prove authenticated actor identity, persistence, a cloud deployment or actual independent code review.
