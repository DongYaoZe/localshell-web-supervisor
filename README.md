# chatgpt-web-supervisor

`chatgpt-web-supervisor` (CWS) is a local control plane for long-running work performed through **ChatGPT Web + Local Shell MCP**.

The central rule is:

> **A ChatGPT conversation is a replaceable worker lease. The durable task is not the conversation.**

CWS does not use the OpenAI API and does not reimplement ChatGPT's private web protocol. The current 0.9 control plane combines read-only browser evidence, optional network-lifecycle metadata, Local Shell MCP durable telemetry, actual workspace/Git state, low-memory worker planning, semantic reconciliation fences, durable crash-fenced action intents, short-lived exact-window leases, a single reusable probe-slot abstraction with write-ahead OPEN/ROTATE/CLOSE operations, versioned page-continuity capabilities, deterministic advisory orchestration, a schema-v6 persisted generation-fenced replaceable-worker protocol, and an independent resident-watchdog host. In addition to the explicit one-shot executor, 0.9 can explicitly opt the resident watchdog into same-worker recovery for recognized ChatGPT Web delivery errors. General unattended dispatch, automatic new-chat takeover, and automatic live-page closing remain disabled.

## Why

Long ChatGPT Web turns can fail independently of local side effects. A tool may have completed while the page still shows an `ing` card; the composer can become usable while delivery is broken; a `Message delivery timed out` error can appear after local mutations already happened. Blindly replaying the prior turn can therefore duplicate commits, writes, uploads, or other side effects.

CWS separates three truth domains:

1. **ChatGPT UI/DOM** — what the page currently renders.
2. **ChatGPT transport activity** — optional V1 CDP/network lifecycle evidence when CWS safely owns or is explicitly allowed to observe a DevTools endpoint.
3. **Local Shell MCP durable execution state** — logical sessions, Goal plans, in-flight tool leases, tracked jobs, and workspace checkpoints.

Completion and recovery decisions are never based on the Send/Stop button alone.

## Current 0.9 control plane

The 0.9 control plane remains fail-closed and conservative:

- SQLite task/worker registry;
- durable task identity independent of conversation URL;
- direct **read-only** ingestion of Local Shell MCP file-state sessions/plans/jobs;
- read-only workspace reconciliation (cwd, Git HEAD, dirty/status digest);
- DOM observation and LSM browser-snapshot normalization boundary;
- exact-URL Windows UI Automation observation of existing authenticated Chrome windows, with ambiguity rejected unless an exact HWND is bound;
- optional CDP Network-domain lifecycle telemetry that stores no headers, cookies, POST data, or response bodies;
- three-signal stall classification across browser/DOM, optional network lifecycle, and durable LSM state;
- durable sanitized reconciliation records with deterministic evidence `fence_token`s;
- system/aggregate Chrome RAM telemetry and conservative active/park/probe planning;
- mandatory two-sample semantic-fence dispatch planning;
- durable write-ahead action attempts (`ARMED`, `SUBMITTED`, `RECONCILE_REQUIRED`, `ACKNOWLEDGED`, `FAILED`, `CANCELLED`);
- SQLite schema v6: all schema-v5 action/window/capability/probe invariants plus durable worker-protocol task state, per-worker generation/lease state, and append-only protocol events;
- an audited `dispatch-plan` command that remains dry-run by default;
- an explicit one-shot `dispatch-execute` path that requires a candidate-ready two-sample fence, exact task confirmation, a fresh exact-window lease, no unresolved action, available recovery budget, and per-invocation UIA opt-in;
- `action-reconcile-uia` can release the action lock only from a completed exact single-turn nonce/hash acknowledgement;
- a reusable probe-slot planner that reuses the same owned slot for the same target, rotates only by exact-close-before-open for another target, and blocks on stale or ambiguous ownership rather than opening another window;
- crash-fenced probe mutation reconciliation that persists authority before close/open, adopts only one exact proven CWS-owned replacement after a crash, and blocks on multiple/changed/unknown window evidence;
- durable page-continuity capabilities bound to evaluator version, site, browser family/major, platform, observation surface, experiment time, and expiry; capability use remains explicit;
- deterministic multi-task orchestration policy for observe/reconcile/recommend/wait/human decisions with fairness, cooldown, recovery-budget, LSM/workspace, exact-window, and capability gates; it never grants mutation authority;
- a revision/generation-fenced worker-lease protocol for registration, heartbeat, handoff, takeover, supersession, abandonment, completion, and parent/child task lineage, persisted atomically with revision compare-and-swap; automatic browser conversation creation remains disabled;
- deterministic health classifier;
- low-noise resident attention watchdog with a SQLite singleton lease/heartbeat;
- opt-in `--auto-recover-timeouts` mode for recognized delivery errors only: it refreshes the exact current window, reconciles LSM/Git, requires two stable semantic samples, honors the action lock/recovery budget/cooldown, sends at most one fenced current-worker continuation per cycle, and waits for positive nonce/hash ACK before another send;
- independent detached watchdog start/status plus cooperative lease-based stop, without relying on LSM process-tree kill semantics;
- strict, separate evidence gates for generation/page continuity and live-LSM-tool continuity across close/reopen; both passed isolated authenticated experiments, including one tracked LSM job that was `running` at close and later `succeeded`;
- a gated exact-window UIA sender plus hash/nonce-only acknowledgement observer with real isolated `ARMED → SUBMITTED → ACKNOWLEDGED` acceptance evidence;
- conservative recovery recommendation and idempotent recovery prompt;
- **no general unattended ChatGPT retry/takeover loop, no automatic new-conversation creation, and no automatic live-worker page close yet**.

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
python -m cws capability-import --file .cws\page-close-evidence.json --json
python -m cws capability-status --json
python -m cws pool-plan --page-close-capability latest
python -m cws action-status example
python -m cws evaluate-page-close --file .cws\page-close-evidence.json --json
python -m cws dispatch-plan example --uia
# Explicit one-shot recovery only after a candidate-ready dry run:
python -m cws dispatch-execute example --confirm-task example --enable-experimental-uia
python -m cws watchdog-status
# Optional resident recovery for recognized delivery-timeout errors on registered tasks:
python -m cws watchdog-start --auto-recover-timeouts
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

`cws watch` is the foreground resident loop. For independent hosting, use `cws watchdog-start`, `cws watchdog-status`, and `cws watchdog-stop`. The detached host still runs the same watcher and SQLite singleton lease. `watchdog-start --auto-recover-timeouts` is an explicit opt-in that also enables exact-window UIA and only handles recognized delivery errors for already registered tasks; it does not create replacement conversations or close live pages. Stop is cooperative: it steals the lease into a short-lived `stop:` fence so the old watcher exits on its next failed heartbeat while a replacement remains blocked. No process-tree kill is required.

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
memory, while `cws pool-plan` produces `DO_NOT_CLOSE` / `PARK_CANDIDATE` advice. The default
remains fail-closed: `close_allowed` is not enabled by the release version alone. The
preferred path is to import validated local evidence into a versioned durable capability and
then explicitly select that capability with `--page-close-capability`; the one-shot
`--page-close-evidence FILE` path remains for compatibility. The planner itself never closes
pages, and live LSM work remains `DO_NOT_CLOSE`.

V3's private-transport NO-GO decision and deterministic dry-run dispatcher are documented
in [`docs/V3_DECISION.md`](docs/V3_DECISION.md) and [`docs/V3_SPEC.md`](docs/V3_SPEC.md).
The 0.6 reusable probe-slot, durable capability, and explicit recovery-execution contracts
are documented in [`docs/V4_SPEC.md`](docs/V4_SPEC.md). The 0.7 crash-fenced probe mutation
and advisory worker-protocol boundary is documented in [`docs/V5_SPEC.md`](docs/V5_SPEC.md).
The 0.8 schema-v6 durable worker protocol and adversarial orchestration closure are documented
in [`docs/V6_SPEC.md`](docs/V6_SPEC.md). The 0.9 same-worker timeout recovery loop is documented
in [`docs/AUTOPILOT.md`](docs/AUTOPILOT.md).

