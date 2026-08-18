# chatgpt-web-supervisor

`chatgpt-web-supervisor` (CWS) is a local control plane for long-running work performed through **ChatGPT Web + Local Shell MCP**.

The central rule is:

> **A ChatGPT conversation is a replaceable worker lease. The durable task is not the conversation.**

CWS does not use the OpenAI API and does not reimplement ChatGPT's private web protocol. The current V3 combines read-only browser evidence, optional network-lifecycle metadata, Local Shell MCP durable telemetry, actual workspace/Git state, low-memory worker planning, semantic reconciliation fences, and a disabled dry-run recovery dispatcher.

## Why

Long ChatGPT Web turns can fail independently of local side effects. A tool may have completed while the page still shows an `ing` card; the composer can become usable while delivery is broken; a `Message delivery timed out` error can appear after local mutations already happened. Blindly replaying the prior turn can therefore duplicate commits, writes, uploads, or other side effects.

CWS separates three truth domains:

1. **ChatGPT UI/DOM** — what the page currently renders.
2. **ChatGPT transport activity** — optional V1 CDP/network lifecycle evidence when CWS safely owns or is explicitly allowed to observe a DevTools endpoint.
3. **Local Shell MCP durable execution state** — logical sessions, Goal plans, in-flight tool leases, tracked jobs, and workspace checkpoints.

Completion and recovery decisions are never based on the Send/Stop button alone.

## Current V3

V3 remains fail-closed and safe:

- SQLite task/worker registry;
- durable task identity independent of conversation URL;
- direct **read-only** ingestion of Local Shell MCP file-state sessions/plans/jobs;
- read-only workspace reconciliation (cwd, Git HEAD, dirty/status digest);
- DOM observation and LSM browser-snapshot normalization boundary;
- exact-URL, read-only Windows UI Automation observation of an existing authenticated Chrome tab;
- optional CDP Network-domain lifecycle telemetry that stores no headers, cookies, POST data, or response bodies;
- three-signal stall classification across browser/DOM, optional network lifecycle, and durable LSM state;
- durable sanitized reconciliation records with deterministic evidence `fence_token`s;
- system/aggregate Chrome RAM telemetry and conservative active/park/probe planning;
- mandatory two-sample semantic-fence dispatch planning;
- an audited `dispatch-plan` command whose action transport is hard-disabled;
- deterministic health classifier;
- low-noise resident attention watchdog with a SQLite singleton lease/heartbeat;
- conservative recovery recommendation and idempotent recovery prompt;
- **no automatic click/retry/takeover dispatch yet**.

### Quick start

```powershell
$env:PYTHONPATH = "src"
python -m cws --help

python -m cws register `
  --task-id example `
  --project demo `
  --objective "finish the demo safely" `
  --cwd D:\work\demo `
  --session-id s_abc123 `
  --conversation-url https://chatgpt.com/c/example

python -m cws status
python -m cws inspect example --uia
python -m cws watch --once
python -m cws reconcile example --uia
python -m cws recommend example --uia
python -m cws ram-status
python -m cws pool-plan
python -m cws dispatch-plan example --uia
```

For browser telemetry, `cws observe-dom` ingests the transport-neutral V0 probe shape and
`cws observe-snapshot` can consume an LSM high-level browser snapshot. CWS does not trust
a truncated LSM body-text prefix as a latest-message heartbeat; long-conversation probes
must provide a DOM tail or latest-message digest.

On Windows PowerShell 5.1, prefer JSON `--file` inputs for `checkpoint`, `observe-dom`,
and `observe-snapshot` over inline `--json`, because native-command quoting can strip
JSON quotes. File input accepts both ordinary UTF-8 and PowerShell 5.1's BOM-prefixed
UTF-8 output.

The registry defaults to `.cws/registry.sqlite3`. Override with `--db` or `CWS_DB`.
Local Shell state is detected from `CWS_LSM_STATE_DIR`, `LOCAL_SHELL_MCP_STATE_DIR`, or the hardened Windows deployment path when present. `--lsm-state-dir` always wins.

