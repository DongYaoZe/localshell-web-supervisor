# V0 specification

## Goal

Produce a local, deterministic watchdog that can answer four questions without invoking OpenAI APIs:

1. What durable tasks exist?
2. Which conversation worker currently represents each task?
3. Is the task demonstrably running, completed, blocked, or suspicious?
4. If suspicious, what should be reconciled before recovery?

V0 is useful even before browser auto-control exists because it centralizes durable identity and removes unsafe inference from ChatGPT UI alone.

## Commands

- `cws register`: create durable task identity and optional initial worker.
- `cws status`: list durable registry states.
- `cws add-worker`: rotate the current conversation worker while preserving task identity/history.
- `cws track-job`: attach an LSM job id to a task for deterministic job evidence.
- `cws checkpoint`: persist a task checkpoint that survives worker replacement (`--file`
  is the preferred Windows PowerShell 5.1 input form; `--json` is also supported).
- `cws observe-dom`: ingest normalized DOM probe output (`--file` is preferred on Windows
  PowerShell 5.1; BOM-prefixed UTF-8 is accepted).
- `cws observe-snapshot`: normalize and ingest an LSM high-level browser snapshot (same
  PowerShell file-input guidance).
- `cws inspect`: refresh read-only LSM telemetry and classify one task.
- `cws watch`: resident watchdog; scan tasks periodically and emit only when the attention
  queue changes (`--once` performs one scan).
- `cws recommend`: produce a conservative recovery recommendation and reconciliation prompt.
- `cws recovery-history`: inspect the recommendation audit trail.

## DOM observation contract

A V0 probe may provide:

```json
{
  "observed_at": 1787000000.0,
  "url": "https://chatgpt.com/c/...",
  "generating": false,
  "send_button_ready": true,
  "pending_tool_calls": 1,
  "visible_error": null,
  "last_dom_change_at": 1786999800.0,
  "message_signature": "optional stable digest"
}
```

No selector strategy is hard-coded into the core classifier. ChatGPT DOM changes should affect the probe adapter, not task semantics.

When a producer supplies `text_tail` or a stable `message_signature`, CWS derives
`last_dom_change_at` by comparing consecutive observations. The LSM v4.0.1 high-level
snapshot captures `body.innerText.slice(0, limit)`. If its `text_truncated` flag is true,
CWS deliberately refuses to use that body prefix as a latest-message signature because a
long conversation can keep changing beyond the captured prefix. The direct DOM probe
contract therefore requires a body **tail** or, preferably, a latest-message-region digest.

## LSM observation contract

The file adapter reads only durable state. It currently consumes:

- session status and active run;
- Goal plan status/activity/steps/continuation flags;
- serialized in-flight tool leases, considering only heartbeats younger than the inspected 2 h lease;
- explicitly tracked job IDs from `jobs.json`;
- terminal per-attempt status payloads as monotonic completion evidence when `jobs.json`
  has not yet caught up;
- recent activity event timestamp/type.

It never edits `sessions/*.json` or `jobs.json`.
Unknown durable schema versions are a hard compatibility error, not a signal to fall
back to stale telemetry.

Repeated watchdog polls do not append identical LSM observations: CWS records a new row
only when semantic evidence changes. Browser, LSM, and workspace observation histories are bounded
per worker/task so a resident watchdog cannot grow the registry without limit.

## Workspace reconciliation contract

`inspect` and `recommend` probe the registered working directory read-only. For Git
repositories CWS records the repository root, current `HEAD` when one exists, dirty state,
a SHA-256 digest of porcelain status, and a bounded list of changed paths. This gives the
recovery layer direct evidence that a previous turn may already have committed or changed
files even if the ChatGPT tool card never reached a terminal UI state.

The resident watchdog avoids spawning Git for every healthy task on every poll: workspace
reconciliation is performed when preliminary evidence is suspicious, while explicit
`inspect`/`recommend` always refresh it. A missing registered cwd with no live durable LSM
work escalates to `NEEDS_HUMAN`; live LSM tool/job evidence still wins while work is active.

## Stall policy defaults

Defaults are deliberately configurable rather than claims about ChatGPT internals:

- DOM silence suspect threshold: 120 s;
- LSM event silence suspect threshold: 180 s;
- hard-stall confidence threshold: 600 s;
- LSM Goal continuation lease: consumed from LSM plan data when present (900 s in inspected v4.0.1).

A future calibration dataset should replace guesswork with observed distributions from real long turns.

## Recovery policy

V0 recovery is **advisory only**. `safe_to_dispatch` is always false for continue/takeover recommendations. The generated instruction requires the next worker to inspect current LSM state, jobs, and workspace before doing anything.

Automatic dispatch is a V1 feature only after:

- a reconcile result is durable;
- LSM has no live in-flight call;
- no continuation race exists;
- recovery budget is available;
- the action can be correlated to a specific worker/run;
- duplicate side effects are fenced or demonstrably absent.

## Non-goals

- OpenAI API / Agents SDK migration;
- ChatGPT private endpoint client;
- cookie/token extraction;
- full browser fleet manager;
- autonomous retries based only on DOM buttons;
- replacing Local Shell MCP jobs, sessions, plans, leases, or takeover.
