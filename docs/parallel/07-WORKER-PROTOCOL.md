# 0.7-D — multi-conversation worker lease protocol

Assigned worktree: `D:\Documents\tools\cws-wt-07-worker`

Assigned branch: `agent/07-worker-protocol`

## Objective

Define and implement a pure protocol layer for multiple ChatGPT conversations acting as replaceable worker leases for durable CWS tasks. This is groundwork for later self-hosted child-session supervision; it must not automate ChatGPT Web creation yet.

## Protocol concepts

Model deterministic transitions for:

- worker registration against durable task identity;
- candidate worker claim;
- active lease heartbeat/freshness;
- supersede/handoff request;
- takeover eligibility;
- old worker fencing after supersession;
- worker completion vs durable task completion;
- abandoned conversation with task still recoverable;
- duplicate or late heartbeat from a superseded worker;
- multiple workers racing to claim the same task;
- parent/child task relationship metadata sufficient for a future supervisor tree without binding durable identity to a browser tab.

The protocol should expose pure decisions/events that a later registry adapter can persist atomically. Include monotonic generation/epoch or equivalent fencing so a stale worker cannot regain authority merely by sending a late heartbeat.

## File ownership / conflict avoidance

Prefer a new `src/cws/worker_protocol.py` plus dedicated tests and a short spec document. Reuse existing model types where clean, but avoid `db.py`/`registry.py` migration changes in this branch. If persistence changes are required later, describe the exact adapter fields/events for the integrator rather than editing schema here.

## Explicit non-goals

- Do not create ChatGPT conversations.
- Do not open/close browser windows.
- Do not start watchdog or background services.
- Do not add automatic recovery dispatch.
- Do not use OpenAI API/private endpoints.

## Deliverable

A committed pure worker-lease state machine/spec with exhaustive tests for stale workers, takeover, handoff, races, and completion semantics.
