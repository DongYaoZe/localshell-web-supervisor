# Operations runbook

This runbook describes how to operate the CWS 0.5 control plane while keeping the experiment-backed UIA mutation module production-gated and preserving the authentication boundary.

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
- the fact that the 0.5 UIA mutation module is gated and has no production CLI enable path;
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
python -m cws pool-plan --page-close-evidence .cws\page-close-evidence.json
```

`pool-plan` is advice only. It can mark workers `DO_NOT_CLOSE`, `PARK_CANDIDATE`, or `NO_PAGE`; it never closes a page. The default is fail-closed. Only an explicitly supplied local `PageCloseEvidence` file that passes the generation gate enables `close_allowed=true` advice for already non-live parking candidates.

A live LSM tool/job/continuation, browser generation, or ambiguous task state still wins over RAM pressure and remains `DO_NOT_CLOSE`. Although one bounded tracked LSM-job close/reopen experiment also passed the stronger tool gate, production live-LSM eviction remains disabled until an eviction dispatcher can atomically bind a specific durable job/session state to a fresh exact-window lease and close operation. If all workers are pinned, CWS reports the pressure rather than choosing a live task to sacrifice.

## 6. Recovery analysis and action fences

```powershell
python -m cws recommend TASK --uia
python -m cws dispatch-plan TASK --uia
python -m cws action-status TASK
python -m cws recovery-history TASK
python -m cws reconciliation-history TASK
```

`dispatch-plan` remains dry-run. It requires two fresh, distinct, sufficiently separated semantic reconciliation fences plus LSM/browser/workspace safety checks. An unresolved write-ahead action (`ARMED`, `SUBMITTED`, or `RECONCILE_REQUIRED`) feeds back into the planner and forces `candidate_ready=false`.

The write-ahead action protocol writes `ARMED` durably before any external side effect. Schema v2 introduced the one-unresolved-action SQLite invariant; schema v3 additionally stores short-lived exact-window leases. Ambiguous transport outcomes require reconciliation instead of automatic retry. See `ACTIONS.md`.

`action-cancel` exists only for an explicit local/operator decision after reconciliation:

```powershell
python -m cws action-cancel ATTEMPT --reason "human reconciliation completed"
```

It releases the local duplicate-send lock and does **not** claim to undo any external effect.

Even when `dispatch-plan` says `candidate_ready=true`, `would_dispatch=false`. 0.5 contains a gated exact-window UIA sender/ACK observer for isolated use, but there is no production CLI command that enables it, sends a recovery turn, or fabricates acknowledgement. A fresh worker-window lease is an additional prerequisite, not a substitute for reconciliation fences.

## 7. Isolated page-close evidence

0.5 completed authenticated same-profile disposable-window experiments for pure generation and one harmless live Local Shell MCP job. Evidence remains local under `.cws/` and can be re-evaluated with:

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

CWS registry schema v3 is additive over v2 and introduces only short-lived worker-window bindings; stale bindings are never treated as authority. Reconciliation fence semantics are separately versioned. Older fence records never silently match a newer fence schema; an upgrade forces fresh reconciliation.

## 9. Data handling

The local `.cws/` directory is ignored by Git and may contain the registry, observations, reconciliation/action history, watchdog logs, and smoke fixtures. Do not publish it as source code.

CWS intentionally minimizes persisted browser data:

- no production cookie/token/password/session-secret collection or migration; the one-time user-authorized v20 cookie-clone diagnostic is not a product feature and its local snapshots must be removed after the experiment;
- no unsent prompt draft collection;
- no request/response bodies or authorization headers;
- no conversation text in `BrowserObservation` or reconciliation records;
- no changed-path list in recovery fences;
- UIA/LSM diagnostics retain signatures, state flags, counts, and bounded metadata needed for supervision;
- worker-window leases retain only worker id, URL, HWND, PID, executable path, source, and timestamps;
- action attempts persist prompt hashes/nonces and control metadata, not prompt text.

Run `secret_scan` or an equivalent repository secret scan before publishing changes.
