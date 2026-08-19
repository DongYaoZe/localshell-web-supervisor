# 0.7-C — adversarial crash/race test campaign

Assigned worktree: `D:\Documents\tools\cws-wt-07-adversarial`

Assigned branch: `agent/07-adversarial-tests`

## Objective

Attack CWS 0.6 invariants with deterministic tests and minimal test helpers. Find scenarios where a plausible-looking state could cause duplicate action, extra probe window, false completion, or unsafe retry.

## Priority attacks

Exercise combinations of:

- stale HWND reused by Chrome;
- PID/executable mismatch;
- duplicate conversation URL windows;
- one ownership tag appearing on multiple windows;
- old and new probe targets both present after a simulated crash;
- SQLite process death before/after ARMED write and budget increment;
- unresolved action plus stale browser ACK evidence;
- nonce appears zero, once, twice, or in the wrong worker;
- stale `jobs.json` says running while attempt-status evidence is terminal;
- LSM takeover while an in-flight lease exists;
- continuation pending races;
- Git HEAD changes between two reconciliation samples;
- dirty tree changes with same HEAD;
- capability expiry/browser-major/context mismatch;
- time rollback/large clock jumps where applicable;
- duplicate scheduler candidates and recovery-budget contention.

Prefer tests against public/pure interfaces rather than monkey-patching internals. Do not weaken assertions to accommodate current behavior. If a test demonstrates a genuine 0.6 defect, either add the smallest clearly isolated fix if it does not collide with another child ownership area, or leave the failing reproducer plus a concise integration note. The branch must finish with its own test suite green; therefore known defects that require another child task should be expressed as xfail only when the reason and expected future owner are explicit.

## Conflict avoidance

This task primarily owns new tests and test helpers. Avoid schema migrations, CLI redesign, watcher orchestration implementation, or browser mutation implementation.

## Explicit non-goals

- No real browser mutation experiments.
- No network/private endpoint experiments.
- No auth/cookie/token inspection.
- No unrelated refactor.

## Deliverable

A committed adversarial regression suite that materially raises confidence in crash/race behavior and documents any uncovered defect with exact reproduction and likely owner.
