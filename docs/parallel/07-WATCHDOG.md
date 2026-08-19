# 0.7-B — watchdog orchestration policy without automatic mutation

Assigned worktree: `D:\Documents\tools\cws-wt-07-watchdog`

Assigned branch: `agent/07-watchdog-orchestration`

## Objective

Design and implement the deterministic scheduling/orchestration layer that decides when a task is eligible for reconciliation or an explicit recovery action, while keeping resident watchdog browser mutation disabled.

## Required behavior

Model task scheduling around the existing durable states and 0.6 fences. The policy must account for:

- task state and current worker lease;
- semantic two-sample reconciliation stability;
- unresolved action attempts;
- LSM in-flight calls, active tracked jobs, and continuation pending;
- workspace/Git reconciliation freshness;
- recovery budget and cooldown;
- exact-window binding freshness where an action would eventually require it;
- page-continuity capability provenance when parking/reopen behavior is relevant;
- fairness across multiple SUSPECT/RECONCILING/RECOVERING tasks;
- bounded backoff to avoid hot loops.

Return explicit decisions such as observe, reconcile, recommend-action, wait/cooldown, blocked/human. Keep decisions pure where possible and separate them from side-effect execution.

Add tests for fairness, cooldown, budget exhaustion, stale evidence, unresolved action lock, active jobs, continuation pending, and multiple competing tasks.

## File ownership / conflict avoidance

Prefer `src/cws/scheduler.py`, `src/cws/orchestrator.py`, `src/cws/watcher.py`, and a new pure orchestration module plus focused tests. Do **not** introduce a registry schema migration. Avoid editing `db.py`/`registry.py` unless absolutely necessary; if blocked on missing durable fields, model an interface and document the required integrator hook instead.

## Explicit non-goals

- Do not call `dispatch-execute` automatically.
- Do not invoke UIA mutation transport.
- Do not start a resident watchdog.
- Do not close/open real browser pages.
- Do not enable live-worker eviction.

## Deliverable

A committed deterministic orchestration policy and tests that an integrator can later connect to durable storage/execution without changing the safety defaults.
