# V6 / CWS 0.8 specification

CWS 0.8 makes replaceable-conversation worker authority durable and closes the main adversarial gaps discovered during the 0.8 parallel supervision wave. The release remains fail-closed: it does not enable resident auto-dispatch, automatic creation of replacement ChatGPT conversations, automatic live-page eviction, copied authentication, or private ChatGPT endpoints.

## 1. Registry schema v6

Schema v6 is additive over v5. It preserves task/worker rows, observations, reconciliation records, action attempts, watchdog leases, exact-window bindings, page capabilities, the reusable probe slot, and write-ahead probe mutation operations.

It adds:

- `worker_protocol_tasks`: durable task lineage, revision, generation, task status, current worker, handoff target, and completion metadata;
- `worker_protocol_leases`: durable per-worker registration, generation, claim/heartbeat/expiry, terminal status, and supersession metadata;
- `worker_protocol_events`: append-only transition events keyed by task revision and event index.

Migration from v5 creates these structures without rewriting existing v5 rows. Unknown future schema versions fail closed.

## 2. Durable worker authority

The worker protocol models a ChatGPT conversation as a replaceable lease for a durable task. Registration, claim, heartbeat, handoff, takeover, worker completion/abandonment, and durable task completion are persisted through a revision compare-and-swap transaction under `BEGIN IMMEDIATE`.

A transition is accepted only against the expected durable revision. A takeover increments generation and supersedes the prior worker. A stale generation cannot heartbeat, complete, request handoff, or reclaim authority after restart. Durable task completion remains explicit and irreversible.

Legacy task/worker rows can be bootstrapped into the protocol only when their state is unambiguous. Ambiguous legacy combinations fail closed rather than inventing a fresh lease.

## 3. Runtime orchestration adapter

`AdvisoryOrchestrationAdapter` bridges current durable/runtime evidence into the pure orchestration policy without granting mutation authority. It refreshes Local Shell MCP and workspace/Git evidence read-only and carries registry facts including:

- current task/worker identity;
- unresolved recovery actions;
- the global unresolved probe-mutation fence;
- exact worker-window binding;
- reconciliation history;
- recovery budget/cooldown evidence;
- optional page-continuity capability provenance;
- explicit scheduler history.

Missing, stale, future-dated, contradictory, or unavailable evidence becomes a reconcile/human blocker. The adapter does not send ChatGPT messages, open/close browser windows, or manufacture missing durable history.

## 4. Operational probe reconciliation

0.8 exposes bounded operator reconciliation for already-durable probe operations. Evidence is schema-checked, bounded, ownership-bound, and rejects malformed or conflicting identity. An unresolved write-ahead probe mutation globally fences overlapping page-continuity recovery.

The one-slot rule remains unchanged: ambiguity must never cause another probe window to be opened merely because local bookkeeping is incomplete.

## 5. Adversarial closure

The integrated adversarial model promoted the remaining expected failures into production invariants:

1. duplicate orchestration inputs for one durable task are canonicalized and fail closed rather than consuming multiple scheduler slots;
2. an unresolved global probe mutation is represented in the pure orchestration input and blocks recovery mutation;
3. exact worker-window and probe-slot bindings are fresh only when `bound_at <= observed_at <= now < expires_at`;
4. probe reconciliation rejects observations dated in the future instead of minting long-lived future bindings;
5. an active worker rejects a heartbeat whose wall clock moves behind its claim/last-heartbeat timeline.

These checks are deliberately conservative because incorrect freshness can recreate authority that should have expired.

## 6. Recovery boundary

The existing current-worker recovery executor remains available only as an explicit one-shot path. It still requires:

- two distinct, fresh, sufficiently separated reconciliation samples with the same semantic fence;
- current durable Local Shell session identity and no in-flight tool/job/continuation conflict;
- reconciled workspace/Git state;
- current active worker identity;
- a fresh exact-window binding;
- no unresolved prior action;
- available recovery budget;
- write-ahead `ARMED` persistence before external submission;
- positive nonce/hash acknowledgement before the duplicate-send lock is released.

The resident watchdog remains advisory in 0.8. Same-worker resident timeout autorecovery is a subsequent control-loop milestone and must not weaken these fences.

## 7. Verification target

A stable 0.8 release must demonstrate:

- additive schema-v5 to schema-v6 migration while preserving v5 invariants;
- revision/generation persistence across process restart;
- one-winner compare-and-swap behavior for racing worker transitions;
- fail-closed legacy bootstrap;
- operational probe reconciliation without browser mutation during release verification;
- all adversarial clock/fence/duplicate cases passing as ordinary tests, with no expected failures;
- full regression suite, compile checks, `git diff --check`, secret scan, doctor/migration smoke, and a clean Git worktree.
