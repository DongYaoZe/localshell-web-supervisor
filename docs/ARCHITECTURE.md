# Architecture

## Problem statement

The execution stack is:

```text
chatgpt-web-supervisor
        |
        v
ChatGPT Web conversation workers
        |
        v
Local Shell MCP
session / plan / jobs / browser
        |
        v
local workspace
```

The failure domains are not aligned. ChatGPT Web message delivery can stall after a local side effect has completed. Conversely, the page can render as idle while Local Shell still has real work in flight. Human monitoring does not scale to dozens of workers.

The supervisor's job is not to execute project work. It owns durable task identity, worker leases, observation, health classification, recovery fencing, and attention scheduling.

## Durable identity

A task persists across workers and contains:

- project / `task_id` / objective;
- cwd/repository;
- Local Shell logical `session_id`;
- Goal plan and checkpoint references;
- tracked Local Shell job IDs;
- current and historical conversation workers;
- browser and LSM heartbeats/observations;
- recovery history and attempt budget.

A conversation is a worker lease with URL/id/status. A new conversation may take over the same durable task after reconciliation and an LSM `session_manage resume(... takeover=true)` transition.

## Source-of-truth hierarchy

No single signal is enough. Evidence is ranked:

1. **Durable LSM/workspace facts** for side effects and execution state.
2. Browser/DOM state for worker liveness and message lifecycle symptoms.
3. V1 network/CDP telemetry for streaming/lifecycle confidence.

A browser `Send` button is weak evidence. An active durable LSM tool lease or tracked job is strong evidence that work is still active.

## Local Shell MCP integration boundary

CWS does not rewrite LSM's lifecycle machinery. In the inspected v4.0.1 deployment LSM already provides:

- durable `sessions/<session_id>.json` including run history, plan, activity, and in-flight calls;
- a 900 s plan execution lease and continuation claim/reservation/report protocol (max 10 attempts);
- fail-closed durable tool-call lease acquisition and 2 h stale in-flight pruning;
- takeover fencing while tool calls are in flight;
- durable tracked-job registry plus per-attempt command/log/status files;
- interrupted job-start/retry reconciliation.

V0's `FileLsmTelemetry` is deliberately read-only. It does **not** become another MCP client and does not mutate LSM private state. Mutations such as takeover must continue to go through LSM's supported tool boundary in a later guarded dispatcher.

The observed hardened deployment stores state at `C:\ProgramData\LocalShellMCP-Hardened\control\state`; this is configuration, not an architectural constant. CWS supports an explicit state-dir override and environment-based discovery.

## Browser integration boundary

LSM's high-level Playwright manager already offers persistent profiles, storage state, page snapshots, DOM actions, page errors, `requestfailed`, and recent response metadata. However its browser-session registry lives in process memory, is capped at 8 sessions, and idle-cleans after one hour. Therefore LSM browser `session_id` is not a durable task identity.

V0 consumes normalized DOM observations. V1 adds two safe observation routes without changing execution transport:

- exact-URL Windows UI Automation for user-authorized top-level windows in normal Chrome; duplicate URL matches fail closed unless an exact HWND is supplied;
- Network-domain lifecycle metadata for CWS-owned or explicitly exposed CDP pages.

V1 records timestamps for:

- request/fetch/XHR start and completion/failure;
- streaming response activity / byte progress when observable;
- WebSocket events if ChatGPT uses them for the relevant lifecycle;
- last network activity and network silence;
- DOM mutation/activity silence;
- composer and tool-card contradiction states.

It does not retain request/response bodies, auth headers/cookies, or private ChatGPT payloads. Observation comes before transport reimplementation.

## Heartbeat model

A task has multiple clocks:

- `browser.observed_at` — probe freshness;
- `browser.last_dom_change_at` — UI activity;
- LSM recent activity event timestamp;
- Goal plan `last_agent_activity` and its execution lease;
- in-flight tool-call heartbeat;
- tracked job update/status timestamps;
- V1 network activity and conservative continuous-quiet baseline.

A heartbeat is evidence, not a completion marker. CWS classifies from the combination.

## Reconcile and recovery protocol

Recovery is two-phase:

1. **RECONCILE** — inspect durable LSM session/plan/jobs and actual repository/filesystem checkpoint. Determine whether prior side effects already happened.
2. **RECOVER** — only after reconciliation, resume the first genuinely incomplete step. If the old worker cannot recover, create/activate a replacement worker and use LSM's supported takeover semantics.

Hard fences:

- never takeover while an LSM in-flight tool lease is live;
- never compete with an LSM continuation already pending;
- never replay a tool merely because its UI card is still `ing`;
- cap recovery attempts and escalate ambiguity to `NEEDS_HUMAN`.

V1 makes `RECONCILE` durable. A reconciliation record stores a sanitized snapshot of
worker identity, browser signature/state, optional network timing, LSM run/plan/in-flight
facts, checkpoint digest, and Git/workspace digest. The canonical snapshot produces a
deterministic `fence_token`. A future action must refresh the evidence and prove the fence
still matches immediately before dispatch; a fence is not itself permission to act.

## Write-ahead action fence

