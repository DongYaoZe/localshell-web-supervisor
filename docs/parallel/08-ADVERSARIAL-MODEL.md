# CWS 0.8 child C — adversarial state-machine/model coverage

## Ownership

Worktree: `D:\Documents\tools\cws-wt-07-adversarial`

Branch: `agent/08-adversarial-model`

Read `docs/AGENT_BOOTSTRAP.md` first and obey it completely.

## Goal

Attack the new 0.7/0.8 control-plane boundaries with deterministic state-machine/model tests. The focus is the real failure class observed during development: a parent ChatGPT Web turn can die after child/local work has progressed, while durable task state must remain recoverable without duplicate authority or replay.

This child is primarily tests-only. If a real product defect is found, add a minimal failing regression and report the exact production owner instead of broad cross-owner edits.

## Required model/invariants

Exercise bounded sequences across:

- `ActionAttempt` write-ahead send fencing;
- schema-v5 probe mutation operations;
- pure worker lease revision/generation protocol;
- advisory orchestration decisions;
- Local Shell/job/continuation and Git/workspace truth inputs.

Prove/check at least these invariants:

1. A dead/stale/superseded conversation never becomes durable task completion by implication.
2. Parent-web-turn/message-delivery failure after a child commit cannot erase the child result and cannot authorize replay of that work.
3. At most one unresolved message action exists per task.
4. At most one unresolved probe mutation exists globally.
5. A submitted ambiguous close/open/send is never replayed merely because the supervising process restarted.
6. A stale worker generation cannot heartbeat, complete, request handoff, or reclaim authority after takeover.
7. Racing expected revisions admit at most one accepted protocol mutation.
8. Orchestration cannot manufacture mutation authority and cannot recommend action while strong LSM/workspace/action/probe fences disagree.
9. Reordering logically independent read-only observations does not produce duplicate external authority.
10. Wall-clock rollback/future timestamps fail closed where freshness is security/reliability relevant.

## Real parent-turn failure fixture

Represent the observed development incident as a sanitized deterministic fixture, not browser text:

- parent web delivery result becomes unknown/timed-out;
- one or more child worktrees may already contain terminal commits;
- Local Shell durable child sessions can be completed even though the parent turn did not deliver;
- integrator recovery begins by reconciling Git/session state and must not restart completed child work.

The test should assert the intended recovery classification/decision boundary with no actual web access.

## Technique

Use only stdlib/unittest/pytest already present. A small deterministic exhaustive transition generator is preferred over adding a property-testing dependency. Keep the state space bounded and reproducible.

## Non-goals

- No schema changes.
- No real browser access/mutation.
- No auth/private endpoint work.
- No production refactor unless a minimal owner-local bug fix is explicitly unavoidable; otherwise report the bug to integrator.

Run focused tests, then the full suite, `git diff --check`, and `secret_scan`; commit and finish the logical session.
