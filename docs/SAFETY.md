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

- Windows UI Automation/accessibility inspection of a user-explicitly-authorized Chrome tab, matched by exact conversation URL;
- a dedicated CWS-controlled Playwright/browser profile created through ordinary user authentication;
- bounded DOM/network lifecycle metadata needed to determine liveness and silence.

CWS must not:

- read or copy browser cookie databases, authentication tokens, session secrets, or passwords;
- transfer the user's normal Chrome login state into another browser profile behind the user's back;
- bypass Cloudflare, CAPTCHA, sign-in, or similar access controls;
- attach to unrelated tabs or enumerate conversation content that the user did not register for supervision;
- reimplement ChatGPT private backend endpoints as the default execution transport;
- parse private response bodies when timing/status metadata is sufficient for health detection.

A fresh isolated browser that is unauthenticated or blocked by an access-control page is treated as a boundary, not as a challenge to circumvent.

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

## Process and machine safety

- Modify only the CWS repository unless another path is explicitly authorized.
- Do not upgrade or patch Local Shell MCP as part of CWS experiments.
- Do not terminate unrelated processes. Test-process cleanup must use a unique command line/PID created by the same experiment.
- Production CWS hosting should be independent of a ChatGPT conversation and should use its own singleton watchdog lease.
- Page-close/RAM experiments must use dedicated harmless workers until it is proven that closing a page cannot damage a live task.

## Agent / supervisor prompt preamble

When CWS asks another model or worker to inspect/recover a task, the instruction should state the safety boundary explicitly rather than relying on implication:

> This is a reliability/recovery task for a user-authorized ChatGPT Web + Local Shell MCP workflow. Work read-only first. Do not bypass authentication or platform controls; do not access or move cookies, tokens, passwords, or private API credentials; do not call or reconstruct private ChatGPT endpoints. Do not click/type in unrelated browser tabs. Before any action with side effects, reconcile the durable Local Shell session/plan/jobs and actual workspace/Git state. The previous message may have failed after side effects already completed, so never blindly replay the prior operation. If evidence is ambiguous, stop at a recommendation or request human review rather than guessing.

This preamble is deliberately repetitive: it makes the intended benign scope machine-readable to both execution agents and safety reviewers.
