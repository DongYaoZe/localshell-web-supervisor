# Safety and scope boundary

`chatgpt-web-supervisor` is reliability infrastructure for work the user already performs through ChatGPT Web and Local Shell MCP. Its purpose is to reduce lost task state, duplicate side effects, browser memory pressure, and unnecessary human polling. It is not a mechanism for bypassing authentication, platform safeguards, rate limits, or access controls.

## Default operating mode

CWS is **observe-first and fail-closed**.

- Read existing task state before taking action.
- Prefer Local Shell MCP durable session/plan/job evidence and actual workspace/Git state over weak UI hints.
- Treat a ChatGPT conversation as a replaceable worker lease, never as the durable task identity.
- Do not replay an operation merely because a tool card appears unfinished.
- Do not auto-recover when evidence is ambiguous.
- Keep action-capable recovery disabled by default until its fences are independently verified.

## Browser safety boundary

Permitted read-only observation paths include:

- Windows UI Automation/accessibility inspection of a user-explicitly-authorized normal-Chrome top-level window, matched by exact conversation URL and, when needed, exact HWND;
- a dedicated CWS-controlled Playwright/browser profile created through ordinary user authentication;
- bounded DOM/network lifecycle metadata needed to determine liveness and silence.

For read-only Windows UI Automation, CWS transiently reads accessibility text only to derive
the latest-message signature/error/liveness state. `probe-uia` does not return conversation
text. Successful observation may persist a short-lived exact-window lease containing only
worker id, URL, HWND, PID, Chrome executable path, source, and timestamps. HWND leases expire
quickly because window handles can be recycled.

For LSM high-level browser snapshots, CWS does not persist the recent error/network entry
payloads; it retains only counts and the bounded state needed for classification.

CWS must not:

- read or copy browser cookie databases, authentication tokens, session secrets, or passwords;
- transfer the user's normal Chrome login state into another browser profile behind the user's back;
- bypass Cloudflare, CAPTCHA, sign-in, or similar access controls;
- attach to unrelated tabs or enumerate conversation content that the user did not register for supervision;
- reimplement ChatGPT private backend endpoints as the default execution transport;
- parse private response bodies when timing/status metadata is sufficient for health detection.

A fresh isolated browser that cannot complete ordinary sign-in is treated as a boundary, not as a challenge to circumvent. Browser sign-in-state migration remains outside the production CWS capability boundary; the supported path is the user's already-authorized normal Chrome window with exact UIA identity fences.

## Recovery safety boundary

Before any future `continue`, retry, or worker takeover, CWS must reconcile at least:

1. Local Shell logical session and active run;
2. Goal plan status, continuation claim/pending state, and in-flight tool leases;
3. tracked Local Shell jobs and terminal per-attempt status;
4. actual cwd/repository state, including Git HEAD and dirty/status digest when applicable;
5. current worker identity and latest browser observation;
6. recovery budget/history and the last durable checkpoint.

Hard fences:

- never race a live LSM tool call;
- never race an LSM continuation already pending;
- never infer idempotence from the ChatGPT UI;
- never repeat a completed side effect simply because message delivery failed;
- escalate to `NEEDS_HUMAN` when the first genuinely incomplete step cannot be proven.

### Write-ahead external-action fence

Any external worker mutation must be recorded durably as `ARMED` **before** the transport is
called. `ARMED`, `SUBMITTED`, and `RECONCILE_REQUIRED` all block another attempt for the same
task. Registry schema v2 introduced that partial unique index, so it also holds across racing
supervisor processes. Schema v3 added short-lived exact-window leases; schema v4 added durable
capability provenance and the reusable probe-slot record. Schema v5 adds a separate globally
unique unresolved probe-mutation fence for `OPEN`, `ROTATE`, and `CLOSE`. For recovery execution, ARMED state
and recovery-budget consumption are committed atomically before submission. An expired
window lease blocks the gated UIA transport before draft input and must be refreshed by observation.

A transport exception is treated as an ambiguous side-effect window and becomes
`RECONCILE_REQUIRED`; it is not permission to retry. Only proof that no external side
effect occurred may produce terminal `FAILED`. A positive acknowledgement must be tied to
the same attempt and worker before the duplicate-send lock is released as `ACKNOWLEDGED`.

CWS exposes only an explicit one-shot current-worker executor. It requires exact task
confirmation plus all semantic/LSM/workspace/window/action fences. The resident watchdog
does not call it automatically. `action-reconcile-uia` can acknowledge only from positive
single-turn completion evidence; no CLI fabricates acknowledgement.

Probe-window mutation has the same write-ahead principle but a separate state machine. A
durable operation must authorize the exact close/open phase before the transport is called.
After a crash, only one exact CWS-owned target may be adopted. Old+new present, multiple
matches, changed HWND/PID/executable identity, or incomplete observation stays fenced in
`RECONCILE_REQUIRED`. An uncertain submitted phase is never blindly replayed.

## Process and machine safety

- Modify only the CWS repository unless another path is explicitly authorized.
- Do not upgrade or patch Local Shell MCP as part of CWS experiments.
- Do not terminate unrelated processes. Test-process cleanup must use a unique command line/PID created by the same experiment.
- Production CWS hosting should be independent of a ChatGPT conversation and should use its own singleton watchdog lease.
- Resident watchdog shutdown should use the cooperative lease-stop fence; do not depend on LSM process-tree kill semantics.
- Strong authenticated evidence now proves close/reopen continuity for pure model generation and one harmless Local Shell MCP job in an exact-bound disposable window. This does not prove arbitrary side-effecting tools are idempotent.
- Production pool policy still pins live LSM work, active generation, and ambiguous recovery states as `DO_NOT_CLOSE`; capability evidence does not authorize automatic eviction.
- Page-continuity capabilities are versioned, context-bound, expiring, and only used when explicitly selected.
- The durable probe model permits at most one reusable slot and at most one unresolved mutation operation; stale or ambiguous ownership blocks replacement rather than opening another window.
- The 0.7 orchestration layer is advisory: even a selected `recommend-action` decision has `mutation_allowed=false` and cannot bypass the explicit executor.
- The 0.7 worker-lease protocol uses revision/generation fencing so a superseded or stale conversation cannot regain authority with a late heartbeat; its persistence/browser-creation adapters are intentionally not automated yet.
- Anonymous/localhost experiments cannot satisfy the page-close safety gate.
- Never bulk-close ambiguous unmarked user windows.

`cws doctor` is intentionally diagnostic-only. It may read registry/LSM schema, local
workspace/Git state, RAM/process working-set counters, watchdog lease state, and optionally
the exact registered URL through UIA; it never performs repair, installation, process
control, browser navigation, or recovery dispatch.

## Agent / supervisor prompt preamble

When CWS asks another model or worker to inspect/recover a task, the instruction should state the safety boundary explicitly rather than relying on implication:

> This is a reliability/recovery task for a user-authorized ChatGPT Web + Local Shell MCP workflow. Work read-only first. Do not bypass authentication or platform controls; do not access or move cookies, tokens, passwords, or private API credentials; do not call or reconstruct private ChatGPT endpoints. Do not click/type in unrelated browser tabs. Before any action with side effects, reconcile the durable Local Shell session/plan/jobs and actual workspace/Git state. The previous message may have failed after side effects already completed, so never blindly replay the prior operation. If evidence is ambiguous, stop at a recommendation or request human review rather than guessing.

This preamble is deliberately repetitive: it makes the intended benign scope machine-readable to both execution agents and safety reviewers.
