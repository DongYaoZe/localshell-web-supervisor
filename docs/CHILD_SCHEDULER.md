# AI child scheduler runbook

This is the operator/parent-AI workflow for CWS 0.10. It is intentionally split into durable bookkeeping, explicit browser mutation, Local Shell MCP execution, and completion so that a failed web turn can be reconciled without blindly repeating an external side effect.

## 1. Parent preflight

Before dispatching children, reconcile the parent task and actual repository:

```powershell
$env:PYTHONPATH = "src"
python -m cws doctor
python -m cws watchdog-status
```

Also inspect the parent's Local Shell MCP logical session, Goal plan, active jobs/in-flight calls, and Git status. Do not dispatch onto a worktree whose ownership or current state is ambiguous.

## 2. Decompose work

A useful child assignment should be independently verifiable and should name:

- one durable child key/task id;
- project and objective;
- exact cwd/worktree;
- expected branch and base ref when Git isolation matters;
- explicit ChatGPT Project root if automatic initial child-spawn may be used;
- an exact prompt that tells the child what it owns, what it must not touch, what tests/checks to run, and what completion reference to return.

Prefer fewer substantial children over many tiny conversational jobs. The parent remains responsible for integration and independent verification.

## 3. Persist the child before opening ChatGPT

Example:

```powershell
python -m cws child-create PARENT_TASK `
  --child-key worker-a `
  --child-task-id CHILD_TASK `
  --project my-project `
  --objective "implement isolated feature A" `
  --cwd D:\worktrees\feature-a `
  --expected-branch agent/feature-a `
  --base-ref ABC123 `
  --chatgpt-project-url https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef `
  --prompt-file .cws\prompts\feature-a.txt `
  --json
```

`child-create` performs no browser, Local Shell MCP, or Git mutation. The prompt and its digest become durable dispatch state before a child conversation exists.

For a manually created existing child conversation, skip the spawn steps and run:

```powershell
python -m cws child-adopt CHILD_TASK `
  --conversation-url https://chatgpt.com/g/g-p-.../c/... `
  --json
```

## 4. Explicit automatic initial child-spawn

First arm. This reconciles the child workspace and writes durable authority but does not touch Chrome:

```powershell
python -m cws child-spawn-arm CHILD_TASK --json
```

Then explicitly authorize the one new normal-Chrome window:

```powershell
python -m cws child-spawn-open SPAWN_ATTEMPT `
  --enable-normal-browser-mutation `
  --confirm-child CHILD_TASK `
  --json
```

Only after it reaches `WINDOW_BOUND`, explicitly authorize the persisted prompt submission:

```powershell
python -m cws child-spawn-send SPAWN_ATTEMPT `
  --enable-normal-browser-mutation `
  --confirm-child CHILD_TASK `
  --json
```

If either browser command returns an ambiguous state, **do not rerun it**. Reconcile instead:

```powershell
python -m cws child-spawn-reconcile SPAWN_ATTEMPT --json
python -m cws child-spawn-status CHILD_TASK --json
```

A result is successful only when the exact bound CWS-owned HWND becomes a conversation inside the expected stable ChatGPT Project id.

## 5. Child-agent bootstrap contract

The persisted child prompt should tell the new ChatGPT worker to:

1. use Local Shell MCP for local execution;
2. start a **new** durable logical session for this child, or resume the already bound child logical session during replacement;
3. use Goal mode for multi-step work;
4. bind the new logical session id back to the durable CWS child:

```powershell
python -m cws child-bind-session CHILD_TASK --session-id s_...
```

5. read the real worktree/Git state before editing;
6. stay inside the assigned cwd/worktree/branch;
7. run the requested focused/full tests, diff checks, secret scan, or other acceptance gates;
8. commit only the assigned scope when the task is Git-backed;
9. use a concrete completion reference such as the verified Git commit hash, build artifact id, or explicit acceptance marker;
10. finish its Local Shell MCP Goal/session and report the completion reference.

For long child jobs the high-level scheduler lease defaults to two hours. It may be overridden explicitly. A parent should not infer task completion merely because the ChatGPT message appears to end.

## 6. Durable completion

After the assigned work is locally complete, end the worker/task with a concrete reference:

```powershell
python -m cws child-complete CHILD_TASK `
  --completion-ref commit:ABCDEF123456 `
  --json
```

`child-complete` first completes the authoritative worker, then the durable child task, and aligns the legacy task state to `COMPLETED`. If a crash occurs between those transitions, rerunning the command with the same completion reference safely finishes the remaining transition. A different completion reference is rejected once the child is complete.

The parent monitors all children with:

```powershell
python -m cws child-status PARENT_TASK --json
```

The parent should independently verify each completion reference and actual Git/workspace state before integrating a child commit.

## 7. Replacement when a child conversation fails

Do not replace a worker only because its browser window disappeared. Reconcile the child's Local Shell MCP session, Goal, jobs/in-flight calls, workspace/Git state, and unresolved browser actions first.

0.10 requires the replacement ChatGPT conversation to already exist and be explicitly known. Register it as a candidate:

```powershell
python -m cws replacement-register CHILD_TASK `
  --conversation-url https://chatgpt.com/g/g-p-.../c/... `
  --json
```

Arm the replacement:

```powershell
python -m cws replacement-arm CHILD_TASK `
  --candidate-worker-id NEW_WORKER `
  --json
```

Then persist one-time Local Shell MCP takeover authority:

```powershell
python -m cws replacement-submit REPLACEMENT_ATTEMPT --json
```

The command prints the supported Local Shell MCP request. The parent AI calls that `session_manage(action="resume", session_id=..., takeover=true)` request **exactly once**. If the tool result is lost or ambiguous, do not call it again; reconcile the existing Local Shell MCP session first.

After the new active Local Shell run is observable and the workspace fence is unchanged:

```powershell
python -m cws replacement-complete REPLACEMENT_ATTEMPT `
  --new-run-id r_... `
  --json
```

Only then is the new conversation granted a newer CWS worker generation.

## 8. Parent-AI decision rule

For each child, the parent should reason in this order:

1. **Is the durable child already completed?** Verify the completion reference; do not send more work.
2. **Is a write-ahead browser/replacement operation unresolved?** Reconcile it; never start another mutation.
3. **Does the current worker still own a valid generation and live Local Shell run?** Let it continue; do not duplicate the child.
4. **Did the worker fail but local execution may have progressed?** Reconcile LSM + Git before deciding what remains incomplete.
5. **Is replacement proven safe?** Use the replacement state machine and supported LSM takeover boundary.
6. **Is evidence ambiguous?** Stop at human review instead of guessing.

The scheduler is designed so that “continue the work” means continue the **durable task from the first genuinely incomplete step**, not repeat the previous ChatGPT message.

## 9. What 0.10 still does not automate

- resident-watchdog creation of arbitrary child tasks;
- automatic creation of replacement ChatGPT conversations;
- automatic integration/merge of child commits without parent verification;
- automatic live-worker page closing;
- private ChatGPT endpoints or authentication-state copying;
- blind resend/reopen/takeover after an ambiguous external result.
