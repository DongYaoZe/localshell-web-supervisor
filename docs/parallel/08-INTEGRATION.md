# CWS 0.8 integration contract

Stable parent: `391943a` (CWS 0.7.0, schema v5, 229 tests).

The 0.8 bootstrap commit containing this contract is the common parent for all four children. Children must not merge each other.

## Child ownership

- A `agent/08-probe-operator`: operator-facing probe-operation status and evidence reconciliation. Owns CLI changes needed for this scope. No schema changes.
- B `agent/08-orchestration-adapter`: read-only durable-evidence adapter into advisory orchestration. Prefer new runtime module/tests; avoid CLI. No schema changes.
- C `agent/08-adversarial-model`: deterministic adversarial/model tests, especially real parent-web-turn death semantics. Primarily tests-only. No schema changes.
- D `agent/08-worker-persistence`: durable worker-protocol persistence and **sole schema-v6 ownership**. Avoid CLI.

## Integration order

Review every child independently; only accept commits with focused/full tests, `git diff --check`, secret scan, and scope-conforming diffs.

Preferred order:

1. D — schema v6 and worker persistence, because it establishes the only database migration for the wave.
2. B — advisory orchestration adapter, rebasing only conceptually through integrator; it must not acquire mutation authority.
3. A — probe operation operator surface; resolve Registry API conflicts against D without weakening v5/v6 fencing.
4. C — adversarial/model tests last so they attack the integrated semantics. If C discovers a real production defect, fix it in the owning layer with a small evidence-backed integrator patch or a follow-up owner branch.

A/B/C are intentionally written to compile against the bootstrap v5 baseline and must not depend on D's v6 additions to finish their own branch.

## Hard integration invariants

- `Conversation can die, Task cannot die.`
- Existing v5 rows migrate additively; no destructive rewrite.
- Existing task/worker ids remain canonical durable identities.
- One unresolved message action per task and one unresolved probe mutation globally remain database invariants.
- Revision/generation worker fencing may not weaken action, exact-window, LSM, workspace, or semantic reconciliation fences.
- Advisory orchestration always keeps `mutation_allowed=false`.
- Operator probe reconciliation consumes bounded local evidence only; it never invokes browser observation/mutation by itself.
- No real browser open/close/rotate/send/focus/type/navigation during integration or verification.
- No automatic ChatGPT conversation creation, retry, takeover, or live-worker eviction.
- No copied auth, cookies/tokens, private ChatGPT endpoints, or OpenAI API.
- No resident watchdog start.

## Integrator verification

After each accepted child commit:

- run focused affected tests;
- run the full suite;
- inspect schema/API conflicts and stale release claims;
- keep the integration worktree clean between commits where practical.

Final verification must include:

- complete pytest suite with no unexplained xfail/skip regression;
- `compileall` and `git diff --check`;
- changed-file/repository secret scan with any fixture false positives explicitly reviewed;
- explicit schema-v5 -> v6 migration smoke on a temporary copy preserving representative v5 state;
- doctor smoke without UIA/browser mutation;
- concurrency/CAS tests for worker protocol persistence;
- proof that probe reconciliation CLI and orchestration adapter cannot call a mutating browser transport;
- sanitized deterministic regression of parent web delivery failure after child/local work has already progressed.

## Milestone decision

Call the integrated result 0.8.0 only if worker protocol persistence is durable/atomic, the operator/advisory surfaces remain non-mutating, migration is additive, all tests are green, and no unresolved correctness issue remains. Otherwise retain a development integration commit and launch another bounded parallel tranche rather than weakening gates to force a release.
