# LWS 0.9 same-worker timeout autopilot

LWS 0.9 closes the operator loop for one narrow failure class: an already-registered current web chat worker visibly reports a recognized delivery error such as `Message delivery timed out`, while durable Local Shell MCP and workspace evidence show that no local work is still in flight.

This feature is explicitly opt-in. It does not attempt to prevent the web chat platform from producing a delivery timeout; it removes the need for a human to type `continue` when the supervisor can prove that a same-worker continuation is safe.

## Enabling

Foreground:

```powershell
python -m lws watch --uia --auto-recover-timeouts
```

Detached resident host:

```powershell
python -m lws watchdog-start --auto-recover-timeouts
```

The detached form automatically enables exact-window UIA observation. Tasks must already exist in the selected LWS registry with the correct current conversation URL and durable LSM logical-session identity.

## Recovery ladder

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
