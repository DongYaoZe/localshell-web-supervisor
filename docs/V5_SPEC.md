# V5 / LWS 0.7 specification

LWS 0.7 extends the 0.6 control plane with crash-fenced probe-window mutation operations, deterministic advisory recovery orchestration, and a pure replaceable-conversation worker lease protocol.

The release remains conservative: it does not enable unattended web chat sends, automatic live-worker page eviction, automatic web chat conversation creation, copied authentication, or private web chat endpoints.

## 1. Registry schema v5

Schema v5 is additive over schema v4. It preserves existing tasks, workers, observations, reconciliation records, action attempts, watchdog leases, worker-window bindings, page capabilities, and the single reusable probe slot.

It adds `probe_mutation_operations`. Each operation records bounded control metadata for one intended probe-window `OPEN`, `ROTATE`, or `CLOSE`:

- operation id and nonce;
- operation kind and durable state;
- target task, worker, conversation URL, slot id, and LWS ownership token;
- expected actual URL and Chrome executable;
- sanitized snapshot of the prior probe slot when applicable;
- timestamps, bounded reconciliation counters/outcomes, and resume state;
- no credentials, prompt bodies, response bodies, or conversation text.

A partial unique SQLite index permits at most one unresolved probe mutation operation across supervisor processes.

Unresolved states are:

- `ARMED`
- `CLOSE_SUBMITTED`
- `READY_TO_OPEN`
- `OPEN_SUBMITTED`
- `RECONCILE_REQUIRED`

Terminal states are `COMPLETED`, `FAILED`, and `CANCELLED`.

## 2. Write-ahead probe mutation authority

A browser mutation is never authorized only by an in-memory page plan. The corresponding durable operation must exist before an external close/open phase can occur.

For `CLOSE` and the close phase of `ROTATE`, the durable state moves to `CLOSE_SUBMITTED` before the transport is called. For `OPEN` and the open phase of `ROTATE`, the durable state moves to `OPEN_SUBMITTED` before the transport is called.

This makes process death part of the normal protocol. A crash after authority was persisted but before the caller records the result is an ambiguous side-effect window; it is reconciled rather than replayed.

## 3. Probe reconciliation

One bounded read-only observation classifies the real browser state against the durable operation. Supported outcomes are:

- `EXACT_TARGET_ABSENT`
- `EXACT_UNIQUE_OWNED_TARGET_PRESENT`
- `OLD_TARGET_STILL_PRESENT`
- `BOTH_OLD_AND_NEW_PRESENT`
- `MULTIPLE_MATCHES`
- `STALE_OR_CHANGED_IDENTITY`
- `UNKNOWN_OBSERVATION`

Identity checks bind the evidence to the expected LWS ownership target and the relevant URL/HWND/PID/Chrome executable facts. Incomplete observations or changed/multiple identities fail closed.

Important transitions:

- An `OPEN` still in `ARMED` with no target present may issue one open authority.
- An expected new target observed before durable open authority is an identity/protocol violation and blocks.
- A unique exact owned target after `OPEN_SUBMITTED` may be adopted into the durable probe slot and complete the operation.
- A submitted open whose target is absent is ambiguous and must not be blindly replayed.
- A `CLOSE` in `ARMED` with the exact old target still present may issue one close authority.
- Once a close has been submitted, seeing the old target still present is ambiguous; another close is not issued blindly.
- For `ROTATE`, old-target absence with no replacement present before open authority is the safe `READY_TO_OPEN` crash point.
- Old and new targets simultaneously present, multiple matches, stale/changed identity, or incomplete evidence always blocks further mutation.

The core invariant is: **a process crash must never justify opening an additional probe window merely because durable slot bookkeeping is incomplete.**

## 4. Page-pool transactional semantics

The ephemeral `PagePool` remains transport-neutral and does not launch or close pages. Existing-lease role updates are transactional: if a role change would violate active/probe capacity, the prior role, worker id, and last-used timestamp are restored before the error is returned.

This prevents a failed advisory pool update from leaving a plausible but false in-memory role assignment.

## 5. Advisory orchestration

`orchestration.py` is a pure scheduling/policy layer. It evaluates recovery candidates from durable facts supplied by an adapter, including:

- task and assessment state;
- unresolved action attempts;
- recovery budget;
- Local Shell session identity/freshness, in-flight calls, tracked jobs, and continuation state;
- workspace/Git identity, freshness, and checkpoint consistency;
- active worker lease freshness;
- recovery cooldown/backoff;
- two distinct, fresh, sufficiently separated reconciliation samples with a stable semantic fence;
- exact worker-window binding when required;
- page-continuity capability provenance when parking/reopen behavior is relevant;
- fairness across multiple recovery candidates.

Decisions are `observe`, `reconcile`, `recommend-action`, `wait/cooldown`, or `blocked/human`.

Even a selected `recommend-action` decision has `mutation_allowed=false`. The orchestration layer cannot invoke the UIA transport, `dispatch-execute`, probe mutation transport, or worker takeover by itself.

`attention_queue` also collapses duplicate candidates for the same durable task deterministically, retaining the highest-priority state and a stable tie-breaker.

## 6. Replaceable worker lease protocol

`worker_protocol.py` models multiple web chat conversations as replaceable leases for one durable task without binding task identity to a browser tab.

The pure protocol includes:

- worker registration and claim;
- active lease heartbeat/expiry;
- handoff request and takeover eligibility;
- supersession and stale-worker fencing;
- monotonic task revision and worker generation fencing;
- worker completion/abandonment distinct from durable task completion;
- parent/root/child task lineage metadata.

A late heartbeat from a superseded generation cannot regain authority. Racing callers must use the expected revision; stale revisions are rejected.

The 0.7 release intentionally does **not** persist this protocol into a new registry schema and does not create web chat conversations automatically. `WORKER_PROTOCOL.md` describes the adapter fields/events a later milestone can persist atomically.

## 7. Action execution remains separate

The existing recovery-message `ActionAttempt` fence is unchanged in purpose. Probe mutation operations do not replace message-action fencing, and message-action attempts do not authorize browser probe open/close.

`dispatch-plan` remains dry-run by default. `dispatch-execute` remains an explicit one-shot current-worker continuation requiring exact task confirmation, a candidate-ready stable semantic fence, durable LSM/workspace revalidation, a fresh exact-window lease, no unresolved action, remaining recovery budget, and per-invocation UIA opt-in.

The resident watchdog never supplies those opt-ins automatically.

## 8. Default-disabled boundaries

LWS 0.7 still does not enable:

- unattended web chat send/retry/takeover;
- automatic live-worker page closing;
- automatic probe-window mutation from the watchdog;
- automatic creation of replacement web chat conversations;
- copied browser authentication or credential migration;
- private web chat transport reconstruction.

Ambiguity resolves to reconciliation, blocking, or human attention rather than optimistic retry.

## 9. Verification target

A stable 0.7 release should demonstrate:

- additive schema-v4 to schema-v5 migration;
- global unresolved probe-operation uniqueness;
- crash-boundary and ambiguity reconciliation tests;
- transactional PagePool role updates;
- deterministic duplicate-free attention scheduling;
- advisory orchestration with `mutation_allowed=false`;
- generation/revision worker fencing tests;
- full regression suite, compile/static checks, secret scan, and a clean Git worktree;
- no real browser mutation and no resident watchdog start as part of release verification.
