# V1 specification: browser observability and recovery fencing

V1 adds stronger worker liveness evidence and a durable reconciliation fence while keeping recovery dispatch advisory-only.

## Implemented scope

### Existing authenticated Chrome: Windows UI Automation

On Windows, CWS can inspect the user-authorized, currently selected Chrome tab through the OS accessibility/UI Automation tree.

Properties:

- exact registered conversation URL match is required;
- reads accessibility text/button/address metadata only;
- no click, typing, navigation, DevTools attach, browser-profile copy, cookie/token/password/session-secret access;
- captures a bounded text tail only transiently to derive `message_signature`; conversation text is not persisted in `BrowserObservation`;
- records positive Stop/Send evidence conservatively: Stop => generating; visible enabled Send => idle; neither => unknown;
- records process working set as a V2 RAM-planning input.

This path successfully observed the explicitly authorized bootstrap conversation in the existing authenticated normal Chrome. A fresh isolated Chromium was unauthenticated and Cloudflare-gated; CWS closed it and did not attempt to bypass the gate or transfer login state.

Limitation: UI Automation is best suited to the selected exact-URL tab. It is not a scalable background-tab DOM transport, so parked-conversation probing remains a V2 browser-ownership problem.

### Optional network lifecycle observation

For a CWS-owned Playwright page, or a browser already explicitly exposing CDP, CWS can collect bounded Network-domain lifecycle metadata.

Stored evidence is intentionally narrow:

- request/response/data event counts;
- encoded byte counts;
- loading-finished/loading-failed counts;
- WebSocket frame count;
- last network activity time and conservative `quiet_since_at` lower bound;
- bounded origin host/resource type/status/failure summaries.

CWS does not persist request or response headers, cookies, authorization values, POST data, response bodies, or private ChatGPT backend payloads. External CDP is loopback-only by default; non-loopback requires an explicit `--allow-remote` opt-in.

Network activity alone never proves that a task is progressing. It is used as conflict evidence and to strengthen a multi-signal silence conclusion.

### Three-signal classifier

The watchdog combines:

1. durable LSM/session/plan/job evidence;
2. browser/UI activity and contradictory lifecycle state;
3. optional network lifecycle activity/silence.

Examples:

- live LSM in-flight tool/job => `RUNNING` regardless of apparently idle UI;
- Goal lease due + recent network activity => `RECONCILING`, not immediate recovery;
- DOM and LSM silent + recent network activity => `RECONCILING` because evidence conflicts;
- DOM + network + LSM all silent beyond configured thresholds => `SUSPECT` with stronger confidence;
- network activity by itself never upgrades an ambiguous task to `RUNNING`.

## Durable reconciliation record

`cws reconcile TASK` refreshes evidence and stores a sanitized `ReconciliationRecord` with a deterministic SHA-256 `fence_token`.

The fence snapshot contains only recovery-relevant state metadata and digests:

- task/current worker/LSM session/recovery budget/checkpoint digest;
- browser state, URL, timestamps, and message signature;
- network timing/count summary;
- LSM run/plan/continuation/in-flight/job summary;
- cwd/Git HEAD/dirty/status digest.

It excludes conversation text, browser raw payloads, request/response headers or bodies, cookie/token material, full LSM activity history, and workspace changed-path lists.

Two reconciliations of the same actionable world state produce the same `fence_token`; a worker signature, Git HEAD/status, LSM run, continuation state, or other fenced fact changing invalidates the old fence.

Commands:

```text
cws probe-uia TASK
cws probe-cdp TASK --endpoint http://127.0.0.1:9222
cws inspect TASK --uia
cws reconcile TASK --uia
cws reconciliation-history TASK
cws recommend TASK --uia
```

Every `recommend` now creates a reconciliation record first and stores its `reconcile_id`/`fence_token` in the recovery audit event.

## Recovery remains disabled

V1 still does not click Continue, submit text, retry a failed turn, or invoke LSM takeover automatically. A fence is a prerequisite for a future action, not permission to act.

The next action-capable layer must re-read the world immediately before dispatch and prove that the supplied fence still matches. It must also prove no LSM tool/job/continuation race exists and the recovery attempt budget is available.
