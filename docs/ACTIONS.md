# Action adapter and crash fencing

CWS 0.4 control-plane work introduces a durable action-attempt protocol but still ships **no production ChatGPT action transport**.

The design goal is simple: a supervisor crash must never turn an uncertain previous send into an automatic duplicate send.

## Write-ahead rule

Before any future browser adapter is allowed to click/type/submit, it must persist an `ActionAttempt` in state `ARMED`.

The durable record contains only control metadata:

- task and worker ids;
- reconciliation fence token/version;
- prompt SHA-256, not prompt text;
- random local nonce;
- pre-action message signature if available;
- timestamps and transport/ack metadata.

SQLite schema v2 has a partial unique index that permits at most one unresolved attempt per task across:

- `ARMED`
- `SUBMITTED`
- `RECONCILE_REQUIRED`

This is a database invariant, not only a Python check.

## State meanings

`ARMED` means the intent was durably recorded before side effects. It is already a duplicate-send lock. A crash after an external click but before the next database write can therefore not be interpreted as "nothing happened".

`SUBMITTED` means the transport positively reported that the submit action happened. It is still unresolved until observation acknowledges the new turn.

`RECONCILE_REQUIRED` means the transport raised, disconnected, crashed, or otherwise could not prove the side-effect result. Automatic retry is forbidden.

`ACKNOWLEDGED` is terminal and means positive evidence tied to the same attempt/worker was observed.

`FAILED` is terminal only when the transport can prove no side effect occurred, for example a required selector was absent before any click.

`CANCELLED` is an explicit local/operator decision. Cancellation releases the duplicate-send lock but cannot undo an external side effect that may already have happened.

## Transport contract

A future adapter implements `ActionTransport.submit(ActionIntent)` and returns:

- whether submission was positively observed;
- whether a side effect may have happened;
- transport name;
- bounded diagnostic detail.

A generic exception is treated conservatively as `RECONCILE_REQUIRED`.

The built-in `DisabledActionTransport` raises before side effects and leaves the attempt `ARMED`. There is no production browser transport in this milestone.

## Acknowledgement contract

Acknowledgement is separate from submission. A future browser observer must provide positive evidence bound to:

- the same `attempt_id`;
- the same worker;
- an observation timestamp after arming;
- a non-empty evidence kind/hash.

CWS does not expose a CLI that lets an operator simply type `ack=true`. This avoids turning acknowledgement into a manual bypass.

For an actual ChatGPT adapter the acknowledgement evidence should be stronger than "click returned success". Candidate evidence includes a new visible user turn or another stable DOM/message identity emitted after the action. The exact adapter-specific proof remains gated on the disposable authenticated browser experiment.

## Planner feedback

An unresolved action feeds back into `dispatch-plan`. Even if the two reconciliation fences would otherwise make `candidate_ready=true`, CWS forces the plan closed until the previous action is acknowledged/cancelled/reconciled.

`cws doctor --task TASK` also reports unresolved action attempts as a warning.

## Operational commands

```powershell
python -m cws action-status TASK
python -m cws action-cancel ATTEMPT --reason "human reconciliation completed"
```

These commands never send or acknowledge ChatGPT messages. `action-cancel` affects only the local lock and explicitly does not claim to undo an external action.

## Schema migration

Registry schema version 2 adds `action_attempts` and the unresolved-attempt unique index. Existing version-1 registries are upgraded in place by additive schema creation; existing task/worker data are preserved. Unknown future schema versions still fail closed.
