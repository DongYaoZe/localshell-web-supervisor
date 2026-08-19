# CWS 0.8 child B — advisory orchestration adapter

## Ownership

Worktree: `D:\Documents\tools\cws-wt-07-watchdog`

Branch: `agent/08-orchestration-adapter`

Read `docs/AGENT_BOOTSTRAP.md` first and obey it completely.

## Goal

Bridge the pure 0.7 orchestration policy to real **read-only CWS durable evidence** so an integrator can ask, “what should the supervisor do next?” without enabling any mutation.

The result should convert Registry + Local Shell telemetry + workspace/Git + existing reconciliation/window/capability records into deterministic orchestration inputs/decisions. It is an advisory runtime adapter, not a dispatcher.

## Required behavior

- Add a small runtime/adapter module that builds orchestration candidates from existing registered tasks and their durable evidence.
- Reuse existing `watcher.assess`, reconciliation records, Registry accessors, `FileLsmTelemetry`, and `WorkspaceProbe` rather than duplicating their semantics.
- Include the evidence needed by the pure orchestration policy: task state, unresolved action lock, recovery budget, LSM identity/freshness/in-flight/jobs/continuation, workspace/Git freshness, current worker lease, recent reconciliations, exact-window lease where relevant, capability provenance where relevant, and cooldown/scheduling metadata when it can be derived safely.
- Where 0.7 lacks durable scheduling-history fields, fail closed or accept them explicitly as adapter inputs; do not invent persisted values.
- The adapter must return advisory decisions only. Every result that points toward recovery must preserve `mutation_allowed=false`.
- Provide a deterministic multi-task plan function suitable for a future CLI/watchdog host, including fair bounded selection and stable ordering.
- It must be usable with fake telemetry/workspace providers in tests and must not require a browser.

## Non-goals

- No schema changes; D is the only schema owner.
- Do not call `dispatch-execute`, action transport, probe mutation transport, UIA mutation, browser open/close, or LSM takeover.
- Do not start or modify the resident watchdog host.
- Do not add automatic retry/takeover/conversation creation.
- Avoid `cli.py` ownership in this child; the integrator will expose any CLI after merge if useful.

## Tests

Cover at least:

- healthy running task stays observe/quiet;
- stalled task with active LSM call/job/continuation cannot recommend action;
- dirty/changed Git or stale workspace evidence blocks action recommendation;
- unresolved `ActionAttempt` blocks;
- unresolved probe mutation does not silently coexist with a page-reopen recommendation;
- two stable fresh semantic fences are required where recovery needs them;
- stale/future timestamps fail closed;
- recovery budget exhaustion and cooldown work;
- fair ordering across several actionable tasks is stable and duplicate-free;
- adapter never sets or forwards mutation authority.

Run focused tests, then the full suite, `git diff --check`, and `secret_scan`; commit and finish the logical session.
