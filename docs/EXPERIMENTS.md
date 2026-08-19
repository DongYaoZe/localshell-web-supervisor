# Isolated browser experiments

CWS does not use the user's active ChatGPT conversation as an experimental surface. Experiments that can change page or message state must use a dedicated disposable browser profile/conversation.

## Current prerequisite state

A dedicated persistent profile named `cws-disposable-v4` can be opened safely by Local Shell MCP without copying cookies, storage state, passwords, or tokens from the normal Chrome profile.

At the time this document was written, that profile was **not authenticated**. The anonymous ChatGPT page could be observed and exercised with normal Playwright/UI actions, but it was not accepted as evidence for authenticated page-close/reopen semantics.

The missing prerequisite for the authenticated lifecycle experiment is deliberately small and explicit:

1. open the dedicated `cws-disposable-v4` browser window;
2. the user signs in through the normal ChatGPT login UI in that window;
3. do not export/copy storage state or cookies to another browser;
4. create a disposable conversation specifically for the experiment;
5. only then run the page-close/reopen experiment.

The current user's normal Chrome tabs are not part of this procedure.

## Anonymous action-adapter probe

An anonymous isolated page was used only to test selector/interaction assumptions. No message was successfully submitted.

Observed lessons:

- a snapshot may expose an invisible fallback `textarea` even when the visible editor is a `div[role=textbox]`;
- snapshot element refs can become stale after a React/modal rerender;
- `fill()` can report success without producing the application-level editor events required to expose a Send control;
- `Enter` cannot be assumed to submit;
- a hard-coded `button[data-testid=send-button]` selector was not positively present on this anonymous page.

Therefore CWS core must not hard-code one ChatGPT selector or treat a successful DOM mutation/click call as action acknowledgement. A transport provider must supply positive pre-action selector proof and positive post-action acknowledgement.

## Authenticated page-close/reopen experiment

The experiment should use one harmless disposable conversation and one nonce-bearing task whose completion is unambiguous. It must record evidence at three phases:

### Phase A: before close

Record:

- exact conversation URL;
- proof this is the dedicated disposable profile;
- proof the profile is normally authenticated;
- a browser/message signature;
- positive generation/execution evidence;
- the fact that no auth material was copied.

Close the page only after generation is positively active.

### Phase B: page absent

Wait long enough that completion would normally occur. Obtain independent evidence that progress occurred while the page was absent. The exact source can vary by experiment, but it must not be inferred only from elapsed time.

### Phase C: reopen

Reopen the exact conversation URL in the same dedicated profile and record:

- authentication is still valid;
- same conversation URL/identity;
- completion evidence is present;
- message signature advanced;
- no duplicate user turn appeared.

Save the evidence as JSON and run:

```powershell
$env:PYTHONPATH = "src"
python -m cws evaluate-page-close --file .cws\page-close-evidence.json --json
```

Only `parking_safe=true` is sufficient evidence to consider enabling live-page parking. Anonymous, localhost, copied-auth, missing-background-progress, duplicate-turn, or same-signature evidence fails closed.

## Example evidence shape

```json
{
  "experiment_id": "chatgpt-close-reopen-001",
  "disposable_profile": true,
  "normally_authenticated": true,
  "auth_material_copied": false,
  "pre_close_url": "https://chatgpt.com/c/...",
  "reopened_url": "https://chatgpt.com/c/...",
  "pre_close_generating": true,
  "close_while_live_confirmed": true,
  "background_progress_observed": true,
  "completion_evidence_after_reopen": true,
  "same_conversation_after_reopen": true,
  "duplicate_turn_observed": false,
  "auth_still_valid_after_reopen": true,
  "pre_close_signature": "...",
  "post_reopen_signature": "...",
  "notes": []
}
```

## Action acknowledgement experiment

Before any production transport is enabled, the adapter must demonstrate all of the following in the disposable conversation:

1. the action is written durably as `ARMED` before any external side effect;
2. there is at most one unresolved action per durable task;
3. a successful submit changes the attempt to `SUBMITTED`;
4. a transport exception or crash window changes/recovers as `RECONCILE_REQUIRED`, never blind retry;
5. the observer produces a positive acknowledgement tied to the same worker/attempt;
6. acknowledgement changes the attempt to `ACKNOWLEDGED` and releases the duplicate-send lock;
7. killing/restarting the supervisor between submit and acknowledgement does not cause a second turn.

Until that experiment passes on a normally authenticated disposable profile, CWS ships no production ChatGPT action transport.