The 0.4 control plane adds a second fence around any future external worker action. A
candidate recovery is not allowed to move directly from a matching reconciliation fence
to a browser mutation. It must first create a durable `ActionAttempt` in SQLite.

The attempt is written **before** any external side effect and starts in `ARMED`. This is
important for the crash window where a browser click might happen but the process dies
before it can write a success record. On restart, `ARMED` is treated as unresolved, not as
proof that nothing happened.

Unresolved states are `ARMED`, `SUBMITTED`, and `RECONCILE_REQUIRED`. Registry schema v2
introduced the partial unique index so one durable task cannot have two unresolved attempts,
even if two supervisor processes race. Transport exceptions or ambiguous outcomes become
`RECONCILE_REQUIRED`; only a transport-proven no-side-effect outcome may become terminal
`FAILED`. Positive observation bound to the same attempt/worker is required before
`ACKNOWLEDGED` releases the duplicate-send lock.

0.5 added schema-v3 short-lived worker-window leases binding worker id to exact conversation
URL, top-level HWND, Chrome PID/executable, observation time, and expiry. HWND is not durable
identity: leases expire quickly and are cleared when a worker is superseded/parked/dead. The
UIA sender/ACK observer requires a fresh binding and revalidates the same identity immediately
before mutation. It is reachable only through the explicit one-shot fenced executor;
the resident watchdog still cannot auto-enable it, and no CLI shortcut can fabricate
acknowledgement. See `ACTIONS.md` and `V4_SPEC.md`.

## RAM / concurrency model

The machine target can be as small as ~8 GB RAM, so the control plane should not equate one task with one permanent Chromium tab.

V2 target topology:

- small active-worker page pool;
- one or a few probe pages reusing a browser context/profile to inspect parked conversation URLs sequentially;
- parked conversations stored as durable URLs/worker records, not resident tabs;
- resource blocking for probes where it does not break ChatGPT state detection;
- explicit experiments to determine whether closing an executing page affects server-side generation/tool delivery.

0.5 now has authenticated ChatGPT-specific evidence for two narrow continuity properties: a
pure model response continued with no page open, and one harmless Local Shell MCP job that
was durably running at close later succeeded and resumed correctly after reopen.

`PageCloseEvidence` makes the proof machine-checkable and separates ordinary generation/page
continuity from live-tool continuity. Evidence may use a normally authenticated dedicated
profile or an exact-bound disposable top-level window in the existing authenticated profile;
copied-auth, anonymous, localhost-only, ambiguous-window, duplicate-turn, and unchanged-
signature cases fail closed. `--require-tool` additionally requires exact job identity,
running-at-close, completed-after-close, and final-response evidence. See `EXPERIMENTS.md`.

The implemented V2 control layer makes that uncertainty explicit. `pool-plan` pins any
worker with live/ambiguous task or LSM evidence and ranks only terminal/queued/blocked workers
as parking candidates. Non-live `close_allowed` advice still requires explicit local
capability selection (or the legacy one-shot evidence input); live LSM and active generation
remain unconditionally pinned. Page closing is not performed. `PagePool` separately tracks
ephemeral active/probe page leases, while schema v4 added one durable reusable probe-slot
record. Schema v5 adds one globally unresolved write-ahead probe mutation operation for
`OPEN`, `ROTATE`, and `CLOSE`. Same-target probes reuse the slot; different targets require
exact-close-before-open; a crash after a submitted phase is reconciled before further
authority is issued; stale, multiple, changed, or incomplete ownership evidence blocks rather
than causing page proliferation. Durable
worker/task identity remains in SQLite, never in the browser page ID.

System and aggregate Chrome working-set telemetry provide pressure evidence, but Chrome's
multi-process model means a single window-process working set is not attributed to one tab.
Memory pressure can change ranking/attention; it cannot override a `DO_NOT_CLOSE` liveness
fence.

## Human-attention scheduling

The output of the watchdog is an **attention queue**, not a mirror of every running worker. Healthy `RUNNING` and terminal tasks stay quiet. Priority is roughly:

1. `NEEDS_HUMAN`
2. `BLOCKED`
3. `RECONCILING`
4. `SUSPECT`

This converts the user's role from polling dozens of conversations to resolving only ambiguous or decision-requiring cases.

## Supervisor process lease

The supervisor is itself a control-plane process, so duplicate resident watchdogs are a
safety issue. `cws watch` owns a renewable SQLite lease scoped to the registry. A second
resident watchdog refuses to start while the lease is fresh; if a running watchdog loses
ownership it exits rather than risk duplicate future control-plane actions. One-shot scans
do not acquire the resident lease.

The 0.4 host layer can launch this same watcher as an independent detached Python process.
A random `host:<uuid>` lease-owner token, rather than launcher PID equality, proves startup;
this matters on Windows where a virtual-environment Python wrapper PID can differ from the
resident Python PID. `watchdog-status` reports the lease PID and liveness.

Stop is cooperative rather than process-tree killing. `watchdog-stop` atomically replaces
the owner with a fresh `stop:<uuid>` lease. The old watcher then fails its next heartbeat
and exits by its existing safety path, while the fresh stop lease prevents a replacement
from racing shutdown. Once the resident PID disappears, the stop lease is cleared.

