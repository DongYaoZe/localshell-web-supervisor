# LWS 0.10 / Schema v8: AI Child Scheduling

## Goal

LWS 0.10 turns the persisted replaceable-worker protocol into a usable parent-AI scheduler for web chat + Local Shell MCP. The durable task remains the unit of work; a web chat conversation is a replaceable worker lease, and a Local Shell MCP logical session is the durable local execution lineage.

0.10 adds three product layers on top of the schema-v6 worker protocol:

1. durable parent-to-child dispatch contracts;
2. guarded replacement-worker reconciliation through supported Local Shell MCP takeover semantics;
3. explicitly gated creation of an **initial** child conversation inside one confirmed web project.

It does not add a private web chat transport, copied browser authentication, general unattended page mutation, or automatic replacement-chat creation.

## Schema v7: child dispatch and replacement intent

Schema v7 adds `child_dispatches` and `replacement_attempts`.

`child_dispatches` stores the parent-to-child contract separately from mutable task checkpoints:

- parent task id, child task id, and unique child key;
- exact prompt text plus SHA-256 digest;
- project/objective/cwd through the ordinary task row;
- optional expected Git branch and base ref;
- optional bounded metadata.

Creating a child task, its worker-protocol lineage, and the dispatch contract is one SQLite transaction. `(parent_task_id, child_key)` is unique. Reusing a key with a different contract is rejected rather than silently changing the assignment.

`replacement_attempts` is a write-ahead record for the boundary between LWS worker authority and Local Shell MCP execution authority. The state path is:

`ARMED -> LSM_TAKEOVER_SUBMITTED -> COMPLETED | RECONCILE_REQUIRED`

The parent AI must persist `LSM_TAKEOVER_SUBMITTED` **before** it makes the one supported `session_manage(action="resume", session_id=..., takeover=true)` call. If that call's result is lost or ambiguous, the parent reconciles; it does not replay the takeover call.

## Schema v8: explicit project binding and child-spawn

Schema v8 additively adds `child_dispatches.web_project_url` and `child_spawn_attempts`.

The supplied project URL must be an ordinary `https://chatgpt.com` Project root. LWS extracts the stable `g-p-<project-id>` identity rather than depending on a cosmetic project slug. This is necessary because web chat may canonicalize between slugged/slugless project URLs or add `/project` while preserving the same project identity.

The child-spawn write-ahead path is:

`ARMED -> WINDOW_OPEN_SUBMITTED -> WINDOW_BOUND -> PROMPT_SUBMITTED -> COMPLETED`

Any ambiguous externally submitted phase becomes `RECONCILE_REQUIRED`. A proven no-side-effect failure may return to a safe retryable state; an unknown result may not be replayed.

### Browser ownership fence

Initial child-spawn is deliberately narrow:

- initial-worker only: the child must have generation 0 and no worker history;
- one explicit web project URL is part of the dispatch contract;
- a new top-level normal Chrome window is opened only after `WINDOW_OPEN_SUBMITTED` is durable;
- the project URL receives a non-secret LWS ownership fragment for the spawn attempt;
- exactly one tagged project window must be observed before binding;
- LWS binds exact HWND, Chrome PID, executable path, project identity, and owner token;
- prompt submission requires a second explicit mutation opt-in and exact child confirmation;
- before typing, the same bound HWND is read again and its current canonical project URL becomes the literal UIA exact-URL fence;
- success is accepted only when that **same HWND** reaches a `/c/...` conversation in the expected stable project id;
- transient generic web chat router URLs may be observed during the bounded wait, but are never accepted as success;
- another project or another host fails closed.

LWS never enumerates or types into unrelated tabs to find a place to run the child.

## Parent scheduler primitives

### Dispatch and manual adoption

- `lws child-create`: atomically persist one child assignment. No browser, LSM, Git, or worktree mutation occurs.
- `lws child-prompt`: print the exact persisted prompt.
- `lws child-bind-session`: bind the child's stable Local Shell MCP logical session exactly once. Replacement conversations reuse this session id.
- `lws child-adopt`: make an explicitly supplied existing conversation the initial current worker when the child has no authoritative worker.
- `lws child-status`: show child task state, LSM session id, protocol revision/generation, current worker and worker leases.
- `lws child-complete`: complete the current worker and durable child task with a non-empty completion reference, then align the legacy task state to `COMPLETED`. Repeating the same completion reference is idempotent; a different reference is rejected.

