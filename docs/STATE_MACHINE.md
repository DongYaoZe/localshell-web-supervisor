# State machine

LWS uses task states, not web chat message states.

```text
QUEUED -> STARTING -> RUNNING
                     |   |
                     |   +--> COMPLETED
                     |
                     +--> SUSPECT -> RECONCILING -> RECOVERING -> RUNNING
                                      |       |            |
                                      |       |            +--> NEEDS_HUMAN
                                      |       +--> BLOCKED
                                      +--> COMPLETED

Any nonterminal state may be explicitly ABANDONED.
```

## Meanings

- `QUEUED`: durable task exists but no execution evidence yet.
- `STARTING`: a worker/session is being attached.
- `RUNNING`: fresh browser or, preferably, durable LSM evidence shows progress.
- `SUSPECT`: freshness thresholds were crossed but the system has not reconciled side effects.
- `RECONCILING`: UI/transport evidence conflicts with or is insufficient relative to durable execution state.
- `RECOVERING`: a fenced recovery/takeover action has actually been dispatched and durably acknowledged. V3 still does not enter this state automatically because the action transport is absent.
- `BLOCKED`: Goal plan or known prerequisite explicitly blocks progress.
- `NEEDS_HUMAN`: ambiguity or recovery budget requires a real decision.
- `COMPLETED`: durable LSM session/plan evidence proves completion and no tracked active work remains.
- `ABANDONED`: explicitly cancelled/abandoned durable task.

## Classification invariants

1. A live LSM in-flight call or tracked active job wins over an apparently idle web chat page: classify `RUNNING`.
2. Browser delivery error => `RECONCILING`, not automatic replay.
3. Send/composer ready + pending tool card + prolonged DOM silence => contradictory lifecycle => `RECONCILING`.
4. Active LSM continuation pending => `RUNNING`; do not race it.
5. Goal execution lease due with no durable work in flight => `SUSPECT` and reconcile.
6. Browser and LSM silence beyond thresholds => `SUSPECT`; optional network silence can strengthen confidence, while recent network activity creates a reconciliation conflict rather than proving progress.
7. Durable completed LSM session, or completed plan with no active tracked work => `COMPLETED`.
8. Recovery without a durable LSM identity is not safe enough for automation.
9. A future recovery dispatch must pass two fresh, distinct, sufficiently separated reconciliations with an identical semantic fence immediately before action.
10. In V3, passing the dispatch gate is still dry-run only; it does not transition the task to `RECOVERING`.

## Why no `continue` state

`continue` is an action on a worker, not a durable task state. The action may start a new web chat turn while the previous message-delivery lifecycle is broken. LWS therefore models it as a possible recovery dispatch after reconciliation, not as a state transition shortcut.
