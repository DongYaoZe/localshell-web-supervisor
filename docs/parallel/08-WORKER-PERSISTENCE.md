# CWS 0.8 child D — durable worker-protocol persistence

## Ownership

Worktree: `D:\Documents\tools\cws-wt-07-worker`

Branch: `agent/08-worker-persistence`

Read `docs/AGENT_BOOTSTRAP.md` first and obey it completely.

## Goal

Persist the pure `worker_protocol.py` revision/generation lease state machine into the existing CWS durable registry without creating a second competing durable-task identity. This child is the **only registry schema owner in the 0.8 parallel wave**.

The result must allow a replacement ChatGPT conversation to be represented as a fenced durable worker lease even when the previous web conversation disappears, while browser creation, message sending, and LSM takeover remain outside this child.

## Required design

- Bump the registry schema additively from v5 to v6.
- Preserve all existing v5 tables, rows, indexes, and invariants.
- Reuse existing `tasks.task_id` and `workers.worker_id` as canonical identities. Do not create a parallel `tasks2` / competing worker identity system.
- Persist protocol metadata in additive tables/fields keyed to the existing task/worker ids. A compact protocol-state row plus append-only protocol events is preferred over duplicating the whole task record.
- Persist at least task revision, worker generation, durable protocol task status, current worker, handoff target/time, lineage metadata, completion metadata, and enough per-worker protocol metadata to reconstruct/validate the pure `WorkerTaskState` exactly.
- Existing pre-v6 tasks/workers must have a deterministic bootstrap path. Bootstrap must not silently invent authority from ambiguous legacy states. If an existing record cannot be mapped consistently, fail closed and require reconciliation.
- Keep existing `TaskRecord.current_worker_id` and existing worker status bookkeeping consistent with the protocol state, or reject the transition atomically.
- Implement Registry/runtime APIs that load a validated protocol snapshot, apply one pure `worker_protocol` transition using an expected revision, and persist accepted state/events atomically under `BEGIN IMMEDIATE`.
- Compare-and-swap semantics must hold across separate Registry connections/processes: two transitions against the same expected revision may not both commit.
- A stale worker generation/revision may never regain authority after takeover or supersession.
- Append protocol events only for accepted mutations; rejected decisions must not advance revision or partially change existing CWS task/worker rows.
- Completion of a worker is not automatically durable task completion unless the pure protocol transition says so.
- Lineage (`parent_task_id`, `root_task_id`, `child_key`) must be persisted without weakening existing foreign-key/task identity guarantees.

## Compatibility boundary

The current `WorkerStatus` enum and v5 worker rows do not necessarily encode every protocol lease status (`candidate`, `completed`, `abandoned`). Prefer an additive protocol metadata representation unless extending the core enum is demonstrably safe and fully backward-tested. Do not repurpose legacy fields in a way that changes 0.7 semantics.

## Non-goals

- No browser/UIA/Playwright/CDP creation or mutation.
- No automatic ChatGPT conversation creation.
- No message send/retry.
- No LSM `takeover` call.
- No resident watchdog changes.
- Avoid `cli.py`; expose Registry/runtime APIs and tests only so A owns CLI surface and B owns advisory adapter.

## Tests

Cover at least:

- additive schema-v5 -> v6 migration preserving representative v5 rows, indexes, action/probe-operation uniqueness, capabilities, and worker-window/probe-slot records;
- bootstrap of a clean existing single-worker task;
- ambiguous/corrupt legacy task/worker combination fails closed;
- register/claim/heartbeat persisted round trip;
- stale expected revision rejected across two Registry connections;
- stale generation heartbeat/completion/handoff rejected after takeover;
- handoff and lease-expiry takeover persist exactly one active generation/current worker;
- worker completion vs durable task completion remain distinct;
- abandonment does not erase durable task identity;
- parent/root/child lineage survives reload;
- accepted transition + event append + mapped legacy bookkeeping are one transaction;
- rejected transition leaves the database byte/logically unchanged except unavoidable SQLite metadata;
- schema-v6 unknown-future version still fails closed.

Run focused tests, then the full suite, `git diff --check`, and `secret_scan`; commit and finish the logical session.
