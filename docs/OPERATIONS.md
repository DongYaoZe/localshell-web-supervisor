# Operations runbook

This runbook describes how to operate CWS V3 without enabling recovery actions or weakening its authentication boundary.

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
- resident watchdog lease status;
- the fact that V3 recovery transport is disabled;
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

Resident scan:

```powershell
python -m cws watch --uia --interval 30
```

The resident watchdog owns a renewable SQLite singleton lease. A second resident instance refuses to start while the lease is fresh. If a running instance loses its lease, it exits rather than create duplicate future control-plane actions.

### Hosting warning on the inspected Windows/LSM environment

Do **not** assume an ordinary Local Shell MCP tracked shell job owns and terminates the full descendant process tree. During bootstrap, `job_stop` persisted test jobs as stopped while their PowerShell/Python descendants remained alive.

Therefore production watchdog hosting should be independent of a ChatGPT conversation and should not rely on an LSM tracked shell job as the sole process-lifetime mechanism. A dedicated console/service/task-manager integration can be added later, but CWS does not install one automatically.

## 5. RAM and worker pool

```powershell
python -m cws ram-status
python -m cws pool-plan
```

`pool-plan` is advice only. It can mark workers `DO_NOT_CLOSE`, `PARK_CANDIDATE`, or `NO_PAGE`. It never closes a page.

A live LSM tool/job/continuation, browser generation, or ambiguous task state always wins over RAM pressure. If all workers are pinned, CWS reports the pressure rather than choosing a live task to sacrifice.

The ChatGPT-specific close/reopen invariant is still unproven because no isolated normally authenticated disposable CWS profile existed during V2 bootstrap. A localhost harness passed, but that does not justify closing a live ChatGPT worker.

## 6. Recovery analysis

```powershell
python -m cws recommend TASK --uia
python -m cws dispatch-plan TASK --uia
python -m cws recovery-history TASK
python -m cws reconciliation-history TASK
```

`dispatch-plan` is V3 dry-run only. It requires two fresh, distinct, sufficiently separated semantic reconciliation fences plus LSM/browser/workspace safety checks.

Even when output says:

```text
candidate_ready=true
```

V3 still reports:

```text
transport_enabled=false
would_dispatch=false
```

There is no CLI flag to enable transport, and `execute_dispatch()` raises `DispatchDisabled` unconditionally.

## 7. Upgrade procedure

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

## 8. Data handling

The local `.cws/` directory is ignored by Git and may contain the registry, observations, reconciliation history, and smoke fixtures. Do not publish it as source code.

CWS intentionally minimizes persisted browser data:

- no cookie/token/password/session-secret collection;
- no unsent prompt draft collection;
- no request/response bodies or authorization headers;
- no conversation text in `BrowserObservation` or reconciliation records;
- no changed-path list in recovery fences;
- UIA/LSM diagnostics retain signatures, state flags, counts, and bounded metadata needed for supervision.

Run `secret_scan` or an equivalent repository secret scan before publishing changes.
