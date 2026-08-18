# Network observability

CWS treats network activity as one independent liveness signal. It is not an execution transport and it is not a private ChatGPT API client.

## Two browser ownership modes

### User-owned normal Chrome

The current user Chrome is not restarted or modified to expose DevTools. On the bootstrap machine it did not publish a CDP TCP endpoint. CWS observes that browser through the read-only Windows UI Automation backend instead.

### CWS-owned Playwright browser

When CWS owns the Playwright browser/context/page, it can create an in-process CDP session with `context.new_cdp_session(page)`. This works with Playwright's normal pipe transport and does not require a `--remote-debugging-port` listener.

`sample_cdp_session()` records only the Network-domain metadata needed for liveness analysis:

- request/response event counts;
- `dataReceived` count and encoded byte count;
- `loadingFinished` / `loadingFailed` counts;
- WebSocket frame activity count;
- last observed network activity time;
- requests still in flight at the end of the bounded sample;
- bounded origin, resource-type, response-status, and failure summaries.

It deliberately does **not** retain request/response headers, cookies, authorization values, POST bodies, response bodies, or private backend payloads.

## External CDP endpoint safety

`CdpNetworkProbe` is an optional compatibility path for a browser that was already explicitly exposed through CDP.

- Loopback (`localhost`, `127.0.0.0/8`, `::1`) is accepted by default.
- A remote CDP host requires an explicit `allow_remote=True` in the library API.
- Endpoint query strings and credentials are never persisted in observation metadata; only the validated host and remote/local classification are retained.
- The probe matches an already-open worker page by exact registered URL, samples it, then detaches. It does not navigate, click, type, evaluate page JavaScript, or close the remote browser.

The CLI accepts `--allow-remote`, but remote CDP remains an explicit opt-in; loopback is the safe default. Endpoint query strings/credentials are not persisted.

## Bootstrap experiments

### External TCP port is not assumed for the user-owned browser

The user's normal authenticated Chrome did not expose a debugging endpoint, and CWS did
not restart or modify it to create one. A fresh isolated ChatGPT Chromium was unauthenticated
and Cloudflare-gated and was closed without bypass attempts or credential transfer.
Therefore CWS does not make external TCP CDP exposure a prerequisite for V1/V2.

### Owned Playwright/CDP sample succeeded

A temporary CWS-owned headless Chromium loaded a localhost-only fixture that issued a fetch roughly every 120 ms. CWS sampled it through a loopback CDP endpoint for 1.2 seconds.

Observed in that run:

- 39 total Network events;
- 10 requests;
- 10 responses;
- 10 `dataReceived` events;
- 40 encoded data bytes;
- 9 `loadingFinished` events;
- 0 `loadingFailed` events;
- localhost was the only recorded origin.

The temporary browser/server were owned by the experiment and cleaned up afterwards.

## Interpretation rule

A bounded sample with positive recent network activity is **conflict evidence**, not proof that the model is progressing: ChatGPT may have unrelated background traffic. If DOM/LSM look stale but network activity is recent, CWS enters `RECONCILING` rather than racing the worker.

A bounded sample with zero events is **not** proof of a stall. CWS carries a conservative
`quiet_since_at` lower bound across observations. DOM + network + durable LSM silence
together can raise stall confidence, while network activity alone never upgrades a task
to `RUNNING`.

Reliable network-silence classification requires a resident/continuous heartbeat owned by CWS, with explicit observation coverage timestamps. That is a V2 browser-pool responsibility rather than something inferred from one short sample.
