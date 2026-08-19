# CWS 0.7 parallel-wave integration order

Baseline: `de740fc`.

Child branches:

1. `agent/07-probe-ops` — schema v5 + crash-fenced probe mutation operations.
2. `agent/07-watchdog-orchestration` — pure scheduling/orchestration, no automatic mutation.
3. `agent/07-adversarial-tests` — regression attacks, primarily tests.
4. `agent/07-worker-protocol` — pure replaceable-worker lease protocol.

Integrator branch: `work-260819-cws-07-integrator`.

## Merge policy

Do not merge by completion time alone. First review each branch independently against the bootstrap invariants. Integrate probe-ops first because it owns schema v5. Rebase or adapt the adversarial suite against that schema next. Worker protocol is intentionally persistence-independent and can be integrated with low conflict. Watchdog orchestration should be integrated after the durable operation and worker semantics are known, then its interfaces can be wired without weakening safety defaults.

After every integration step run the full test suite. The final 0.7 release must keep unattended browser mutation disabled unless a separate, explicit acceptance milestone proves the complete crash-fenced path end-to-end.
