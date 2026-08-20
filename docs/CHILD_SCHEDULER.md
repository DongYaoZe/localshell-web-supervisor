# AI child scheduler runbook

This is the operator/parent-AI workflow for LWS 0.11. It is intentionally split into durable bookkeeping, explicit browser mutation, Local Shell MCP execution, and completion so that a failed web turn can be reconciled without blindly repeating an external side effect.

## 1. Parent preflight

Before dispatching children, reconcile the parent task and actual repository:

```powershell
$env:PYTHONPATH = "src"
python -m lws doctor
python -m lws watchdog-status
```

Also inspect the parent's Local Shell MCP logical session, Goal plan, active jobs/in-flight calls, and Git status. Do not dispatch onto a worktree whose ownership or current state is ambiguous.

## 2. Decompose work

A useful child assignment should be independently verifiable and should name:

- one durable child key/task id;
- project and objective;
- exact cwd/worktree;
- expected branch and base ref when Git isolation matters;
- explicit web project root if automatic initial child-spawn may be used;
- an exact prompt that tells the child what it owns, what it must not touch, what tests/checks to run, and what completion reference to return.

Prefer fewer substantial children over many tiny conversational jobs. The parent remains responsible for integration and independent verification.

## 3. Persist the child before opening web chat

Example:

```powershell
python -m lws child-create PARENT_TASK `
  --child-key worker-a `
  --child-task-id CHILD_TASK `
  --project my-project `
  --objective "implement isolated feature A" `
  --cwd D:\worktrees\feature-a `
  --expected-branch agent/feature-a `
  --base-ref ABC123 `
  --web-project-url https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef `
  --prompt-file .lws\prompts\feature-a.txt `
  --json
```

`child-create` performs no browser, Local Shell MCP, or Git mutation. The prompt and its digest become durable dispatch state before a child conversation exists.

### Non-ASCII child contracts

Do not pass Chinese or other non-ASCII paths/prompts through a raw PowerShell command-line argument chain. That shell/code-page boundary can corrupt text before LWS sees it. Automation should instead write one UTF-8 JSON contract at an ASCII-safe path and pass only that path:

```json
{
  "child_key": "worker-cn",
  "child_task_id": "CHILD_TASK",
  "project": "my-project",
  "objective": "inspect Chinese source material without changing paths",
  "cwd": "D:\\worktrees\\unicode-task",
  "prompt_text": "Read the assigned source; preserve all non-ASCII path text exactly.",
  "web_project_url": "https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef",
  "metadata": {"wave": "W1"}
}
```

```powershell
python -m lws child-create PARENT_TASK `
  --contract-file C:\lws-contracts\worker-cn.json `
  --json
```

`--contract-b64` accepts the same JSON encoded as UTF-8 and then base64, which is useful when even the contract-file path must cross an ASCII-only automation boundary. Raw child-create input containing U+FFFD or an impossible `?` inside a Windows path is rejected instead of being durably persisted.

For a manually created existing child conversation, skip the spawn steps and run:

```powershell
python -m lws child-adopt CHILD_TASK `
  --conversation-url https://chatgpt.com/g/g-p-.../c/... `
  --json
```

## 4. Explicit automatic initial child-spawn

First arm. This reconciles the child workspace and writes durable authority but does not touch Chrome:

```powershell
python -m lws child-spawn-arm CHILD_TASK --json
```

Then explicitly authorize the one new normal-Chrome window:

```powershell
python -m lws child-spawn-open SPAWN_ATTEMPT `
  --enable-normal-browser-mutation `
  --confirm-child CHILD_TASK `
  --json
```

Only after it reaches `WINDOW_BOUND`, explicitly authorize the persisted prompt submission:

```powershell
python -m lws child-spawn-send SPAWN_ATTEMPT `
  --enable-normal-browser-mutation `
  --confirm-child CHILD_TASK `
  --wait-cooldown `
  --json
```

For tracked batch dispatchers, `--wait-cooldown` handles ChatGPT's `Too many requests` dialog as a proven **pre-send throttle state**. LWS detects the exact-window dialog before touching the composer, invokes `Got it` when that exact control is available, persists the global `web_child_dispatch` cooldown, and leaves the child in `WINDOW_BOUND`. The default cooldown is 120 seconds; `--rate-limit-cooldown` and `--max-cooldown-wait` bound it. A different child cannot bypass the same cooldown by taking the dispatcher immediately.

A new `/c/...` route is also not sufficient proof of delivery. LWS requires the persisted prompt to be consumed or generation to start; if the first Send only creates the route while leaving the exact same prompt send-ready, it may perform at most one durably recorded second-stage Send. It never performs a third automatic replay.

If either browser command returns an ambiguous state, **do not rerun it**. Reconcile instead:

```powershell
python -m lws child-spawn-reconcile SPAWN_ATTEMPT --json
python -m lws child-spawn-status CHILD_TASK --json
```

A result is successful only when the exact bound LWS-owned HWND becomes a conversation inside the expected stable web project id.

### Bounded batch dispatch

When many children are already persisted, prefer the batch helper over manually opening one page per task:

```powershell
python -m lws child-dispatch-batch PARENT_TASK `
  --max-windows 2 `
  --enable-normal-browser-mutation `
  --confirm-parent PARENT_TASK `
  --json
```

`child-dispatch-batch` is an explicit one-shot advancement command, not a hidden resident loop. It composes the existing `child-spawn-arm`, `child-spawn-open`, and `child-spawn-send` state machines and reports every action. Run it again after children bind their durable LSM sessions.

