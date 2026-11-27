---
status: accepted
---

# 0005 - External publishing: authorization, safety and reconciliation

**Related:** ADR 0004 (runtime), `docs/architecture/DECISIONS.md` (invariants 4, 8, 9, 13).

## Context

Plugins may publish to external platforms (video hosts, social networks) on a schedule without a human pressing the
button. External side effects are the highest-risk operations BitCadence performs: they are visible to the public,
often cannot be deleted through the platform API, and a crash at the wrong moment can leave the outcome unknown.
Platform rules also differ: some require per-post user consent screens, some restrict unaudited applications to
private visibility, all impose rate limits and expiring tokens.

## Decision

Autonomous publishing is permitted only through a plugin that implements this contract. Deployment-specific policy
(which accounts, which projects, posting times, approval thresholds) is **private configuration**, never part of the
public product.

1. **Authorization.** Each channel has an explicit mode: `off | dry-run | inbox | auto`. New channels start in
   `dry-run`. `auto` is set by the owner and may require approval of the first *N* posts. Owner-only setup and token
   handling run in a visible session; tokens live in the OS credential store and never appear in files, status output
   or logs.
2. **Consent.** Where a platform requires per-post user consent, the consent is captured in advance through the
   platform's required fields and is bound to a hash of the exact media and text. Changed content needs new consent.
3. **Scheduling and audit.** Each post is a board job with a `not_before` time and, where policy requires it,
   `requires_approval`. Its event history (created, approved by whom, leased, completed with receipt) is the audit
   trail; a local append-only ledger mirrors it. Cancelling the job unschedules the post.
4. **Kill switches.** Global STOP, plugin pause and channel pause are re-checked immediately before every external
   call (upload, container creation, publish). A panic action pauses all publishing.
5. **Idempotency.** Every post has a key derived from channel and content hash; a second publish of the same key
   requires an explicit owner action.
6. **Reconciliation.** Each post moves through durable states
   (`queued -> needs_owner -> armed -> preparing -> ready -> publishing -> published_unverified -> verified`, with
   `failed`, `cancelled`, `skipped`). After a crash in an uncertain state the plugin **asks the platform** what
   happened (container or publish status) and never re-publishes blindly.
7. **Verification.** A post is `verified` only after the platform confirms it (identifier, permalink, timestamp,
   content hash), re-checked after a delay. Rate limits are checked before publishing.
8. **Degradation.** When a platform restricts the application (for example to private visibility before an audit),
   the channel drops to `inbox` mode (the media is delivered to the account's drafts or inbox and the owner is
   notified) instead of failing silently or retrying.
9. **Testing.** Platform adapters are tested against recorded fixtures for every endpoint and error; dry-run must make
   zero publish calls; a canary on a test account precedes `auto` on any real channel.

## Consequences

* Publishing is autonomous where policy allows, and every post is attributable, stoppable and reconcilable.
* Fully unattended posting can be limited by platform audit decisions outside our control; inbox mode is the designed
  fallback.
* The public repository carries only this contract; accounts, projects and policies stay private.
