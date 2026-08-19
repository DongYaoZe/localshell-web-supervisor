# Operations runbook

This runbook describes the CWS 0.10 control plane. Observation and planning remain the default. Recovery mutation is still fenced and exact-window-bound; the resident watchdog may call the same current-worker executor only when explicitly started with `--auto-recover-timeouts` and a recognized delivery error passes every reconciliation gate. 0.10 additionally provides an explicitly gated initial child-conversation spawn path and a parent-AI replacement protocol. Automatic replacement-chat creation and live-page auto-close remain disabled.

## 1. Preflight

From the repository root:

```powershell
$env:PYTHONPATH = "src"
python -m cws --version
python -m cws doctor
```

For one registered task whose current Chrome tab is explicitly authorized for supervision:

```powershell
python -m cws --db .cws\registry.sqlite3 doctor --task TASK --uia
```

`doctor` is read-only. It reports:

- CWS version and registry path;
- Local Shell MCP durable state/schema compatibility;
- system and aggregate Chrome memory pressure;
- optional Playwright/CDP capability;
- resident watchdog lease/PID/stop-fence status;
- unresolved write-ahead action-fence status for a named task;
- exact-window lease presence/freshness for a named worker;
- the fact that recovery transport is gated by default and requires explicit one-shot opt-in plus exact task confirmation;
- durable page-capability record counts/freshness and the at-most-one reusable probe-slot invariant;
- unresolved schema-v5 probe mutation state, if any, so a crash-fenced operation is reconciled before another mutation;
- optional task workspace/Git, LSM logical session/plan/in-flight state, worker status, and exact-URL UIA liveness.

A `WARN` does not necessarily mean the supervisor is broken. For example, no resident watchdog lease is expected before `cws watch` is started. `FAIL` means a requested hard invariant such as a named task/workspace or durable-state schema could not be verified.

## 2. Register durable tasks

Registration creates local CWS bookkeeping only:

```powershell
python -m cws register `
  --task-id my-task `
  --project my-project `
  --objective "finish the task safely" `
  --cwd D:\path\to\repo `
  --session-id s_... `
  --conversation-url https://chatgpt.com/.../c/...
```

The conversation URL is a worker lease, not the durable task identity. The Local Shell logical session and actual workspace remain the execution/recovery truth.

### Parent-AI child scheduling

Persist each child before creating a ChatGPT conversation. `child-create` stores the exact prompt, cwd, optional expected branch/base ref, and optional explicit ChatGPT Project root. It performs no browser or Git mutation. A child can then be manually adopted, or the parent can use the separate `child-spawn-arm` -> `child-spawn-open` -> `child-spawn-send` path. `open` and `send` each require explicit normal-browser mutation opt-in and exact child confirmation. An ambiguous external result must go through `child-spawn-reconcile`; it is never permission to resend.

The child should start its own Local Shell MCP durable logical session and bind it with `child-bind-session`. After local work/tests/commit are verified, use `child-complete --completion-ref ...`; `child-status PARENT` exposes the LSM session id, generation, worker state, and durable completion. Replacement uses `replacement-register` / `replacement-arm` / `replacement-submit`, one supported Local Shell MCP `session_manage(..., takeover=true)` call, then `replacement-complete` after fresh LSM/workspace evidence. See `CHILD_SCHEDULER.md` for the complete command sequence and prompt contract.

## 3. Read-only observation

For the current authorized Chrome tab on Windows:

```powershell
python -m cws probe-uia TASK
python -m cws inspect TASK --uia
python -m cws reconcile TASK --uia
```

UI Automation is exact-URL matched over top-level normal-Chrome windows. If more than one window matches the same URL, observation fails closed unless an exact HWND is supplied by the caller. Successful `probe-uia` observations refresh a short-lived local worker-window lease containing only URL/HWND/PID/executable/timestamps. CWS does not return conversation text from `probe-uia`; it transiently reads accessibility text only to derive a signature and persists bounded state/hash metadata.

Optional CDP observation should use a CWS-owned or explicitly exposed DevTools endpoint. Loopback is the default safety boundary:

```powershell
python -m cws probe-cdp TASK --endpoint http://127.0.0.1:9222
```

Do not expose or attach a remote browser merely to make CDP available. CWS does not require CDP for the normal authenticated Chrome/UIA path.

