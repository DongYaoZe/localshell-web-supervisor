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

- exact-URL Windows UI Automation for the user-authorized selected tab in normal Chrome;
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

## RAM / concurrency model

The machine target can be as small as ~8 GB RAM, so the control plane should not equate one task with one permanent Chromium tab.

V2 target topology:

- small active-worker page pool;
- one or a few probe pages reusing a browser context/profile to inspect parked conversation URLs sequentially;
- parked conversations stored as durable URLs/worker records, not resident tabs;
- resource blocking for probes where it does not break ChatGPT state detection;
- explicit experiments to determine whether closing an executing page affects server-side generation/tool delivery.

Until those experiments exist, CWS must not assume a page is safe to close during execution.

The implemented V2 control layer now makes that uncertainty explicit. `pool-plan` pins
any worker with live/ambiguous task or LSM evidence and ranks only terminal/queued/blocked
workers as parking candidates. Page closing is not performed. `PagePool` separately tracks
ephemeral active/probe page leases, fails closed on capacity instead of evicting a page,
and rotates parked worker URLs through a small reusable probe queue. Durable worker/task
identity remains in SQLite, never in the browser page ID.

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
ownership it exits rather than risk duplicate future recovery dispatches. One-shot scans
do not acquire the resident lease.

Production hosting should be independent of a ChatGPT conversation and, on the inspected
Windows v4.0.1 environment, should not assume an LSM tracked shell job owns the full child
process tree. See `LSM_FINDINGS.md` for the bootstrap experiment that motivated this fence.

## Version boundaries

### V0
DOM + LSM durable telemetry + registry + deterministic watchdog + advisory recovery.

### V1
Read-only exact-URL UIA + optional CDP network lifecycle observation + three-signal
classification + durable reconciliation fences. Recovery dispatch remains disabled until
the browser-worker identity/action boundary and revalidation rules are proven in isolation.

### V2
Low-memory worker planning + active/probe page lease pool + parking bookkeeping + RAM
telemetry. Actual ChatGPT page-close/reopen behavior remains an experiment gate; a local
harness validates methodology only and does not justify closing a live ChatGPT worker.

### V3
Minimal Web transport research only if browser orchestration cannot meet reliability goals. Reimplementing ChatGPT private endpoints is explicitly not the default architecture.
