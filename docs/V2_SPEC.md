# V2 specification: low-memory worker and browser orchestration

V2 turns the 8 GB RAM constraint into an explicit scheduler input. The implemented layer is deliberately conservative: it can inventory pressure, rank workers, track page leases, and rotate probe targets, but it does not close a live ChatGPT page automatically.

## Memory telemetry

`cws ram-status` reads:

- physical total/available/used memory from the local OS;
- aggregate Chrome process count and working-set totals on Windows.

The Windows browser process probe records only PID, working set, and whether a process owns a main window. It does not inspect process command lines, browser profile paths, page URLs, environment variables, cookies, or credentials.

A bootstrap sample on the development machine observed roughly:

- 7.6 GiB physical RAM;
- 3.0 GiB available RAM;
- 21 Chrome processes;
- about 1.0 GiB aggregate Chrome working set.

These values are runtime observations, not hard-coded thresholds.

## Pool planner

`cws pool-plan` combines task/worker state, durable LSM evidence, latest browser observation, and memory pressure.

Default planning policy:

- active-worker target: 4;
- probe-page target: 1;
- low-memory warning: <= 1 GiB available or >= 85% physical-memory use;
- page-close experiment: **not passed**, therefore actual page closing remains disabled.

Disposition rules are fail-closed:

### `DO_NOT_CLOSE`

A worker is pinned if any strong/ambiguous liveness condition exists, including:

- LSM in-flight tool call;
- tracked active job;
- continuation pending;
- browser reports active generation;
- task state `STARTING`, `RUNNING`, `RECOVERING`, `RECONCILING`, `SUSPECT`, or `NEEDS_HUMAN`.

Memory pressure and active-worker count do not override those fences. If pinned workers alone exceed the configured target, CWS reports the pressure rather than choosing a live task to sacrifice.

### `PARK_CANDIDATE`

Without live LSM/browser evidence, terminal/idle states can be ranked as parking candidates:

1. `COMPLETED`;
2. `ABANDONED`;
3. `QUEUED`;
4. `BLOCKED`.

A parking candidate is **not** permission to close a page. `close_allowed=false` remains hard-coded in the CLI until a dedicated authenticated ChatGPT page-close/reopen experiment proves that behavior safe.

### `NO_PAGE`

Workers already marked `parked`, `superseded`, or `dead` do not count as resident page leases.

`cws worker-status WORKER parked` exists only as explicit bookkeeping for a page that was independently closed/detached. The command itself never opens or closes a browser page.

## Ephemeral page pool

`PagePool` tracks browser-runtime page leases independently of durable task identity.

Roles:

- `ACTIVE`: a page currently assigned to an executing/interactive worker;
- `PROBE`: one of the small reusable observation pages used to visit parked conversation URLs sequentially.

The pool:

- enforces active/probe capacity;
- fails closed instead of evicting a page implicitly;
- forgets only pages the browser adapter reports as already closed/detached;
- maintains a priority/oldest-first probe-target queue;
- keeps page IDs ephemeral and never treats them as durable task identity.

A probe resource policy can block visual-only `image`, `media`, and `font` resources in a dedicated CWS-owned probe context. Documents, scripts, stylesheets, XHR/fetch, and WebSocket traffic remain allowed because state detection may depend on them.

## Page-close/reopen experiment status

The actual question is:

> If a ChatGPT Web task has already been submitted, can its page be closed while server-side generation/tool execution continues, and can reopening the conversation URL reconstruct a trustworthy state?

This is **not yet proven** on ChatGPT.

During V2 bootstrap no isolated normally authenticated CWS browser profile existed. The user's real authorized conversation was actively generating and was therefore not used as an experiment target. CWS did not copy its login state or type/click in that page to manufacture a test worker.

A localhost-only methodology harness did pass:

1. a CWS-owned test page started a harmless server-side background job;
2. the page was closed;
3. the local server completed the job independently;
4. a new page reopened the state endpoint and observed completion.

This proves the test harness/page-pool mechanics, **not ChatGPT semantics**. The ChatGPT-specific experiment remains blocked until an isolated, user-authenticated, disposable conversation is available through ordinary login.

## V2 safety consequence

V2 provides the machinery needed to reduce RAM without pretending the final close-page invariant is known. The planner can tell the future browser adapter *what should be considered first*, while the action boundary remains disabled.
