# Operations runbook

This runbook describes how to operate the CWS 0.4 control plane without enabling ChatGPT mutation transport or weakening its authentication boundary.

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
- the fact that ChatGPT mutation transport is disabled;
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

UI Automation is exact-URL matched. CWS does not read or move cookies/tokens/passwords, does not read the unsent prompt textarea, and does not return conversation text from `probe-uia`. It transiently reads accessibility text only to derive a message signature, then persists state/hash metadata rather than the text.

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
```

`pool-plan` is advice only. It can mark workers `DO_NOT_CLOSE`, `PARK_CANDIDATE`, or `NO_PAGE`. It never closes a page.

A live LSM tool/job/continuation, browser generation, or ambiguous task state always wins over RAM pressure. If all workers are pinned, CWS reports the pressure rather than choosing a live task to sacrifice.

A dedicated persistent profile `cws-disposable-v4` now exists without copied authentication state, but it is currently unauthenticated. Therefore ChatGPT-specific close/reopen remains unproven. Anonymous and localhost tests do not justify closing a live ChatGPT worker.

## 6. Recovery analysis and action fences

```powershell
python -m cws recommend TASK --uia
python -m cws dispatch-plan TASK --uia
python -m cws action-status TASK
python -m cws recovery-history TASK
python -m cws reconciliation-history TASK
```

`dispatch-plan` remains dry-run. It requires two fresh, distinct, sufficiently separated semantic reconciliation fences plus LSM/browser/workspace safety checks. An unresolved write-ahead action (`ARMED`, `SUBMITTED`, or `RECONCILE_REQUIRED`) feeds back into the planner and forces `candidate_ready=false`.

The 0.4 action protocol writes `ARMED` durably before any future external side effect. Registry schema v2 enforces at most one unresolved action per task at the SQLite layer. Ambiguous transport outcomes require reconciliation instead of automatic retry. See `ACTIONS.md`.

`action-cancel` exists only for an explicit local/operator decision after reconciliation:

```powershell
python -m cws action-cancel ATTEMPT --reason "human reconciliation completed"
```

It releases the local duplicate-send lock and does **not** claim to undo any external effect.

Even when `dispatch-plan` says `candidate_ready=true`, ChatGPT mutation transport remains absent and `would_dispatch=false`. There is no CLI command that sends a recovery turn or fabricates acknowledgement.

## 7. Isolated page-close evidence

After the user normally signs into the dedicated `cws-disposable-v4` browser profile and creates a disposable conversation, follow `EXPERIMENTS.md` and evaluate the captured evidence with:

```powershell
python -m cws evaluate-page-close --file .cws\page-close-evidence.json --json
```

Only `parking_safe=true` is sufficient to reconsider live-page parking. The evaluator fails closed for anonymous tests, copied authentication, localhost-only evidence, missing background progress, duplicate turns, changed conversation identity, or unchanged message signature.

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

Reconciliation fence semantics are also versioned. Older fence records never silently match a newer fence schema; an upgrade forces fresh reconciliation.

## 9. Data handling

The local `.cws/` directory is ignored by Git and may contain the registry, observations, reconciliation/action history, watchdog logs, and smoke fixtures. Do not publish it as source code.

CWS intentionally minimizes persisted browser data:

- no cookie/token/password/session-secret collection;
- no unsent prompt draft collection;
- no request/response bodies or authorization headers;
- no conversation text in `BrowserObservation` or reconciliation records;
- no changed-path list in recovery fences;
- UIA/LSM diagnostics retain signatures, state flags, counts, and bounded metadata needed for supervision;
- action attempts persist prompt hashes/nonces and control metadata, not prompt text.

Run `secret_scan` or an equivalent repository secret scan before publishing changes.