`cws watch` is the resident watchdog. It scans every 30 seconds by default and emits
only when the attention queue changes; use `--interval N` to tune it or `--once` for
cron/scripts/smoke tests. Healthy running tasks stay quiet.

The V0 file adapter is deliberately **schema-version gated**. It currently understands
the inspected v4.0.1 file backend (`session version=1`, `jobs version=2`). If LSM changes
those durable formats, CWS fails closed rather than silently making a recovery decision
from a schema it does not understand. See [`docs/LSM_FINDINGS.md`](docs/LSM_FINDINGS.md).

## Local Shell MCP findings from the bootstrap environment

The initial implementation was grounded against Local Shell MCP **v4.0.1**, not a guessed API surface:

- logical sessions and Goal plans are durable JSON state;
- Goal mode has a 900 s inactivity/execution lease and at most 10 continuation attempts;
- each tool call gets a durable per-run in-flight lease before execution; failure to persist that lease is fail-closed;
- in-flight leases are heartbeat-renewed and fence logical-session takeover;
- `resume(..., takeover=true)` supersedes an active run only when no tool calls are in flight;
- tracked jobs have a durable `jobs.json`, attempt log/status files, and interrupted-state reconciliation;
- persistent shells themselves are **not** durable across server restart;
- high-level browser sessions are in-memory, limited to 8, idle-cleaned after 1 hour; profiles/storage state are persistent;
- browser snapshots already retain bounded response/request-failure evidence, but not enough stream lifecycle timing for robust ChatGPT stall detection;
- in the bootstrap Windows/ConPTY experiment, `job_stop` marked resident test jobs stopped while their PowerShell/Python descendants remained alive, so CWS is fenced by its own watchdog lease and should be hosted independently of an ordinary tracked shell job.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/V0_SPEC.md`](docs/V0_SPEC.md).

V1 browser/network/reconcile details are in [`docs/V1_SPEC.md`](docs/V1_SPEC.md),
[`docs/NETWORK_OBSERVABILITY.md`](docs/NETWORK_OBSERVABILITY.md), and
[`docs/SAFETY.md`](docs/SAFETY.md).

The V2 low-memory planner/page-pool layer is documented in
[`docs/V2_SPEC.md`](docs/V2_SPEC.md). `cws ram-status` reports system/aggregate Chrome
memory, while `cws pool-plan` produces `DO_NOT_CLOSE` / `PARK_CANDIDATE` advice. It does
not close pages.

V3's private-transport NO-GO decision and deterministic dry-run dispatcher are documented
in [`docs/V3_DECISION.md`](docs/V3_DECISION.md) and [`docs/V3_SPEC.md`](docs/V3_SPEC.md).

For day-to-day use, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md). `cws doctor` is a
read-only preflight for registry/LSM schema, RAM, watchdog lease, workspace/task state,
and optional exact-URL UIA. It never repairs or changes browser/task state.

## Safety boundary

CWS never treats `continue` as idempotent. Before any recovery, it requires reconciliation against durable LSM state, browser evidence, and the actual workspace. It does not copy authentication state, bypass access controls, or reconstruct private ChatGPT endpoints. Automatic recovery is deferred until the system can prove an action is fenced, budgeted, and safe.

Browser telemetry is also data-minimized: UIA does not read the unsent prompt draft;
`probe-uia` returns state/signature diagnostics rather than conversation text; persisted
UIA/LSM snapshot raw metadata is reduced to the numeric/state fields needed by the watchdog.

## Roadmap

- **V0:** DOM + LSM telemetry + registry + watchdog + recovery recommendations.
- **V1:** read-only UIA/CDP observability + three-signal classification + durable reconciliation fences. Implemented; recovery dispatch remains disabled.
- **V2:** low-RAM worker planner, active/probe page-pool primitives, parked-worker bookkeeping, and RAM telemetry. Implemented conservatively; ChatGPT page-close dispatch remains disabled pending a dedicated authenticated experiment.
- **V3:** evidence says no private Web transport is currently needed; semantic two-phase fences + disabled dry-run recovery/takeover planning are implemented. There is still no automatic click/type/continue/takeover transport.