## 4. Resident watchdog

One-shot scan:

```powershell
python -m cws watch --once --uia
```

Foreground resident scan:

```powershell
python -m cws watch --uia --interval 30
```

Independent detached host:

```powershell
python -m cws watchdog-start --uia --interval 30
python -m cws watchdog-status
python -m cws watchdog-stop
```

The resident watchdog owns a renewable SQLite singleton lease. `watchdog-start` launches the same watcher as an independent Python process and waits until a random launch-owner token is visible in the lease before reporting success. It does not install a Windows service or scheduled task.

On this Windows environment the launcher/wrapper PID can differ from the resident Python PID, so CWS reports both the spawn PID and the lease PID. Lease ownership, not wrapper PID equality, is the startup proof.

`watchdog-stop` does **not** send a process-tree kill. It atomically changes the singleton owner to a short-lived `stop:` fence. The old watcher fails its next heartbeat and exits through its existing duplicate-control safety path; the fresh stop lease blocks a replacement during shutdown. After the resident PID disappears, the stop lease is cleared.

### Hosting warning on the inspected Windows/LSM environment

Do **not** assume an ordinary Local Shell MCP tracked shell job owns and terminates the full descendant process tree. During bootstrap, `job_stop` persisted test jobs as stopped while their PowerShell/Python descendants remained alive.

The detached CWS host and cooperative lease stop avoid depending on that process-tree behavior. CWS still installs no permanent OS service automatically.

## 5. RAM and worker pool

```powershell
python -m cws ram-status
python -m cws pool-plan
python -m cws capability-import --file .cws\page-close-evidence.json --json
python -m cws capability-status --json
python -m cws pool-plan --page-close-capability latest
```

`pool-plan` is advice only. It can mark workers `DO_NOT_CLOSE`, `PARK_CANDIDATE`, or `NO_PAGE`; it never closes a page. The default is fail-closed. The preferred path is to validate evidence once with `capability-import`, then explicitly select a context-matching, unexpired generation capability with `--page-close-capability`. Historical experiment provenance must be embedded in the evidence or supplied explicitly at import time; CWS never substitutes the current browser/runtime as the old experiment's provenance. The legacy one-shot `--page-close-evidence` input remains compatible.

The reusable probe model has at most one durable slot. Same-target observation may reuse it; a different parked target requires exact-close-before-open; stale or ambiguous ownership blocks instead of causing an extra window. Schema v5 adds a separate write-ahead mutation record for `OPEN`, `ROTATE`, and `CLOSE`: durable authority is recorded before each external phase, and a crash is reconciled against exact old/new CWS-owned window evidence before another phase may proceed. Multiple, changed, or incomplete evidence blocks rather than opening another window. The probe-window mutation transport remains disabled by default and is not called by the watchdog.

A live LSM tool/job/continuation, browser generation, or ambiguous task state still wins over RAM pressure and remains `DO_NOT_CLOSE`. Although one bounded tracked LSM-job close/reopen experiment also passed the stronger tool gate, production live-LSM eviction remains disabled until an eviction dispatcher can atomically bind a specific durable job/session state to a fresh exact-window lease and close operation. If all workers are pinned, CWS reports the pressure rather than choosing a live task to sacrifice.

## 6. Recovery analysis and action fences

```powershell
python -m cws recommend TASK --uia
python -m cws dispatch-plan TASK --uia
python -m cws action-status TASK
# Only after a candidate-ready dry run and an explicit operator decision:
python -m cws dispatch-execute TASK --confirm-task TASK --enable-experimental-uia
python -m cws action-reconcile-uia ATTEMPT
python -m cws recovery-history TASK
python -m cws reconciliation-history TASK
```

`dispatch-plan` remains dry-run. It requires two fresh, distinct, sufficiently separated semantic reconciliation fences plus LSM/browser/workspace safety checks. An unresolved write-ahead action (`ARMED`, `SUBMITTED`, or `RECONCILE_REQUIRED`) feeds back into the planner and forces `candidate_ready=false`.

