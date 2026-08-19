# V4 / LWS 0.6 — reusable page identity and explicit recovery execution

LWS 0.6 advances the control plane without turning the resident watchdog into an autonomous browser mutator. The default remains observation and planning. Mutation exists only in explicit, bounded paths with durable write-ahead state.

## 1. Single reusable probe slot

A parked conversation may be sampled through one logical LWS probe slot.

The planner has four outcomes:

- `REUSE` — the fresh LWS-owned slot already points at the same parked worker;
- `OPEN` — no slot exists, so one tagged slot may be created by an explicitly enabled transport;
- `ROTATE` — the fresh owned slot points at another parked worker; the old exact window must be proven absent before a replacement may open;
- `BLOCKED` — stale, ambiguous, non-owned, active-worker, or invalid-conversation state.

A stale slot never causes an implicit replacement window. Registry v4 enforces at most one durable probe slot. The probe-window mutation transport remains disabled by default and has no watchdog auto-use path in 0.6.

The slot tag is a local ownership marker, not an authentication mechanism. Exact window identity still uses top-level HWND, Chrome PID, executable path, target conversation identity, and the ownership-tagged URL.

## 2. Durable page-continuity capability provenance

A successful close/reopen experiment is no longer treated as a release-wide boolean.

Schema v4 stores capability records with:

- capability kind: ordinary generation continuity or live-tool continuity;
- scope host;
- browser family and major version;
- operating platform and observation surface;
- isolation mode;
- evidence evaluator version;
- evidence digest and source experiment id;
- actual experiment observation time, import time, and expiry;
- sanitized boolean check metadata only.

The raw prompt/response text and page signatures are not copied into the capability row. A capability is usable only when its evaluator version and runtime context match and it has not expired.

`pool-plan` remains fail-closed by default. Durable capability use is explicit with `--page-close-capability`. The legacy one-shot evidence-file path remains available for compatibility.

## 3. Explicit current-worker recovery executor

`dispatch-plan` remains a dry run. `dispatch-execute` is a separate one-shot command and is blocked unless the invocation explicitly enables the gated UIA transport and repeats the exact task id as confirmation.

Execution additionally requires:

1. a current-worker continuation recommendation using the canonical recovery prompt;
2. two distinct reconciliation samples with sufficient time separation;
3. stable semantic fence token and latest-reconciliation identity;
4. durable LSM session active, with no in-flight call, tracked active job, or continuation already pending;
5. registered workspace present;
6. active current worker with exact registered conversation URL;
7. fresh browser observation showing generation complete and composer ready;
8. required DOM/network quiet conditions;
9. no unresolved prior action attempt;
10. a fresh exact-window worker lease;
11. recovery budget still available.

The resident watchdog does not call this command automatically in 0.6.

## 4. Atomic write-ahead recovery arming

Recovery execution persists the `ARMED` action and consumes one recovery-budget slot in the same SQLite transaction before any external submission is attempted.

The canonical recovery text is not stored. The wire turn appends a non-secret per-attempt marker derived from the durable nonce. The wire prompt hash is stored, allowing a restart to reconstruct exactly the intended turn while preventing a different prompt from being substituted.

Any post-draft or uncertain submission result remains unresolved and blocks another send.

## 5. Positive acknowledgement

`action-reconcile-uia` does not fabricate success. It requires a fresh exact-window lease and observes bounded metadata only. An action is acknowledged only when:

- the attempt still belongs to the active current worker;
- the signed-in conversation is the exact registered URL/window;
- the known per-attempt marker occurs exactly once;
- generation is complete;
- a non-empty page text signature is available.

Until that proof exists, the action remains unresolved and duplicate dispatch stays blocked.

## 6. Deliberate non-goals

0.6 does not:

- auto-send from the watchdog;
- auto-close a live/generating/LSM-active worker page;
- treat a stale HWND or stale probe slot as authority;
- open a second probe window because the first slot is ambiguous;
- use private web chat transport endpoints;
- move browser sign-in state between profiles;
- interpret a capability from another browser major/surface/evaluator version as valid.

The next safe milestone can add crash-fenced probe-slot mutation operations and watchdog scheduling around the already-explicit executor, but only after those transitions have the same write-ahead/reconciliation guarantees as action dispatch.