For day-to-day use, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md). `cws doctor` is a
read-only preflight for registry/LSM schema, RAM, watchdog lease/PID state, unresolved action fences, workspace/task state, and optional exact-URL UIA. It never repairs or changes browser/task state. The write-ahead action protocol is documented in [`docs/ACTIONS.md`](docs/ACTIONS.md), and isolated browser experiments in [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md).

## Safety boundary

CWS never treats `continue` as idempotent. Before recovery execution, it requires reconciliation against durable LSM state, browser evidence, and the actual workspace. CWS does not migrate browser sign-in state, bypass access controls, or reconstruct private ChatGPT endpoints. The default watchdog remains advisory; only the explicit `--auto-recover-timeouts` mode may call the same fenced current-worker executor, and only for recognized delivery errors. Page eviction is likewise advisory only for live work.

Browser telemetry is also data-minimized: UIA does not read the unsent prompt draft;
`probe-uia` returns state/signature diagnostics rather than conversation text; persisted
UIA/LSM snapshot raw metadata is reduced to the numeric/state fields needed by the watchdog.

## Roadmap

- **V0:** DOM + LSM telemetry + registry + watchdog + recovery recommendations.
- **V1:** read-only UIA/CDP observability + three-signal classification + durable reconciliation fences. Implemented; recovery dispatch remains disabled.
- **V2:** low-RAM worker planner, active/probe page-pool primitives, parked-worker bookkeeping, and RAM telemetry. Implemented conservatively; close/reopen evidence exists, but live page eviction remains disabled.
- **V3:** evidence says no private Web transport is currently needed; semantic two-phase fences + disabled dry-run recovery/takeover planning are implemented.
- **0.4:** durable write-ahead action/crash fencing, strict page-close evidence evaluation, schema-v2 registry, and independent detached watchdog hosting.
- **0.5:** authenticated same-profile disposable-window experiments proved generation continuity, one tracked LSM-job continuity across page close/reopen, and the real write-ahead crash window. Exact top-level window identity and the gated UIA sender/ACK module were established.
- **0.6:** schema-v4 durable capability provenance, at-most-one reusable probe-slot state, nonce-bound recovery prompts, atomic recovery-budget arming, explicit fenced `dispatch-execute`, and positive `action-reconcile-uia` acknowledgement. Default `dispatch-plan` stays dry-run; watchdog auto-dispatch and live-worker auto-close remain disabled.
- **0.7:** schema-v5 write-ahead probe `OPEN`/`ROTATE`/`CLOSE` operations and deterministic crash reconciliation, advisory fair recovery orchestration, a pure generation-fenced replaceable-worker protocol, transactional page-pool updates, and duplicate-free attention scheduling. Browser mutation remains explicit; watchdog auto-dispatch and live-worker auto-close remain disabled.
- **0.8:** schema-v6 durable worker protocol persistence with revision-CAS writes and append-only events, restart-safe generation authority, an advisory runtime evidence adapter, operational probe reconciliation, and adversarial closure for duplicate scheduling, global probe fencing, future timestamps, and wall-clock rollback. Same-worker recovery remains explicit in this release; automatic new-chat takeover and live-page auto-close remain disabled.
- **0.9:** opt-in resident same-worker timeout autopilot for recognized ChatGPT Web delivery errors. It keeps the existing two-sample LSM/workspace/exact-window/action fences, adds ACK-state replay suppression and a recovery cooldown, and performs at most one possible external send per watchdog cycle. Automatic new-chat takeover and live-page auto-close remain disabled.
