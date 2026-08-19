# LocalShell Web Supervisor (LWS)

**LocalShell Web Supervisor** is a local reliability and orchestration layer for long-running browser-driven agents that use [Local Shell MCP](https://github.com/fwerkor/local-shell-mcp) for durable local execution.

The central rule is simple:

> **A web conversation is a replaceable worker lease. The durable task is not the conversation.**

A browser turn, a Local Shell tool call, and the actual Git/workspace state can fail or finish independently. LWS keeps those truth domains separate so a stalled or interrupted web turn does not automatically cause a duplicate commit, write, upload, or retry.

## What LWS does

LWS provides a fail-closed control plane around browser-based agent work:

- durable SQLite task and worker registry;
- Local Shell MCP logical-session, Goal, in-flight-call, continuation, and tracked-job reconciliation;
- read-only workspace/Git observation;
- browser/DOM observation and exact-window Windows UI Automation fencing;
- optional CDP network-lifecycle evidence without retaining headers, cookies, request bodies, or response bodies;
- deterministic health/stall classification across browser, Local Shell, and workspace evidence;
- write-ahead recovery actions with replay suppression;
- worker generations, leases, handoff, takeover, supersession, and durable completion;
- parent/child task scheduling with persisted prompts and worktree metadata;
- explicitly gated initial child-conversation creation in a confirmed web project;
- guarded replacement-worker takeover coordinated with supported Local Shell MCP session takeover;
- low-noise resident watchdog support for narrowly recognized delivery failures;
- browser-memory and active/park/probe planning.

LWS is deliberately conservative. Unknown external outcomes are reconciled, not replayed.

## Provider boundary

The task, Local Shell, Git, worker-protocol, scheduler, and recovery layers are provider-neutral. The current browser adapter is tested against `chatgpt.com` in normal Chrome and therefore contains a small amount of provider-specific URL and accessibility logic.

That adapter does **not** reconstruct private service endpoints, copy authentication material, or use browser cookies/tokens as an execution API. Provider-specific browser behavior is treated as an observation/mutation adapter behind the durable control plane.

## Safety model

LWS is reliability infrastructure for work the user already authorized. It is not an authentication bypass or a private web-client implementation.

Core invariants:

- never infer completion from a Send/Stop button alone;
- never treat `continue` or resend as idempotent;
- reconcile Local Shell state and actual workspace/Git state before recovery;
- bind browser mutation to exact task/worker/window identity;
- persist mutation authority before an external side effect;
- if an open/send/takeover result is ambiguous, reconcile instead of replaying it;
- never inspect, copy, or move credentials, cookies, tokens, passwords, or session secrets;
- never click or type in unrelated browser tabs;
- automatic replacement-conversation creation and automatic live-worker page eviction remain disabled.

See [`docs/SAFETY.md`](docs/SAFETY.md) for the detailed contract.

## Installation

LWS requires Python 3.11+.

```powershell
python -m pip install -e .
lws --version
```

For source-tree development without installation:

```powershell
$env:PYTHONPATH = "src"
python -m lws --help
```

The local registry defaults to `.lws/registry.sqlite3` and is ignored by Git. Override it with `--db` or `LWS_DB`.

## Basic task registration

```powershell
lws register `
  --task-id example `
  --project demo `
  --objective "finish the demo safely" `
  --cwd D:\work\demo `
  --session-id s_example `
  --conversation-url https://chatgpt.com/c/example

lws inspect example --uia
lws reconcile example --uia
lws recommend example --uia
```

The conversation URL is worker metadata. The durable Local Shell logical session and the real workspace are the execution truth.

## Parent/child scheduling

Persist a child assignment before opening or adopting a browser conversation:

```powershell
lws child-create PARENT_TASK `
  --child-key worker-a `
  --child-task-id CHILD_TASK `
  --project demo `
  --objective "implement isolated feature A" `
  --cwd D:\worktrees\feature-a `
  --expected-branch agent/feature-a `
  --base-ref ABC123 `
  --web-project-url https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef `
  --prompt-file .lws\prompts\feature-a.txt `
  --json
```

`child-create` performs no browser, Local Shell, or Git mutation.

For an existing child conversation:

```powershell
lws child-adopt CHILD_TASK --conversation-url https://chatgpt.com/g/g-p-.../c/... --json
```

For explicitly gated initial child creation:

```powershell
lws child-spawn-arm CHILD_TASK --json

lws child-spawn-open SPAWN_ATTEMPT `
  --enable-normal-browser-mutation `
  --confirm-child CHILD_TASK `
  --json

lws child-spawn-send SPAWN_ATTEMPT `
  --enable-normal-browser-mutation `
  --confirm-child CHILD_TASK `
  --json
```

If either external mutation has an unknown outcome, do **not** rerun it:

```powershell
lws child-spawn-reconcile SPAWN_ATTEMPT --json
```

A child should bind its Local Shell durable session back to LWS:

```powershell
lws child-bind-session CHILD_TASK --session-id s_child
```

After independent verification, finish the durable child with a concrete completion reference:

```powershell
lws child-complete CHILD_TASK --completion-ref commit:ABCDEF123456 --json
lws child-status PARENT_TASK --json
```

See [`docs/CHILD_SCHEDULER.md`](docs/CHILD_SCHEDULER.md) for the complete workflow.

## Replacement workers

A missing browser window alone is not enough evidence to replace a worker. Replacement requires fresh Local Shell/workspace evidence and no unresolved external mutation.

The high-level flow is:

```text
replacement-register
    -> replacement-arm
    -> replacement-submit
    -> one supported Local Shell MCP session takeover
    -> replacement-complete
```

The Local Shell takeover call is made exactly once after write-ahead authorization. If its result is lost or ambiguous, the next step is reconciliation, not another takeover call.

## Resident watchdog

The default watchdog is advisory. The optional timeout-recovery mode is intentionally narrow:

```powershell
lws watchdog-start --auto-recover-timeouts
lws watchdog-status
lws watchdog-stop
```

It only acts after the same worker passes exact-window, Local Shell, workspace, semantic-fence, action-lock, cooldown, and recovery-budget checks.

## Architecture

LWS separates four layers:

```text
web conversation / browser evidence
             |
             v
      LocalShell Web Supervisor
      - registry
      - reconciliation
      - worker leases/generations
      - scheduler/replacement
      - mutation write-ahead logs
             |
             +------> Local Shell MCP durable sessions / Goals / jobs
             |
             +------> actual workspace / Git state
```

The failure domains are intentionally independent. Browser UI is evidence, not durable execution state.

Additional design documents:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- [`docs/STATE_MACHINE.md`](docs/STATE_MACHINE.md)
- [`docs/ACTIONS.md`](docs/ACTIONS.md)
- [`docs/WORKER_PROTOCOL.md`](docs/WORKER_PROTOCOL.md)
- [`docs/CHILD_SCHEDULER.md`](docs/CHILD_SCHEDULER.md)
- [`docs/SAFETY.md`](docs/SAFETY.md)
- [`docs/V9_SPEC.md`](docs/V9_SPEC.md) — public rebrand/schema-v9 notes

Older version-spec documents are retained as design history. The Git history intentionally preserves the project's earlier name and development record.

## Local/private state

`.lws/` is intentionally ignored. It may contain:

- registry databases;
- browser observations and exact-window bindings;
- action/replacement/spawn write-ahead records;
- watchdog logs;
- local experiment fixtures.

Do not publish `.lws/`, browser profiles, storage-state files, cookies, tokens, session secrets, or machine-specific dumps.

## Development checks

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
git diff --check
```

Run a secret/privacy scan before publishing changes.

## License

MIT. See [`LICENSE`](LICENSE).
