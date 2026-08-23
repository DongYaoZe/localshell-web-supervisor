# Action adapter and crash fencing

LWS keeps the exact-window Windows UI Automation transport gated by default. It is wired to the explicit one-shot `dispatch-execute` command and to two separately opted-in resident paths: recognized delivery-timeout recovery and hard wall-clock overrun continuation. The default watchdog remains advisory.

The design goal remains unchanged: a supervisor crash must never turn an uncertain previous send into an automatic duplicate send.

## Write-ahead rule

Before any external browser mutation, LWS persists an `ActionAttempt` in state `ARMED`.

The durable record contains control metadata only:

- task and worker ids;
- reconciliation fence token/version;
- wire-prompt SHA-256, not recovery prompt text;
- random local nonce and prompt-protocol version;
- pre-action message signature when available;
- timestamps and transport/ack metadata.

SQLite enforces at most one unresolved attempt per task across:

- `ARMED`
- `SUBMITTED`
- `RECONCILE_REQUIRED`

This is a database invariant, not only a Python check.

## State meanings

`ARMED` means the intent was durably recorded before side effects. It is already a duplicate-send lock. A crash after a browser action but before the next database write cannot be interpreted as "nothing happened".

`SUBMITTED` means the transport positively reported that the submit action returned. It remains unresolved until independent observation acknowledges the turn.

`RECONCILE_REQUIRED` means the transport raised, disconnected, timed out, or reached an ambiguous side-effect window. Automatic retry is forbidden.

`ACKNOWLEDGED` is terminal and means positive evidence tied to the same attempt/worker was observed.

`FAILED` is terminal only when the transport can prove no external side effect occurred.

`CANCELLED` is an explicit local/operator decision after reconciliation. It releases the duplicate-send lock but cannot undo an external effect.

## Exact-window UIA transport

`ChromeUiaActionTransport` remains disabled by default. There is still no generic `lws send` or `lws continue` command. `dispatch-execute` enables the adapter for one explicitly confirmed invocation. Resident `--auto-recover-timeouts` may use the adapter only after a recognized delivery error and its deterministic recovery fences pass. Resident `--auto-continue-overruns` may use it only after the hard work-turn deadline and its separate two-sample/current-worker/exact-window/composer fences pass.

The preferred construction path is a fresh `WorkerWindowBinding` recorded by exact-URL UIA observation. Current registry migrations retain the schema-v3 short-lived lease containing:

- worker id;
- native HWND;
- browser PID;
- Google Chrome executable path;
- exact registered conversation URL;
- observation/bind/expiry timestamps.

Bindings are cleared when a worker is parked, superseded, or dead. A stale binding is not usable for mutation. Even with a fresh lease, the transport revalidates the live window immediately before action.

The PowerShell helper requires:

1. exact HWND;
2. `Chrome_WidgetWin_1`;
3. exact Google Chrome executable;
4. exact address-bar URL using `AutomationId=view_1012`;
5. an existing `/c/` conversation;
6. signed-in/profile evidence with no login controls;
7. `prompt-textarea`;
8. no pre-existing ready draft.

The canonical recovery prompt is rendered with one non-secret `LWS-ACTION-<nonce>` idempotency marker before hashing/submission. The original recovery text is not persisted. The wire text is passed to the child process outside the command line and is reconstructed only from the canonical prompt plus the durable nonce.

After `ValuePattern.SetValue(prompt)`, web chat may update its application-level composer state asynchronously. A real isolated smoke showed the Send control can appear later than 300 ms. The adapter therefore polls for at most 2 seconds, every 50 ms, revalidating the exact URL and requiring a positive enabled/on-screen `composer-submit-button`. If readiness never appears, the outcome is side-effect-ambiguous and requires reconciliation.

Only after that positive proof does the transport invoke Send.

## Real crash-window findings

The 0.5 experiments exercised several failure windows:

- a script failed after normal keyboard input changed the draft but before the post-check; reconciliation found the draft and input was not replayed;
- an action remained durably `ARMED` while a browser Send had already been accepted; a fresh process observed the resulting conversation state and only then acknowledged the action;
- the first concrete UIA adapter smoke produced `RECONCILE_REQUIRED` when the readiness wait was too short, preserving the duplicate-send fence;
- after the bounded readiness fix, a fresh action followed the real path `ARMED -> SUBMITTED -> ACKNOWLEDGED`.

These are the intended semantics, not exceptional escape hatches.

## Acknowledgement observer

`ChromeUiaAckObserver` is separate from submission. It observes an exact worker/window and returns bounded metadata only:

- worker/window identity;
- signed-in state;
- generation state;
- Send readiness when positively visible;
- occurrence count of an expected local nonce;
- text-element count;
- SHA-256 text signature.

It does not return conversation text or transport payload content.

`acknowledgement_from_uia_observation()` requires the same worker and a positive evidence hash. Callers can require generation completion and set minimum/maximum nonce-occurrence bounds.

Accessibility trees may expose the same visible turn multiple times, so nonce occurrences are a **bounded duplicate fence**, not an exact count of user turns. Stable signatures across independent observations are stronger than one instantaneous sample.

`action-reconcile-uia ATTEMPT` invokes this observer and marks the attempt acknowledged only when the known marker is observed exactly once and generation is complete. There is still no CLI that fabricates `ack=true`. In 0.9, after a positive ACK, LWS also records the comparable normal-browser message signature in existing action metadata; an unchanged acknowledged UI state cannot retrigger resident timeout recovery.

## Planner feedback

An unresolved action feeds back into `dispatch-plan`. Even if two reconciliation fences would otherwise make `candidate_ready=true`, the planner forces the action closed until the unresolved attempt is acknowledged, cancelled after reconciliation, or otherwise resolved.

`lws doctor --task TASK` reports unresolved action attempts and exact-window lease state.

## Operational inspection

```powershell
python -m lws action-status TASK
python -m lws action-reconcile-uia ATTEMPT
python -m lws action-cancel ATTEMPT --reason "human reconciliation completed"
```

`action-status` is read-only. `action-reconcile-uia` can only acknowledge from positive exact-window evidence. `action-cancel` affects only the local duplicate-send lock.

## Schema milestones

- schema v2: `action_attempts` plus the one-unresolved-action unique index;
- schema v3: short-lived `worker_window_bindings` for exact-window mutation fencing;
- schema v4: durable page-capability provenance and the reusable probe-slot record; recovery execution also uses atomic ARMED+budget persistence.
- schema v5: durable write-ahead probe-window mutation operations with one globally unresolved operation fence. This is separate from recovery-message `ActionAttempt` fencing.

Migrations are additive. Unknown future registry schema versions fail closed.

## Explicit execution and narrow resident timeout recovery

The policy layer exposes only a one-shot current-worker continuation. `dispatch-execute` requires:

- fresh, stable two-sample semantic reconciliation fences;
- exact `--confirm-task` match and explicit UIA enable flag;
- no unresolved prior action;
- fresh exact-window binding;
- durable LSM/workspace revalidation with no active LSM work;
- remaining recovery budget;
- canonical recovery prompt;
- the gated transport.

For ordinary fault recovery, the ARMED row and recovery-budget increment are committed atomically before submission. Positive post-action acknowledgement remains a separate observation step. Hard-overrun continuation uses the same ARMED/unresolved/ACK state machine and an atomic current-worker writer, but intentionally does not consume the bounded fault-recovery budget; its periodic 25m20 nudges are a separate maintenance mechanism. The default resident watchdog does not invoke either path. When explicitly enabled, the two resident paths share a one-possible-external-send-per-cycle ceiling, and an unresolved action from either path blocks the other until reconciled.
