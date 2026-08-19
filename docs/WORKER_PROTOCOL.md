# Multi-conversation worker lease protocol

This document specifies the pure protocol in `cws.worker_protocol`. It deliberately does not
create ChatGPT conversations, control browser pages, dispatch recovery, or define a registry
schema. A ChatGPT conversation is replaceable worker metadata; `task_id` remains the durable
identity.

## Durable identities and lineage

Each protocol snapshot belongs to one durable task and carries:

- `task_id`;
- `parent_task_id` when the task is a child;
- `root_task_id` for reconstructing an arbitrary-depth supervisor tree;
- optional `child_key` for a stable parent-local role/slot.

Root tasks name themselves as `root_task_id`. Child creation requires the parent's durable
root id explicitly. Conversation references never participate in task identity or fencing.

## Worker lifecycle

A registered worker starts as `candidate`. Registration alone grants no authority.

An initial claim is allowed only when there is no current worker. A successful claim:

1. increments the task's monotonic `generation`;
2. marks the candidate `active` at that generation;
3. records heartbeat/expiry timestamps; and
4. makes that worker the sole current worker.

A heartbeat is authoritative only when all of the following match the current snapshot:

- worker id;
- active status;
- current worker id;
- current generation; and
- a lease that is still fresh at the heartbeat timestamp.

An expired lease cannot be revived by a late heartbeat. Recovery therefore needs a new
candidate and an explicit takeover (or an explicit abandonment followed by a normal claim).

## Handoff and takeover

A fresh active worker may request handoff to a registered candidate. The request does not
change authority. It only makes that exact candidate takeover-eligible before lease expiry.

Without handoff, takeover is eligible only after the current lease expires. A successful
takeover is one transition that:

- increments `generation`;
- marks the old worker `superseded` and records `superseded_by`;
- promotes the candidate to `active` at the new generation;
- clears pending handoff metadata; and
- emits both supersession and takeover events at one protocol revision.

After supersession, old-worker heartbeats or completions are fenced even if they arrive late.
The generation never decreases, so a stale conversation cannot regain authority merely by
sending a new message.

## Abandonment and completion

`abandon_worker` is a reconciliation transition for a disappeared current conversation. It
fences that worker and clears `current_worker_id`, but leaves the durable task `open`. Another
conversation can therefore register and claim the same task at the next generation.

Worker completion and durable task completion are intentionally different transitions:

- `complete_worker` ends only the current fresh worker lease. The durable task remains open.
- `complete_task` requires no active worker and an opaque non-empty `completion_ref` supplied
  by higher-level reconciliation (for example, a workspace/Git proof reference). It is the
  explicit irreversible durable-task terminal transition.

This separation permits the supervisor to recover after a conversation completes too early,
dies before reporting, or disappears after the workspace has already reached the objective.

## Race and persistence adapter contract

Every mutating operation requires `expected_revision`. Accepted mutations increment the
snapshot revision exactly once. Rejected stale calls return `STALE_REVISION` and make no state
change. This lets two conversations race without both acquiring authority: only one compare-
and-swap can commit the shared prior revision.

A future persistence adapter must implement the following transaction shape:

1. load one task protocol snapshot plus its workers;
2. call the pure transition with the loaded `revision` as `expected_revision`;
3. begin an atomic write transaction;
4. compare the persisted revision with the decision's `expected_revision`;
5. if it differs, abort and reload/reconcile;
6. otherwise persist the complete next snapshot and all events from that decision together;
7. commit before any later external action uses the new authority.

For takeover in particular, the old-worker terminal state, new-worker active state, task
`current_worker_id`, new generation, handoff clearing, and both events must commit together.
There must be no durable intermediate state in which two workers appear authoritative.

The logical information a later adapter must be able to represent is:

- task protocol revision and monotonic generation;
- current worker id;
- optional handoff target/request timestamp;
- task lineage (`parent_task_id`, `root_task_id`, optional `child_key`);
- worker id-to-task id registration, status, assigned generation,
  registration/claim/heartbeat/expiry/end timestamps, and optional `superseded_by`;
- durable task completion timestamp/reference;
- ordered protocol events keyed by task revision.

These are adapter requirements, not database column or migration ownership. This branch makes
no changes to `db.py`, `registry.py`, or registry schema versioning; the schema-owning branch
may map these logical fields onto its additive migration.

## Fail-closed rules

- A corrupt snapshot raises `ProtocolInvariantError`; an adapter must reconcile rather than
  repairing it optimistically.
- A fresh current lease blocks unrequested takeover.
- An expired current worker cannot heartbeat, request handoff, or self-complete.
- A superseded worker cannot be re-registered under the same worker id to escape fencing.
- A completed durable task accepts no new worker authority.
- No transition performs browser, network, Local Shell, watchdog, or workspace mutation.