Production hosting is therefore independent of a ChatGPT conversation and does not rely
on an LSM tracked shell job owning the full Windows descendant process tree. See
`LSM_FINDINGS.md` for the bootstrap experiment and `OPERATIONS.md` for host commands.

## Version boundaries

### V0
DOM + LSM durable telemetry + registry + deterministic watchdog + advisory recovery.

### V1
Read-only exact-URL UIA + optional CDP network lifecycle observation + three-signal
classification + durable reconciliation fences. Recovery dispatch remains disabled until
the browser-worker identity/action boundary and revalidation rules are proven in isolation.

### V2
Low-memory worker planning + active/probe page lease pool + parking bookkeeping + RAM
telemetry. The original V2 layer kept ChatGPT page-close as an experiment gate. 0.5 later
produced authenticated continuity evidence; 0.6 stores that evidence as expiring, versioned,
deployment-scoped capabilities. The V2 planner still does not close live workers automatically.

### V3
Evidence-based NO-GO on private ChatGPT endpoint reimplementation. V3 instead added a
mandatory two-sample semantic fence and deterministic dry-run dispatcher. Normal
`dispatch-plan` behavior still has `transport_enabled=false` / `would_dispatch=false`.
A separate explicit one-shot executor may enable the exact-window UIA module only after
all V3 semantic fences plus action/window/task-confirmation/budget checks pass. The
resident watchdog does not call it automatically. Takeover remains blocked until a
separately bound replacement worker and LSM-supported takeover transition can be proven
safely in isolation. See `V3_DECISION.md`, `V3_SPEC.md`, and `V4_SPEC.md`.

### 0.4 control-plane milestone
Durable write-ahead action attempts, schema-v2 duplicate-action fencing, strict isolated
page-close evidence evaluation, and independent watchdog hosting/cooperative stop were
implemented.

### 0.5 exact-window evidence milestone
Authenticated same-profile disposable-window experiments proved pure-generation continuity
and one harmless live-LSM-job continuity across close/reopen. UIA observation rejects
ambiguous same-URL windows unless exact top-level window identity is available. Schema v3
added short-lived worker-window leases and the gated exact-window UIA sender/ack observer.

### 0.6 reusable-page and explicit-execution milestone
Schema v4 adds durable versioned page-capability provenance and an at-most-one reusable
probe-slot record. Recovery prompts carry a durable per-attempt marker, and ARMED recovery
state plus budget consumption are committed atomically before submission. `dispatch-execute`
is an explicit one-shot current-worker continuation behind exact task confirmation and all
existing semantic/LSM/workspace/window/action fences; `action-reconcile-uia` releases the lock
only from positive single-turn completion evidence. Normal planning remains dry-run, the
resident watchdog does not auto-dispatch, and live-worker page eviction remains disabled.

### 0.7 crash-fenced probe and worker-orchestration milestone
Schema v5 adds write-ahead probe-window operations and a database-level one-unresolved-
mutation invariant. Pure reconciliation distinguishes exact absence, unique owned target,
old target still present, old+new present, multiple matches, stale/changed identity, and
unknown observation; ambiguous states cannot authorize another open/close. The orchestration
layer evaluates fairness, cooldown, recovery budget, LSM/workspace freshness, semantic fences,
window leases, and capability provenance but always returns `mutation_allowed=false`. A pure
multi-conversation worker protocol adds revision/generation fencing for registration, claim,
heartbeat, handoff, takeover, supersession, abandonment, completion, and task lineage. See
`V5_SPEC.md` and `WORKER_PROTOCOL.md`.

### 0.8 durable worker-orchestration milestone
Schema v6 persists the worker protocol in additive task/lease/event tables. Every authority
transition is revision-CAS protected under `BEGIN IMMEDIATE`; generation ownership survives
process restart and ambiguous legacy worker combinations fail closed during bootstrap. The
runtime orchestration adapter refreshes LSM/workspace evidence read-only and carries global
probe/action fences into the pure planner. Adversarial closure additionally rejects duplicate
task inputs, future exact-window/probe evidence, future probe observations, and wall-clock
heartbeat rollback. Automatic ChatGPT conversation creation, watchdog auto-dispatch, and
live-worker auto-close remain disabled in 0.8. See `V6_SPEC.md`.

### 0.9 same-worker timeout-autopilot milestone
The resident watchdog gains one explicit mutation mode for recognized ChatGPT Web delivery
errors. It reuses the existing exact current-worker window and the existing fenced executor;
it does not add a second send path. Before submission it still requires two stable semantic
reconciliation samples, durable LSM/workspace agreement, a fresh exact-window binding, no
unresolved action, remaining recovery budget, and a cooldown. A positive nonce/hash ACK keeps
the duplicate-send lock authoritative, while a stored normal-browser ACK signature prevents
an unchanged old error banner from retriggering recovery. At most one possible external send
is attempted per watchdog cycle. Generic retries, new-conversation creation/takeover, and
live-worker page closing remain disabled. See `AUTOPILOT.md`.
