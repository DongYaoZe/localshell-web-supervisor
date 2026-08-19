# 0.7-A — crash-fenced probe-window operations

Assigned worktree: `D:\Documents\tools\cws-wt-07-probe`

Assigned branch: `agent/07-probe-ops`

This is the only 0.7 child task that owns the registry schema migration.

## Objective

Turn the 0.6 probe-slot planner into a durable write-ahead operation model for `OPEN`, `ROTATE`, and `CLOSE`, so a process crash can never justify blindly opening an additional probe window.

## Required design

Add an additive schema-v5 operation record (name may differ) with a state machine analogous in spirit to action attempts. Persist enough sanitized identity before mutation to reconcile after restart:

- operation id / nonce;
- operation kind (`OPEN`, `ROTATE`, `CLOSE`);
- state (`ARMED` plus explicit terminal/ambiguous states);
- target task/worker/conversation and ownership tag;
- prior probe-slot snapshot when relevant;
- expected new tagged target when relevant;
- timestamps and bounded retry/reconcile metadata;
- no credentials or conversation text.

Enforce at most one unresolved probe mutation operation in SQLite. Arm the operation atomically before external mutation authority can be returned.

Define deterministic reconciliation outcomes using exact CWS ownership tag plus HWND/PID/executable/target identity:

- exact target absent;
- exact unique owned target present;
- old target still present;
- both old and new present;
- multiple matches;
- stale/changed identity;
- unknown observation.

Any multiple/changed/unknown condition must block further open/rotate. A crash after browser open but before durable slot update must be recoverable by adopting exactly one proven owned match rather than opening another window. A crash after old-window close but before replacement open must remain safely resumable without pretending the replacement exists.

## Scope

Expected production ownership: `src/cws/db.py`, `src/cws/registry.py`, `src/cws/models.py`, `src/cws/page_runtime.py`, plus new focused module(s) if helpful. CLI changes are allowed only if needed for an explicit operator reconciliation command; do not add unattended browser mutation.

Add focused tests for migration v4→v5, uniqueness, every crash boundary, idempotent reconciliation, ambiguity, stale identities, and exact-close-before-open semantics.

## Explicit non-goals

- Do not actually open or close Chrome.
- Do not wire this into resident watchdog auto-mutation.
- Do not enable live-worker eviction.
- Do not copy browser auth or use private endpoints.
- Do not redesign recovery action attempts.

## Deliverable

A committed branch whose pure/durable state machine makes probe OPEN/ROTATE/CLOSE crash-fenced and fail-closed. Include integration notes for any schema/CLI conflicts.
