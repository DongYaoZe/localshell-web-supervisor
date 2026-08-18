# chatgpt-web-supervisor

`chatgpt-web-supervisor` (CWS) is a local control plane for long-running work performed through **ChatGPT Web + Local Shell MCP**.

The central rule is:

> **A ChatGPT conversation is a replaceable worker lease. The durable task is not the conversation.**

CWS does not use the OpenAI API and does not reimplement ChatGPT's private web protocol. V0 observes browser/DOM evidence and Local Shell MCP durable telemetry, classifies task health, and recommends conservative recovery only after reconciliation.

## Why

Long ChatGPT Web turns can fail independently of local side effects. A tool may have completed while the page still shows an `ing` card; the composer can become usable while delivery is broken; a `Message delivery timed out` error can appear after local mutations already happened. Blindly replaying the prior turn can therefore duplicate commits, writes, uploads, or other side effects.

CWS separates three truth domains:

1. **ChatGPT UI/DOM** — what the page currently renders.
2. **ChatGPT transport activity** — V1 will add richer network/CDP evidence.
3. **Local Shell MCP durable execution state** — logical sessions, Goal plans, in-flight tool leases, tracked jobs, and workspace checkpoints.

Completion and recovery decisions are never based on the Send/Stop button alone.

## Current V0

V0 is intentionally advisory and safe:

- SQLite task/worker registry;
- durable task identity independent of conversation URL;
- direct **read-only** ingestion of Local Shell MCP file-state sessions/plans/jobs;
- read-only workspace reconciliation (cwd, Git HEAD, dirty/status digest);
- DOM observation and LSM browser-snapshot normalization boundary;
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
python -m cws inspect example
python -m cws watch --once
python -m cws recommend example
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

## Safety boundary

CWS V0 never treats `continue` as idempotent. Before any recovery, it asks for reconciliation against durable LSM state and the actual workspace. Automatic recovery is deferred until the system can prove a recovery action is fenced, budgeted, and safe.

## Roadmap

- **V0:** DOM + LSM telemetry + registry + watchdog + recovery recommendations.
- **V1:** CDP/network observability, robust reconcile, guarded takeover, deterministic scheduler.
- **V2:** low-RAM browser orchestration, active-worker pool, parked conversations, probe-page reuse.
- **V3:** only if evidence demands it, minimal Web transport research; private API reimplementation is not the default direction.