The write-ahead action protocol writes `ARMED` durably before any external side effect. Recovery arming and recovery-budget consumption happen in the same SQLite transaction. Schema v5 preserves the unresolved-action invariant, short-lived exact-window leases, durable capability provenance, and reusable probe-slot record, and adds one globally unresolved probe mutation operation. Ambiguous transport outcomes require reconciliation instead of automatic retry. See `ACTIONS.md`, `V4_SPEC.md`, and `V5_SPEC.md`.

`action-cancel` exists only for an explicit local/operator decision after reconciliation:

```powershell
python -m cws action-cancel ATTEMPT --reason "human reconciliation completed"
```

It releases the local duplicate-send lock and does **not** claim to undo any external effect.

Normal `dispatch-plan` keeps `would_dispatch=false`. `dispatch-execute` is a separate explicit one-shot path: it requires the two-sample semantic fence, latest-fence identity, active current worker, canonical recovery prompt, no active LSM work, fresh exact-window lease, no unresolved prior action, remaining recovery budget, `--enable-experimental-uia`, and an exact `--confirm-task` match. The resident watchdog never supplies those opt-ins automatically.

The submitted recovery turn carries a non-secret durable attempt marker. `action-reconcile-uia` acknowledges only when that marker occurs exactly once on the exact current conversation and generation has completed; otherwise the action stays unresolved and another dispatch remains blocked.

## 7. Isolated page-close evidence

The 0.5 experiment milestone established close/reopen continuity for pure generation and one harmless live Local Shell MCP job. In 0.6 that result can be imported as versioned, expiring deployment-scoped capability provenance rather than treated as a release-wide boolean. Evidence remains local under `.cws/` and can be re-evaluated with:

```powershell
python -m cws evaluate-page-close --file .cws\page-close-evidence.json --json
python -m cws evaluate-page-close --file .cws\page-close-evidence.json --require-tool --json
```

The generation gate and live-tool gate are separate. `--require-tool` additionally requires exact tool/job identity, running-at-close, completion-after-close, and final-response evidence. Copied-auth, anonymous, localhost-only, ambiguous-window, missing-progress, duplicate-turn, changed-identity, and unchanged-signature cases fail closed.

A passing gate does **not** enable automatic page closing. `pool-plan` continues to pin live LSM work, active generation, and ambiguous states as `DO_NOT_CLOSE`; it still performs no browser mutation.

## 8. Upgrade procedure

Before changing CWS or Local Shell MCP:

1. stop or otherwise account for the resident watchdog;
2. run `cws doctor` and record the result;
3. ensure important project work has durable checkpoints/commits;
4. upgrade one layer at a time;
5. run the full CWS test suite;
6. run `cws doctor` again;
7. only then resume the resident watchdog.

The direct LSM file adapter is schema-gated. If LSM changes session/job durable formats, CWS should fail closed until the adapter is explicitly updated and tested.

CWS registry schema v8 is additive. Schema v6 preserves all v5 task/worker, observation, action, watchdog, capability, worker-window, probe-slot, and probe-mutation rows/indexes and adds durable worker-protocol task state, per-worker lease metadata, and append-only protocol events. Schema v7 adds durable child-dispatch and replacement-attempt records. Schema v8 adds explicit child ChatGPT Project binding plus child-spawn write-ahead records; an explicit migration also adds the project column to registries that had already reached schema v7. Protocol writes keep revision compare-and-swap semantics, and the older unresolved-probe/action uniqueness fences remain unchanged.

## 9. Data handling

The local `.cws/` directory is ignored by Git and may contain the registry, observations, reconciliation/action history, watchdog logs, and smoke fixtures. Do not publish it as source code.

CWS intentionally minimizes persisted browser data:

- no browser sign-in-state collection or migration as a product feature;
- no unsent prompt draft collection;
- no request/response bodies or authorization headers;
- no conversation text in `BrowserObservation` or reconciliation records;
- no changed-path list in recovery fences;
- UIA/LSM diagnostics retain signatures, state flags, counts, and bounded metadata needed for supervision;
- worker-window leases retain only worker id, URL, HWND, PID, executable path, source, and timestamps;
- action attempts persist wire-prompt hashes/nonces and control metadata, not recovery prompt text;
- page-capability rows persist evidence digests, context/version/expiry data, and boolean evaluation metadata, not raw experiment text/signatures.

Run `secret_scan` or an equivalent repository secret scan before publishing changes.
