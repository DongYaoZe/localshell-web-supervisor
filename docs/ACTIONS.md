# Action adapter and crash fencing

CWS 0.5 contains an **experiment-backed exact-window Windows UI Automation transport module**, but it is still gated and is not wired to a production CLI or the resident watchdog.

The design goal remains unchanged: a supervisor crash must never turn an uncertain previous send into an automatic duplicate send.

## Write-ahead rule

Before any external browser mutation, CWS persists an `ActionAttempt` in state `ARMED`.

The durable record contains control metadata only:

- task and worker ids;
- reconciliation fence token/version;
- prompt SHA-256, not prompt text;
- random local nonce;
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

`ChromeUiaActionTransport` is the first concrete ChatGPT mutation adapter in the repository. It remains disabled by default and there is intentionally no `cws send`, `cws continue`, or watchdog auto-dispatch path in 0.5.

The preferred construction path is a fresh `WorkerWindowBinding` recorded by exact-URL UIA observation. Registry schema v3 stores a short-lived lease containing:

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

Prompt text is passed to the child process through a local environment variable encoded for transport; it is not placed in the command line or persisted in the action record.

After `ValuePattern.SetValue(prompt)`, ChatGPT may update its application-level composer state asynchronously. A real isolated smoke showed the Send control can appear later than 300 ms. The adapter therefore polls for at most 2 seconds, every 50 ms, revalidating the exact URL and requiring a positive enabled/on-screen `composer-submit-button`. If readiness never appears, the outcome is side-effect-ambiguous and requires reconciliation.

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

It does not return conversation text, request bodies, cookies, tokens, or headers.

`acknowledgement_from_uia_observation()` requires the same worker and a positive evidence hash. Callers can require generation completion and set minimum/maximum nonce-occurrence bounds.

Accessibility trees may expose the same visible turn multiple times, so nonce occurrences are a **bounded duplicate fence**, not an exact count of user turns. Stable signatures across independent observations are stronger than one instantaneous sample.

There is no CLI that lets an operator fabricate `ack=true`.

## Planner feedback

An unresolved action feeds back into `dispatch-plan`. Even if two reconciliation fences would otherwise make `candidate_ready=true`, the planner forces the action closed until the unresolved attempt is acknowledged, cancelled after reconciliation, or otherwise resolved.

`cws doctor --task TASK` reports unresolved action attempts and exact-window lease state.

## Operational inspection

```powershell
python -m cws action-status TASK
python -m cws action-cancel ATTEMPT --reason "human reconciliation completed"
```

These commands do not send or acknowledge ChatGPT messages. `action-cancel` affects only the local duplicate-send lock.

## Schema milestones

- schema v2: `action_attempts` plus the one-unresolved-action unique index;
- schema v3: short-lived `worker_window_bindings` for exact-window mutation fencing.

Migrations are additive. Unknown future registry schema versions fail closed.

## Still not production dispatch

0.5 proves that the adapter can work safely on a disposable, exact-bound normal-Chrome conversation. It does **not** automatically authorize recovery turns in ordinary supervised tasks.

Production auto-dispatch still requires an explicit policy layer that combines:

- fresh semantic reconciliation fences;
- no unresolved prior action;
- fresh exact-window binding;
- durable LSM/workspace revalidation;
- recovery budget;
- the gated transport;
- positive post-action acknowledgement.

Until that policy is deliberately wired and tested, resident watchdog operation remains observe/recommend only.
