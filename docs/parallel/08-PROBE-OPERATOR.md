# CWS 0.8 child A — probe-operation operator plumbing

## Ownership

Worktree: `D:\Documents\tools\cws-wt-07-probe`

Branch: `agent/08-probe-operator`

Read `docs/AGENT_BOOTSTRAP.md` first and obey it completely.

## Goal

Turn the schema-v5 probe mutation state machine into a safe operator-facing **status and evidence-reconciliation** surface without enabling browser mutation.

The operator must be able to inspect an unresolved `OPEN` / `ROTATE` / `CLOSE` operation and feed bounded previously-observed window evidence into deterministic reconciliation. This is local registry control-plane work, not browser automation.

## Required behavior

- Add a read-only command such as `probe-op-status` that can show the unresolved/latest operation or an exact operation id in human and JSON form.
- Add an explicit evidence-fed reconciliation command such as `probe-op-reconcile OPERATION --file EVIDENCE.json --json`.
- Reconciliation input must contain only bounded window identity/state facts accepted by the existing pure `probe_ops` evaluator. Do not accept cookies, headers, page text, response bodies, or auth state.
- The command must never open, close, navigate, focus, type in, or attach to a browser window. It may only parse local evidence and update the durable operation/slot through existing registry reconciliation semantics.
- Missing, multiple, changed, old+new, or otherwise ambiguous evidence must remain unresolved/fail closed. No retry authority may be invented.
- Exact unique owned replacement adoption is allowed only where the existing operation state machine already authorizes it.
- Output must clearly distinguish `COMPLETED`, still-unresolved, and blocked/reconcile-required outcomes.
- Keep `doctor` compatible with the new operator surface.

## Non-goals

- No schema changes; D is the only schema owner.
- No `probe-open`, `probe-close`, or `probe-rotate` mutation command.
- No UIA/Playwright/CDP observation in the reconciliation command.
- No watchdog integration.
- No automatic cancellation of unresolved operations.

## Tests

Cover at least:

- status with no operation;
- status of one unresolved operation;
- exact id lookup;
- malformed or incomplete evidence rejection;
- old target still present after submitted close does not replay close;
- both old and new present remains unresolved;
- multiple exact matches remains unresolved;
- unique expected target after submitted open adopts/completes;
- evidence for the wrong operation/owner/URL is rejected;
- CLI tests prove no browser observation/mutation adapter is called.

Run focused tests, then the full suite, `git diff --check`, and `secret_scan`; commit and finish the logical session.
