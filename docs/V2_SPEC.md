# V2 specification: low-memory worker and browser orchestration

V2 turns the ~8 GB RAM constraint into an explicit scheduler input. The control layer inventories memory pressure, ranks workers, tracks page leases, and rotates probe targets without equating one durable task with one permanent browser tab.

## Memory telemetry

`lws ram-status` reads physical memory and aggregate Chrome process working-set information. It does not inspect browser command lines, cookies, credentials, or page bodies for RAM accounting.

Chrome is multi-process, so the working set of one window process is not treated as the full cost of one tab. Memory pressure changes scheduling advice; it never overrides task-safety fences by itself.

## Pool planner

`lws pool-plan` combines task/worker state, durable LSM evidence, latest browser observation, and memory pressure.

Default planning targets:

- active workers: 4;
- probe pages: 1;
- low-memory pressure: <= 1 GiB available or >= 85% physical-memory use.

### `DO_NOT_CLOSE`

The current control plane still pins a worker when any strong/ambiguous live condition exists:

- LSM in-flight tool call;
- tracked active job;
- continuation pending;
- browser reports active generation;
- task state `STARTING`, `RUNNING`, `RECOVERING`, `RECONCILING`, `SUSPECT`, or `NEEDS_HUMAN`.

This remains true even though isolated experiments prove that generation and one real Local Shell tool chain can survive page closure. Automatic live-worker closing is a separate policy decision and is **not enabled**.

### `PARK_CANDIDATE`

Terminal/idle states with no live evidence are ranked:

1. `COMPLETED`;
2. `ABANDONED`;
3. `QUEUED`;
4. `BLOCKED`.

The release does not hard-code page-close capability globally. `pool-plan` defaults to `page_close_experiment_passed=false`. Validated evidence can be imported into a versioned, expiring, context-bound capability and then explicitly selected with `--page-close-capability`; the legacy `--page-close-evidence FILE` path remains compatible. Only already non-live candidates can become `close_allowed=true`. The planner itself still does not close pages.

### `NO_PAGE`

Workers already marked `parked`, `superseded`, or `dead` do not count as resident page leases.

`lws worker-status WORKER parked` is bookkeeping for a page independently closed/detached; the command itself does not close a browser.

## Ephemeral page pool

`PagePool` is transport-neutral and tracks runtime page leases only. Durable task/worker identity remains in SQLite.

Roles:

- `ACTIVE`: page assigned to an executing/interactive worker;
- `PROBE`: small reusable page pool for sequentially revisiting parked URLs.

The pool fails closed on capacity instead of implicitly evicting a page. Schema v4 additionally records at most one reusable durable probe slot: same-target observation reuses it; another parked target requires exact-close-before-open; stale or ambiguous ownership blocks rather than opening another window. Schema v5 adds write-ahead probe mutation fencing, but the actual probe-window mutation transport remains disabled by default. A probe policy may block visual-only image/media/font resources, while documents, scripts, stylesheets, XHR/fetch, and WebSocket remain available for state detection.

## Empirical page-close findings

The key question was whether web chat work continues when no page displays the conversation.

### Ordinary generation

A normally authenticated disposable window was closed while a positive Stop control showed generation in progress. No other window displayed the conversation. After 15 seconds, reopening the exact conversation showed the complete expected long response and no duplicate user turn.

Result: `generation_parking_safe` can be proven by the evidence evaluator and, in 0.6, represented as durable capability provenance tied to the experiment time and runtime context.

### Local Shell tool execution

A separate disposable project conversation started a real Local Shell tracked job. The close harness required both:

- exact target job durably `running`;
- web chat Stop visible.

It wrote pre-close evidence before closing the only experiment window. While the page was absent, the job completed successfully and wrote its expected ignored marker. Reopening the same conversation showed the model's final post-tool completion response.

Result: `tool_execution_parking_safe` can be proven when the stronger tool fields all pass.

Run the evidence gate with:

```powershell
python -m lws evaluate-page-close --file .lws\page-close-evidence.json --json
```

The preferred path validates/imports evidence once and then explicitly selects the durable capability:

```powershell
python -m lws capability-import --file .lws\page-close-evidence.json --json
python -m lws capability-status --json
python -m lws pool-plan --page-close-capability latest --json
```

The one-shot `--page-close-evidence` path remains for compatibility.

For a worker that may have live tool execution:

```powershell
python -m lws evaluate-page-close `
  --file .lws\page-close-evidence.json `
  --require-tool `
  --json
```

The two gates are intentionally distinct: ordinary generation evidence never silently authorizes live-tool parking.

## Why live workers are still pinned

The experiment proves capability on the inspected deployment, not universal browser/platform semantics forever. Before production automatic live-page parking, LWS still needs a deliberate policy that binds:

- exact current worker URL/HWND;
- an explicitly selected, context-matching, unexpired page-close capability;
- durable LSM execution state;
- recovery/checkpoint guarantees;
- RAM-pressure policy;
- single-slot reopen/probe guarantees with no implicit page proliferation.

Until that policy is explicit, `live_lsm` and `generating=True` remain `DO_NOT_CLOSE`. This preserves the conservative behavior while removing the earlier uncertainty about whether page closure necessarily kills the work.
