# CWS parallel-agent bootstrap

This repository is developed through ChatGPT Web + Local Shell MCP. The stable baseline for the 0.7 parallel wave is commit `de740fc` (CWS 0.6.0, registry schema v4, 157 tests).

## Non-negotiable invariants

- Work only on `chatgpt-web-supervisor`; do not inspect or modify any unrelated project.
- Use Local Shell MCP Goal mode. Start a fresh durable logical session for the assigned task and finish it cleanly.
- Work only in the exact assigned worktree and branch. Do not edit the integrator worktree.
- Do not modify, upgrade, reinstall, or reconfigure Local Shell MCP.
- Do not use the OpenAI API, Agents SDK, private ChatGPT endpoints, copied browser auth, cookies, tokens, passwords, or session secrets.
- Read-only inspection first. Do not open, close, rotate, or mutate a real ChatGPT browser window unless the task brief explicitly authorizes a dedicated acceptance experiment.
- No task in this 0.7 wave authorizes a real browser mutation experiment. Pure state-machine and deterministic tests come first.
- Never replay an ambiguous external action. Durable state must be reconciled before retry.
- Preserve the principle: **Conversation can die, Task cannot die.** A conversation is a replaceable worker lease, not durable task identity.
- Preserve the separation between ChatGPT UI state, ChatGPT transport/network state, Local Shell MCP durable execution state, and actual workspace/Git state.
- Keep memory use conservative for an 8 GB machine. Do not create resident browser sessions, watchdogs, services, or permanent background processes unless the task explicitly requires it.
- Do not start the resident watchdog during this wave.

## Required start sequence

1. Confirm cwd, branch, and `git rev-parse HEAD`.
2. Confirm the branch began from `de740fc` and inspect `git status --short`.
3. Read this bootstrap and the assigned task brief completely.
4. Run the baseline test suite with `PYTHONPATH=src` using an available interpreter with pytest. In this environment `D:\Program Files\anaconda\python.exe` is known to work.
5. Create a Goal plan before substantive edits.

## Engineering rules

- Prefer small pure functions and deterministic state transitions.
- SQLite state transitions that fence external mutation must be atomic and use write-ahead durable records before side effects.
- Crash points are part of the API. Model process death before and after each durable write or external mutation.
- Ambiguous real-world evidence resolves to BLOCKED / RECONCILE / NEEDS_HUMAN, never optimistic retry.
- Keep stored browser evidence sanitized and bounded. Do not persist prompt/response bodies or credentials.
- Do not weaken 0.6 gates merely to make a test pass.
- Backward migration must be additive from schema v4 when a task owns a schema migration.
- Avoid broad refactors unrelated to the assigned task.

## Required finish sequence

1. Run the focused tests and then the full suite.
2. Run `git diff --check`.
3. Run Local Shell MCP `secret_scan` for the worktree.
4. Review `git diff` for scope creep and stale current-version claims.
5. Commit only the assigned task with a clear commit message.
6. Report the commit hash, tests, design decisions, remaining blockers, and integration notes through the durable logical session, then finish the session.
7. Do not merge/cherry-pick into the integrator branch yourself.
