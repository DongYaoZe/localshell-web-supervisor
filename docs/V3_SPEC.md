# V3 specification: deterministic dry-run recovery dispatch

V3 closes the loop from observation to an auditable action decision without enabling the action transport itself.

## Private transport decision

CWS does not build a private ChatGPT backend client. See `V3_DECISION.md` for the evidence and revisit criteria.

## Semantic fence correction

Reconciliation records retain observation timestamps for audit, but timestamps that merely describe *when a sample was taken* are excluded from `fence_token` material.

The fence still includes time values that are themselves part of action safety, such as:

- last DOM change baseline;
- network quiet baseline/last activity;
- Goal last-agent activity;
- in-flight heartbeat/event timing.

Therefore two independently sampled records can match when the actionable world has not changed, while a new worker/message signature, Git state, LSM run/plan/continuation, or other safety fact invalidates the fence.

Fence semantics are versioned. Historical V1 records without an explicit version load as
fence version 1; V3 semantic records are version 2. Cross-version records never match, so
an upgrade safely forces a new two-phase reconciliation.

## Mandatory two-phase confirmation

`cws dispatch-plan TASK` never acts. It produces an audited `DispatchPlan` after:

1. loading the previous reconciliation record;
2. refreshing the current browser/LSM/workspace state;
3. persisting a second reconciliation record;
4. comparing the two semantic fences.

Two-phase confirmation is mandatory. There is no production CLI option to reduce it to one sample.

Default gates include:

- both reconciliations are fresh (120 s default maximum age);
- the records have distinct IDs;
- sample separation is at least 3 s by default;
- `fence_token`s are identical;
- task state is `SUSPECT` or `RECONCILING`;
- recovery attempt budget remains;
- the current worker ID has not changed;
- the registered worker is still marked active;
- the registered conversation URL is present and exactly matches the observed browser URL;
- the browser observation itself is fresh (30 s default maximum age);
- matching LSM session is active;
- LSM has zero in-flight tool calls;
- no tracked active job exists;
- no LSM continuation is pending;
- registered workspace exists;
- current browser worker is positively observed;
- browser is not generating;
- Send/composer readiness is positively observed;
- DOM is quiet for at least 5 s by default;
- when network telemetry exists, network lifecycle is quiet for at least 5 s by default.
- when network telemetry exists, the network observation itself is fresh (30 s default maximum age).

A missing network signal is allowed because the user-owned normal Chrome has no required CDP endpoint; LSM/browser/workspace fences still apply.

## Output states

### Observe / no action

If the ordinary recovery recommendation says work is still active, `dispatch-plan` returns `OBSERVE` and never evaluates an action transport.

### Current-worker candidate

If every deterministic precondition passes, V3 may report:

```text
action=CONTINUE_CURRENT_WORKER
candidate_ready=true
transport_enabled=false
would_dispatch=false
```

This means the deterministic control layer considers the world stable enough for a future action adapter. It does **not** mean CWS sent anything.

### Takeover design

If the old browser worker is not positively observed, the conceptual action becomes `TAKEOVER_NEW_WORKER`, but `candidate_ready` remains false. UI disappearance is insufficient proof for replacement.

A future takeover requires an explicit newly bound conversation worker plus LSM's supported `session_manage resume(... takeover=true)` transition after the same fence is revalidated.

### Human decision

Blocked Goal plans, exhausted budgets, missing durable identity, inconsistent fences, or other ambiguity remain non-automatic.

## Action transport is absent

The CLI hard-codes `transport_enabled=false`. There is no flag to enable it.

`execute_dispatch()` unconditionally raises `DispatchDisabled`. V3 does not ship a browser click/type implementation and does not invoke LSM takeover.

Dry-run plans are appended to the existing recovery audit history so the supervisor can later explain why a proposed recovery was blocked or considered a candidate.

## Example

```powershell
# First sample / fence
python -m cws reconcile TASK --uia

# After a stability interval, refresh and dry-run the action gate
python -m cws dispatch-plan TASK --uia

# No action is sent. Inspect the audit trail instead.
python -m cws recovery-history TASK
```

## Transition to an action-capable future version

An action adapter should be considered only after an isolated user-authenticated disposable ChatGPT conversation proves:

- exact worker identity survives the selected transport;
- a Continue/new-turn action can be positively correlated to the intended worker;
- page-close/reopen semantics are understood if parking is involved;
- duplicate dispatch can be fenced across crashes/restarts;
- a successful dispatch has a durable acknowledgement/attempt ID before CWS enters `RECOVERING`.

Until those invariants are demonstrated, V3 stops at deterministic dry-run planning.
