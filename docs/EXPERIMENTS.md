# Isolated browser experiments

CWS experiments that can alter a ChatGPT page or submit a turn use disposable conversations/windows and exact identity fences. The user's active work conversation is excluded explicitly.

This document records the evidence behind the 0.5 capability boundary. It is not a recipe to bypass authentication.

## Authentication-path result

A dedicated persistent Playwright profile (`cws-disposable-v4`) could be created without copied authentication, but normal login was not usable in that browser on this machine: Google rejected the browser and the phone-number path remained blocked by repeated Cloudflare checks.

The user then explicitly authorized a cookie-copy experiment. CWS performed a one-time local diagnostic rather than adding product functionality:

- the locked Chrome cookie database was copied from a temporary Windows VSS snapshot without stopping the user's Chrome;
- the temporary shadow copy was deleted and verified absent;
- the offline clone was pruned to ChatGPT/OpenAI-domain rows only;
- no cookie value was printed or sent anywhere;
- the relevant source cookies, including auth/session/token-like rows, all used Chrome `v20` App-Bound encryption;
- opening the cloned profile in the same installed Google Chrome discarded the usable auth cookies and remained logged out;
- no plaintext session token extraction/decryption was attempted.

Conclusion: **cookie/profile migration is a NO-GO architecture path** for CWS. Production code still has no cookie-export/import feature.

## Preferred authenticated isolation: disposable normal-Chrome window

A safer path worked: create a new top-level window in the already authenticated normal Google Chrome profile. It naturally uses the user's ordinary session without moving credentials.

Desktop UI Automation proved the disposable window could be distinguished from the user's active conversation by:

- exact conversation/project URL;
- exact HWND;
- Chrome PID and expected Google Chrome executable;
- signed-in profile/account controls;
- absence of login/signup controls.

The experiment also exposed a bug in the old probe: `Get-Process(...).MainWindowHandle` does not enumerate every top-level window of a multi-window Chrome browser process. 0.5 therefore enumerates Desktop-root `Chrome_WidgetWin_1` windows and rejects ambiguous URL matches unless an exact HWND is supplied.

Stable controls observed on the authenticated page included:

- composer: `AutomationId=prompt-textarea`;
- submit: `AutomationId=composer-submit-button`.

A reversible draft-only probe confirmed that accessibility input can trigger application editor state without submitting a turn.

## Generation close/reopen evidence

A disposable conversation was submitted behind the write-ahead action fence and later closed while positive generation/Stop evidence was visible. A fresh process confirmed the experiment window was absent. After a closed interval the same conversation was reopened.

The sanitized evidence showed:

- normally authenticated normal Chrome;
- `existing_profile_disposable_window` isolation;
- exact experiment window binding and explicit exclusion of the user's active conversation;
- no auth material copied;
- close while generation was live;
- independent progress while the page was absent;
- same conversation after reopen;
- completion evidence after reopen;
- changed content signature;
- valid signed-in state;
- no duplicate user turn.

`cws evaluate-page-close` returned:

```text
generation_parking_safe=true
```

This supports page continuity as a capability, not automatic eviction of arbitrary workers.

## Live Local Shell tool close/reopen evidence

A later bounded experiment strengthened the evidence. Before close, its persisted pre-close record contained:

- a specific tracked Local Shell job id/name;
- durable job status `running`;
- positive ChatGPT Stop/generation evidence;
- exact disposable conversation/window identity;
- pre-close text hash.

The close harness recorded that the exact experiment window was closed while that job was still running. Durable LSM state later recorded the same job as `succeeded`. After reopening the same conversation, the evidence recorded tool completion and final-response continuity without a duplicate turn.

The strict evaluator therefore returns both:

```text
generation_parking_safe=true
tool_execution_parking_safe=true
```

when run against that D-round evidence, including `--require-tool`.

CWS 0.6 still keeps live LSM work `DO_NOT_CLOSE` in the normal `pool-plan` policy. The evidence is now representable as versioned, expiring capability provenance, but an automatic live-worker eviction path still does not exist.

## Action acknowledgement and crash-fence evidence

The write-ahead protocol was exercised repeatedly in disposable authenticated conversations.

Required invariants were observed:

1. `ARMED` existed durably before browser mutation.
2. Only one unresolved attempt existed for a task.
3. Positive submit moved the attempt to `SUBMITTED`.
4. Ambiguous browser-side state was reconciled rather than retried.
5. Positive evidence tied to the same worker/attempt produced `ACKNOWLEDGED`.
6. The unresolved lock was released only after terminal proof.

A particularly useful negative case occurred when the UIA adapter entered a draft but did not positively observe Send in time. The attempt became `RECONCILE_REQUIRED`; it was **not replayed**. Accessibility text could see the draft nonce, proving that a raw nonce hit is not enough to claim submission. The disposable draft window was closed and the same conversation reopened; nonce count returned to zero and the conversation hash returned to the pre-action state. Only then was the attempt marked terminal `FAILED`.

The actual 0.5 `ChromeUiaActionTransport` class then passed a fresh acceptance turn:

```text
ARMED → SUBMITTED → ACKNOWLEDGED
```

The bounded acknowledgement contained only URL/HWND/PID, generating/idle state, signed-in state, nonce count, text-element count and SHA-256. It returned no conversation text.

## Evidence evaluator

Evidence JSON is local/ignored and can be evaluated with:

```powershell
$env:PYTHONPATH = "src"
python -m cws evaluate-page-close --file .cws\page-close-evidence.json --json
python -m cws evaluate-page-close --file .cws\page-close-evidence.json --require-tool --json
```

The generation gate fails closed for anonymous/localhost tests, copied authentication, ambiguous isolation, changed conversation identity, missing background progress, duplicate turns, invalid auth or unchanged signatures.

The stronger tool gate additionally requires:

- tool execution was actually observed;
- exact tool/job identity was confirmed;
- the tool was running at close;
- that tool completed after close;
- its final response was observable after reopen.

## Current conclusion

The 0.5 experiments remain the evidence source; 0.6 consumes that evidence conservatively:

- ordinary authenticated page close/reopen continuity: **proven in isolation**;
- one bounded tracked LSM-job close/reopen continuity: **proven in isolation**;
- exact-window UIA action/ack primitives: **proven in isolation**;
- versioned, context-bound page-continuity capability records: **implemented in 0.6**;
- explicit one-shot fenced current-worker recovery execution: **implemented in 0.6**;
- resident-watchdog automatic recovery dispatch: **not enabled**;
- automatic live-LSM page eviction: **not enabled**;
- private ChatGPT endpoint reconstruction: **still unnecessary and out of scope**.
