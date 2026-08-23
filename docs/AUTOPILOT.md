# Same-worker watchdog autopilot

LWS supports two explicitly opted-in same-worker continuation triggers. Delivery-error recovery handles recognized failures such as `Message delivery timed out`. Hard-overrun continuation handles the separate case where one managed work turn has exceeded a configured wall-clock limit (default 1520 seconds = 25m20s) without durable completion.

Neither mode treats `continue` as idempotent. Both reuse the exact-window write-ahead action protocol so an ambiguous previous send is reconciled rather than replayed.

## Enabling

Foreground:

```powershell
python -m lws watch --uia --auto-recover-timeouts --auto-continue-overruns --overrun-after 1520
```

Detached resident host:

```powershell
python -m lws watchdog-start --auto-recover-timeouts --auto-continue-overruns --overrun-after 1520
```

The detached form automatically enables exact-window UIA observation. Tasks must already exist in the selected LWS registry with the correct current conversation URL. Delivery-error recovery additionally depends on the durable LSM logical-session/workspace evidence described below.

## Hard-overrun ladder

For `--auto-continue-overruns`, the watchdog uses a per-conversation turn clock. Before evaluating timers it performs a read-only normal-Chrome discovery pass over top-level windows, keeping only `chatgpt.com/.../c/...` active address-bar URLs and bounded HWND/PID identity. Each visible conversation receives one durable `watch-chat-<conversation-id>` owner; any older task aliases for the same URL are deduplicated behind that owner. The clock anchor is the newest of the watcher worker start, the most recent effective LWS continuation, and the start of the most recent observed manual generation episode. Streaming DOM/signature changes inside one generation do not keep resetting this clock. Discovery itself does not read conversation text, cookies, tokens, or inactive-tab URLs.

During the lead window before the deadline the watchdog records a reconciliation sample but does not send. After the deadline it requires a second fresh sample with the same semantic fence, the same active current worker, the exact registered URL/HWND/PID/Chrome identity, a fresh browser observation, a usable composer, and generation no longer active. Freshness comparisons use a wall-clock sample taken after the current UIA/reconciliation evidence is recorded, so live evidence cannot be rejected merely because it was collected milliseconds after the scan began. `COMPLETED`, `ABANDONED`, `BLOCKED`, and `NEEDS_HUMAN` states suppress the nudge.

The action is persisted before submission. `SUBMITTED`, `RECONCILE_REQUIRED`, or later `ACKNOWLEDGED` state resets the turn clock durably, so a watchdog restart cannot replay the same nudge. A proven pre-send failure does not reset the clock, but a retry cooldown applies; this includes a rate-limit modal that was detected/dismissed before Send. Hard-overrun nudges are maintenance actions and do not consume the bounded fault-recovery budget. At most one possible browser send is attempted per watchdog cycle across the overrun and delivery-error paths.

## Delivery-error recovery ladder

For a recognized delivery error, one watchdog cycle does the following:

1. refresh the exact registered current-worker window through read-only UIA;
2. refresh durable Local Shell MCP session/plan/job state;
3. refresh actual workspace/Git state;
4. reconcile any unresolved prior action before considering another send;
5. record a new sanitized reconciliation sample;
6. require two distinct, fresh, sufficiently separated samples with the same semantic fence;
7. require the current active worker, fresh exact-window binding, no in-flight LSM tool/job/continuation, unchanged reconciled workspace, no unresolved action, and remaining recovery budget;
8. require the visible error to be one of the recognized delivery-error markers;
9. require the resident recovery cooldown to have elapsed;
10. atomically persist a new `ARMED` action and consume one recovery-budget slot before external submission;
11. submit only the canonical current-worker recovery prompt through the exact bound window;
12. stop after at most one submission whose side effect may have happened in that watchdog cycle;
13. on later cycles, keep the action locked until positive exact-window nonce/hash acknowledgement is available.

If any gate is missing, stale, contradictory, future-dated, or ambiguous, the watchdog observes/reconciles instead of sending.

## Replay suppression

A successful nonce acknowledgement is not itself assumed to clear an old visible web chat error banner. After positive ACK, LWS stores the normal browser observation signature for that acknowledged UI state. If the same signature is observed again, the old banner is treated as already handled and cannot trigger another recovery action.

Separately, if one contiguous visible-error episode ever coexists with `generating=True`, LWS treats that banner as stale/ambiguous for the rest of that episode. That suppression does not expire after a fixed number of samples: it scans the full retained browser-observation window until the banner clears or changes. If the episode start itself has aged out of retention, recovery fails closed instead of assuming the error is fresh.

A changed UI signature may become a new candidate only after the cooldown and all ordinary reconciliation fences pass again.

## What remains disabled

0.9 does not enable:

- generic unattended retries for arbitrary UI errors;
- blind replay of a prior user message;
- automatic replacement-conversation creation;
- automatic worker takeover/new-generation browser creation;
- automatic live-worker page eviction;
- copied browser authentication;
- private web chat endpoints or protocol reconstruction.

Repeated failure beyond the bounded recovery budget still escalates to human attention. Ambiguous external side effects remain locked in `ARMED`, `SUBMITTED`, or `RECONCILE_REQUIRED` until reconciliation proves an outcome.
