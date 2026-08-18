# V3 decision: do not build a private ChatGPT Web API client

## Decision

**NO-GO for private ChatGPT Web transport reimplementation as the default CWS architecture.**

V3 does not extract browser authentication material, reproduce ChatGPT private endpoints, or create a Python `requests`/WebSocket client that impersonates the web application.

This is an evidence-based engineering decision, not a permanent claim that browser transports can never fail. The bar for revisiting it is that the supported browser-observation/action route is shown incapable of meeting reliability requirements even after V1/V2 orchestration is mature.

## Evidence accumulated through V0-V2

### Durable execution truth already exists below ChatGPT Web

Local Shell MCP v4.0.1 already persists logical sessions, Goal plans, continuation state, per-tool in-flight leases/heartbeats, tracked jobs, and guarded takeover semantics. CWS can reconcile those facts with actual Git/workspace state without asking ChatGPT's private backend to explain what happened.

### The current authenticated browser can be observed without copying credentials

Windows UI Automation successfully matched the user-authorized current conversation by exact URL and exposed positive generation/composer/error evidence. CWS did not read a cookie database, token, password, or browser profile secret and did not attach DevTools to the normal Chrome instance.

This gives the watchdog a browser-side signal while preserving the user's existing authentication boundary.

### Dedicated browser/CDP telemetry works without parsing private payloads

A CWS-owned isolated Chromium can provide Network-domain lifecycle timing/count metadata. A localhost-only end-to-end smoke captured request/response/data/finished activity successfully. The observer deliberately ignores headers, cookies, POST data, and response bodies.

This is sufficient for liveness/silence correlation. The supervisor does not need to understand or replay ChatGPT's private protocol merely to detect stalled delivery.

### Fresh isolated ChatGPT browser access is an authentication boundary, not a reverse-engineering task

A new isolated Chromium without normal user authentication reached a Cloudflare/sign-in boundary. CWS closed it. It did not copy the normal Chrome login state or attempt to bypass the access-control page.

A future CWS-owned profile should be authenticated through ordinary explicit user interaction if needed.

### Reconciliation is now stronger than transport state alone

V1 stores sanitized deterministic `fence_token`s over worker/browser, LSM, workspace, checkpoint, and optional network facts. A private API client would not remove the need to reconcile local side effects before retrying; it would add another moving protocol/auth surface.

### RAM pressure can be addressed independently

V2 now has system/Chrome memory telemetry, a conservative worker pool planner, active/probe page leases, and parked-worker bookkeeping. The remaining ChatGPT-specific page-close/reopen question is an experiment gate, not evidence that a private endpoint client is required.

## Costs avoided by the NO-GO decision

Private Web transport would introduce several new failure modes:

- coupling to undocumented endpoint and payload changes;
- dependence on copied browser credentials/session secrets;
- new token/cookie lifetime and storage responsibilities;
- risk of acting on a different server-side conversation state than the rendered worker;
- duplication of browser behavior that already exists in a supported user session;
- a larger safety/security surface for little benefit to deterministic watchdog logic.

## What V3 implements instead

V3 adds a **disabled-by-default deterministic dispatch gate**.

`cws dispatch-plan TASK`:

1. reads the previous reconciliation record;
2. refreshes browser/LSM/workspace evidence and persists a new reconciliation;
3. requires two distinct reconciliations with the same semantic `fence_token`;
4. requires both samples to be fresh and separated by a minimum stability interval;
5. requires a fresh recoverable state (`SUSPECT` or `RECONCILING`);
6. requires recovery budget availability;
7. requires the expected active worker and exact registered conversation URL;
8. requires fresh browser evidence;
9. requires an active matching LSM session with no in-flight tool call, active tracked job, or pending continuation;
10. requires a valid workspace;
11. for current-worker continuation, requires positive non-generating + ready-composer evidence and a stable DOM quiet interval;
12. if network telemetry exists, requires a fresh sample and stable network-quiet interval.

Even if every check passes, the shipped CLI sets `transport_enabled=false`, so the result can become:

```text
candidate_ready=true
would_dispatch=false
```

There is no V3 command that clicks Continue, sends text, retries a message, copies authentication, or invokes LSM takeover automatically.

## Takeover boundary

If the current browser worker cannot be positively observed, the dispatcher can classify the conceptual action as `TAKEOVER_NEW_WORKER`, but it remains blocked. UI disappearance alone is not proof that an old worker is safe to replace.

An automated takeover will require a separate explicit replacement-worker binding protocol that can prove:

- the new conversation URL/worker identity belongs to the same durable task;
- the old LSM run has no live tool lease;
- the takeover is performed through LSM's supported `session_manage resume(... takeover=true)` boundary;
- the replacement browser worker is authenticated through an approved normal browser path;
- the latest semantic fence is revalidated immediately before the takeover transition.

## Revisit criteria

Private transport research should be reconsidered only if all of the following become true:

1. dedicated authenticated browser orchestration is implemented and measured;
2. UIA/DOM + CDP + LSM evidence still cannot reliably distinguish a material class of stalls;
3. the missing information cannot be obtained from supported browser instrumentation;
4. the benefit is large enough to justify a new authentication/protocol security surface;
5. the user explicitly approves that narrower research direction.

Until then, V3 ends at browser orchestration + deterministic fenced action planning.
