# Local Shell MCP integration findings

This note records the empirical Local Shell MCP facts used to bootstrap CWS. It is intentionally separate from the architectural contract because these details can change when LSM is upgraded.

## Inspected deployment

- Local Shell MCP: **v4.0.1**
- Python: 3.11.15
- controller mode: `stdio`
- state backend: `file`
- workspace root: `D:\Documents`
- hardened deployment state root observed on this machine: `C:\ProgramData\LocalShellMCP-Hardened\control\state`
- durable session schema version: `1`
- durable jobs-store schema version: `2`

The path above is an observed deployment detail, not a portable protocol. CWS supports an explicit state-directory override and environment-based discovery.

## Durable logical sessions

`session_runtime.py` persists logical sessions as `sessions/<session_id>.json`. The transport-local `session_key` is deliberately not persisted; durable identity is the logical `session_id` plus agent `run_id`.

A resume with `takeover=true` creates a new agent run and supersedes the previous active run. LSM refuses takeover while tool calls are still in flight. CWS therefore must not invent a parallel takeover mechanism or edit session JSON directly.

## Tool-call fencing

Before an external tool executes, LSM persists a per-run in-flight lease containing `run_id`, `started_at`, and `heartbeat_at`. If the lease cannot be durably persisted, execution fails closed. Completion removes the lease; long calls renew its heartbeat. The inspected stale in-flight lease threshold is two hours.

This is strong reconciliation evidence: an apparently idle ChatGPT UI cannot override a live durable tool lease.

## Goal continuation

The inspected Goal implementation provides:

- execution/inactivity lease: 900 seconds;
- continuation-pending TTL: 300 seconds;
- maximum continuation attempts: 10;
- claim/reserve/validate/report semantics;
- no continuation claim while a tool call is in flight.

CWS should observe and coordinate with this mechanism, not duplicate it. Its missing layer is ChatGPT Web worker/message lifecycle health and cross-worker task continuity.

## Jobs

The file backend persists `jobs.json` plus per-attempt command, log, and status files. LSM itself reconciles interrupted start/retry operations against both terminal runner status and its live persistent-shell registry.

CWS V0 uses only facts safe to infer out of process:

- `jobs.json` state;
- a terminal per-attempt status JSON is treated as monotonic evidence that the attempt finished, even if `jobs.json` still says `running`;
- absence of a terminal status file is **not** treated as proof that a job died, because CWS does not own LSM's in-memory shell registry.

Persistent shell sessions use the ConPTY backend in this deployment and are **not durable across controller restart**. Logical sessions/plans/jobs are the durable layer.

### Resident-process stop experiment

During the CWS bootstrap, a resident `python -m cws ... watch` process was launched twice
as an LSM tracked shell job. `job_stop` returned `killed=true` and persisted each job as
`stopped`, but process inspection afterwards still showed both jobs' PowerShell wrappers
and Python descendants alive. The smoke processes were then terminated by their unique
test command line and verified absent.

Source inspection shows `job_stop` delegates shell jobs to `kill_shell`, and the ConPTY
backend closes/terminates the PTY process. In this specific Windows deployment that did
not guarantee descendant process-tree termination. CWS therefore treats LSM job status
as execution evidence, not as proof that arbitrary descendants are gone, and should not
be production-hosted as an ordinary LSM tracked shell job unless this behavior is fixed
or independently fenced.

## Browser sessions

The high-level Playwright browser manager in v4.0.1:

- caps active sessions at 8;
- idle-cleans after 3600 seconds;
- keeps its browser-session registry in process memory;
- can persist browser profiles and storage state;
- records bounded page/console/request failures and recent response metadata.

It does not yet expose enough stream lifecycle timing to distinguish every ChatGPT generation/delivery stall. V1 should therefore add timestamped network/CDP observability, while continuing to use DOM + durable LSM evidence as independent signals.

## Compatibility rule

The direct-file adapter is an implementation bridge for the inspected file backend, not a claim that LSM's JSON files are a stable public API. CWS gates known schema versions and fails closed on unknown versions. If a future LSM release changes these formats, update and test the adapter before allowing classification/recovery decisions.