The pool rules are intentionally strict:

- `--max-windows` bounds the number of exact child dispatcher pages (1-16, default 2).
- A nonterminal child keeps its exact page until `child-bind-session` has durably recorded its LSM session. The batch will report `awaiting_lsm_binding` or `pool_wait` rather than stealing that page.
- Once a nonterminal child has a durable LSM session, its exact worker HWND may be navigated to the next persisted child. The old child continues through its durable worker/LSM state and no longer owns the browser binding.
- A terminal child may be recycled or closed even if it never bound an LSM session. `child-complete` clears the legacy worker binding, so terminal ownership is recovered from the newest unconsumed `COMPLETED` spawn record for that exact HWND/PID. Successful reuse/close durably marks that spawn record consumed so an older history record cannot reclaim the page later.
- If no undispatched child remains, exact terminal child pages are closed by default. `--keep-terminal-pages` disables that cleanup for operator inspection.
- `WINDOW_OPEN_SUBMITTED`, `PROMPT_SUBMITTED`, and `RECONCILE_REQUIRED` are batch stop states. The helper performs this ambiguity preflight for all children before starting any new browser mutation. Reconcile first; never use the batch command as a blind retry mechanism.
- The same normal-browser mutation opt-in and exact parent confirmation are required as for the individual spawn commands. The helper does not copy authentication state, call private endpoints, or bypass provider/browser controls.

A normal pool-full result is not a failure. The expected operator loop is: persist many children, run the batch helper, let dispatched children bind LSM, run the helper again, and let the exact bounded set of dispatcher windows move forward through the queue.

## 5. Child-agent bootstrap contract

The persisted child prompt should tell the new web chat worker to:

1. use Local Shell MCP for local execution;
2. start a **new** durable logical session for this child, or resume the already bound child logical session during replacement;
3. use Goal mode for multi-step work;
4. bind the new logical session id back to the durable LWS child:

```powershell
python -m lws child-bind-session CHILD_TASK --session-id s_...
```

5. read the real worktree/Git state before editing;
6. stay inside the assigned cwd/worktree/branch;
7. run the requested focused/full tests, diff checks, secret scan, or other acceptance gates;
8. commit only the assigned scope when the task is Git-backed;
9. use a concrete completion reference such as the verified Git commit hash, build artifact id, or explicit acceptance marker;
10. finish its Local Shell MCP Goal/session and report the completion reference.

For long child jobs the high-level scheduler lease defaults to two hours. It may be overridden explicitly. A parent should not infer task completion merely because the web chat message appears to end.

## 6. Durable completion

After the assigned work is locally complete, end the worker/task with a concrete reference:

```powershell
python -m lws child-complete CHILD_TASK `
  --completion-ref commit:ABCDEF123456 `
  --json
```

`child-complete` first completes the authoritative worker, then the durable child task, and aligns the legacy task state to `COMPLETED`. If a crash occurs between those transitions, rerunning the command with the same completion reference safely finishes the remaining transition. A different completion reference is rejected once the child is complete.

The parent monitors all children with:

```powershell
python -m lws child-status PARENT_TASK --json
```

The parent should independently verify each completion reference and actual Git/workspace state before integrating a child commit.

## 7. Replacement when a child conversation fails

Do not replace a worker only because its browser window disappeared. Reconcile the child's Local Shell MCP session, Goal, jobs/in-flight calls, workspace/Git state, and unresolved browser actions first.

The current release requires the replacement web conversation to already exist and be explicitly known. Register it as a candidate:

```powershell
python -m lws replacement-register CHILD_TASK `
  --conversation-url https://chatgpt.com/g/g-p-.../c/... `
  --json
```

Arm the replacement:

```powershell
python -m lws replacement-arm CHILD_TASK `
  --candidate-worker-id NEW_WORKER `
  --json
```

Then persist one-time Local Shell MCP takeover authority:

```powershell
python -m lws replacement-submit REPLACEMENT_ATTEMPT --json
```

The command prints the supported Local Shell MCP request. The parent AI calls that `session_manage(action="resume", session_id=..., takeover=true)` request **exactly once**. If the tool result is lost or ambiguous, do not call it again; reconcile the existing Local Shell MCP session first.

After the new active Local Shell run is observable and the workspace fence is unchanged:

```powershell
python -m lws replacement-complete REPLACEMENT_ATTEMPT `
  --new-run-id r_... `
  --json
```

Only then is the new conversation granted a newer LWS worker generation.

## 8. Parent-AI decision rule

For each child, the parent should reason in this order:

1. **Is the durable child already completed?** Verify the completion reference; do not send more work.
2. **Is a write-ahead browser/replacement operation unresolved?** Reconcile it; never start another mutation.
3. **Does the current worker still own a valid generation and live Local Shell run?** Let it continue; do not duplicate the child.
4. **Did the worker fail but local execution may have progressed?** Reconcile LSM + Git before deciding what remains incomplete.
5. **Is replacement proven safe?** Use the replacement state machine and supported LSM takeover boundary.
6. **Is evidence ambiguous?** Stop at human review instead of guessing.

The scheduler is designed so that “continue the work” means continue the **durable task from the first genuinely incomplete step**, not repeat the previous web chat message.

## 9. What LWS still does not automate

- resident-watchdog creation of arbitrary child tasks;
- automatic creation of replacement web chat conversations;
- automatic integration/merge of child commits without parent verification;
- automatic live-worker page closing;
- private web chat endpoints or authentication-state copying;
- blind resend/reopen/takeover after an ambiguous external result.