The high-level scheduler worker lease default is two hours and can be overridden explicitly.

### Explicit initial child-spawn

- `lws child-spawn-arm`: read-only reconcile the child workspace and durably arm one initial spawn. No browser mutation.
- `lws child-spawn-open`: requires `--enable-normal-browser-mutation` and exact `--confirm-child`; opens at most the one authorized tagged project window.
- `lws child-spawn-send`: requires the same explicit opt-in/confirmation; sends only the persisted prompt to the exact bound window.
- `lws child-spawn-reconcile`: read-only browser reconciliation of an interrupted spawn. It never opens another window or resends the prompt.
- `lws child-spawn-status`: inspect the durable spawn write-ahead history.

### Replacement worker flow

- `lws replacement-register`: register an exact existing replacement conversation as a non-authoritative candidate.
- `lws replacement-arm`: reconcile current LSM/workspace/action state and write-ahead arm a replacement.
- `lws replacement-submit`: persist one-time LSM takeover authority and print the supported Local Shell MCP request.
- the parent AI calls the printed `session_manage` request exactly once;
- `lws replacement-complete`: after a fresh observation proves the new Local Shell MCP active run and unchanged workspace fence, publish the candidate as the new LWS generation;
- `lws replacement-status`: inspect write-ahead replacement history.

0.10 does **not** automatically create the replacement web chat conversation. The candidate URL must already be explicitly known. This keeps replacement page creation separate from LSM takeover authority.

## Replacement safety gates

A disappeared or stalled worker is not replaced merely because its browser window is missing. Before replacement, LWS requires:

- no unresolved browser action for the child;
- no unresolved global probe mutation;
- no unresolved child-spawn/replacement race;
- a bound durable LSM logical session;
- fresh LSM evidence with no in-flight tool call, tracked job, or continuation;
- fresh workspace/Git reconciliation;
- expected branch consistency when a branch was declared;
- an explicitly registered candidate worker;
- unchanged worker-protocol revision/generation until takeover publication.

The inherited 0.9.2 incident is the canonical negative case: an old worker window disappeared while its external browser action remained `SUBMITTED`. Missing-window evidence does not prove the earlier send did not happen, so replacement must remain blocked until that ambiguity is independently resolved.

## Live acceptance evidence

The 0.10 acceptance used an isolated child under the LWS web project and an ignored `.lws` work directory. The parent:

1. persisted the child assignment and explicit project root;
2. armed a spawn without browser mutation;
3. opened one LWS-tagged normal-Chrome window and bound its exact HWND/PID;
4. submitted the persisted child prompt once;
5. reconciled the resulting same-project conversation without replay after an ambiguous return;
6. observed the new child Chat start its own Local Shell MCP durable logical session in Goal mode;
7. observed that child bind its session id back to the LWS task, write and verify the requested marker, and finish the logical session;
8. completed the durable child with the verified marker as completion reference.

The acceptance also exposed two route-handling defects before release: project-root slug canonicalization before send and a transient web chat router URL after send. Both were fixed without weakening exact HWND/PID ownership or permitting blind replay, and both now have regression coverage.

## Non-goals and safety boundary

LWS 0.10 does not authorize:

- OpenAI API use as a substitute for web chat;
- web chat private backend endpoint reconstruction;
- cookie/token/password extraction or sign-in-state copying;
- CAPTCHA/authentication bypass;
- typing or clicking in unrelated browser tabs;
- unbounded conversation/window creation;
- automatic replacement-chat creation;
- automatic live-worker page closing;
- replay of a browser send, page open, or LSM takeover whose external outcome is ambiguous.

See `CHILD_SCHEDULER.md`, `OPERATIONS.md`, and `SAFETY.md` for the operator/AI workflow and safety rules.
