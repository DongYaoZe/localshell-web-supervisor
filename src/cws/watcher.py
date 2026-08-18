from __future__ import annotations

import time
from dataclasses import dataclass

from .models import (
    Assessment,
    BrowserObservation,
    LsmObservation,
    SupervisorState,
    TaskRecord,
    WorkspaceObservation,
)


@dataclass(slots=True)
class WatchPolicy:
    browser_suspect_after_s: float = 120.0
    lsm_suspect_after_s: float = 180.0
    hard_stall_after_s: float = 600.0


def assess(
    task: TaskRecord,
    browser: BrowserObservation | None,
    lsm: LsmObservation | None,
    *,
    workspace: WorkspaceObservation | None = None,
    now: float | None = None,
    policy: WatchPolicy | None = None,
) -> Assessment:
    now = time.time() if now is None else now
    policy = policy or WatchPolicy()
    evidence: list[str] = []

    if workspace:
        evidence.append(f"workspace.cwd_exists={str(workspace.cwd_exists).lower()}")
        if workspace.is_git_repo is not None:
            evidence.append(f"workspace.git_repo={str(workspace.is_git_repo).lower()}")
        if workspace.git_head:
            evidence.append(f"workspace.git_head={workspace.git_head[:12]}")
        if workspace.git_dirty is not None:
            evidence.append(f"workspace.git_dirty={str(workspace.git_dirty).lower()}")
        if workspace.error:
            evidence.append(f"workspace.error={workspace.error[:160]}")
        checkpoint_head = task.checkpoint.get("git_head")
        if checkpoint_head and workspace.git_head:
            evidence.append(
                "workspace.checkpoint_head_match="
                + str(str(checkpoint_head) == workspace.git_head).lower()
            )

    if task.state == SupervisorState.ABANDONED:
        return Assessment(SupervisorState.ABANDONED, "task was explicitly abandoned", "high")

    if lsm:
        evidence.append(f"lsm.session={lsm.session_status or 'unknown'}")
        evidence.append(f"lsm.plan={lsm.plan_status or 'none'}")
        if lsm.in_flight_calls:
            evidence.append(f"lsm.in_flight={lsm.in_flight_calls}")
        if lsm.active_jobs:
            evidence.append(f"lsm.active_jobs={lsm.active_jobs}")
        if lsm.failed_jobs:
            evidence.append(f"lsm.failed_jobs={lsm.failed_jobs}")

        if lsm.session_status in {"completed", "cancelled"}:
            state = (
                SupervisorState.COMPLETED
                if lsm.session_status == "completed"
                else SupervisorState.ABANDONED
            )
            return Assessment(state, f"durable LSM session is {lsm.session_status}", "high", evidence)

        if lsm.plan_status == "blocked":
            return Assessment(SupervisorState.BLOCKED, "durable Goal plan is blocked", "high", evidence)

        if lsm.plan_status == "completed" and lsm.in_flight_calls == 0 and lsm.active_jobs == 0:
            return Assessment(
                SupervisorState.COMPLETED,
                "durable Goal plan completed with no tracked active work",
                "high",
                evidence,
            )

        if lsm.in_flight_calls > 0 or lsm.active_jobs > 0:
            return Assessment(
                SupervisorState.RUNNING,
                "durable Local Shell work is still active; UI state cannot override it",
                "high",
                evidence,
            )

    if workspace and not workspace.cwd_exists:
        return Assessment(
            SupervisorState.NEEDS_HUMAN,
            "registered working directory no longer exists",
            "high",
            evidence,
            requires_reconcile=True,
        )

    browser_age = None
    dom_silence = None
    if browser:
        browser_age = max(0.0, now - browser.observed_at)
        if browser.last_dom_change_at is not None:
            dom_silence = max(0.0, now - browser.last_dom_change_at)
        evidence.append(f"browser.observation_age_s={browser_age:.0f}")
        if dom_silence is not None:
            evidence.append(f"browser.dom_silence_s={dom_silence:.0f}")
        if browser.visible_error:
            evidence.append(f"browser.error={browser.visible_error[:120]}")
        if browser.pending_tool_calls is not None:
            evidence.append(f"browser.pending_tool_calls={browser.pending_tool_calls}")

    lsm_silence = None
    if lsm and lsm.recent_event_at is not None:
        lsm_silence = max(0.0, now - lsm.recent_event_at)
        evidence.append(f"lsm.event_silence_s={lsm_silence:.0f}")

    # A delivery/UI error is not permission to replay. Reconcile first because side effects may exist.
    if browser and browser.visible_error:
        return Assessment(
            SupervisorState.RECONCILING,
            "ChatGPT Web reports an error; reconcile durable LSM/workspace state before any retry",
            "high",
            evidence,
            requires_reconcile=True,
        )

    # The characteristic broken lifecycle: composer looks idle while a tool card remains pending.
    if (
        browser
        and browser.send_button_ready is True
        and (browser.pending_tool_calls or 0) > 0
        and (dom_silence is None or dom_silence >= policy.browser_suspect_after_s)
    ):
        return Assessment(
            SupervisorState.RECONCILING,
            "composer is ready while a tool call still appears pending; delivery lifecycle is contradictory",
            "high",
            evidence,
            requires_reconcile=True,
        )

    if lsm and lsm.plan_status == "active" and lsm.continuation_pending:
        return Assessment(
            SupervisorState.RUNNING,
            "LSM continuation is already pending; supervisor must not compete with it",
            "high",
            evidence,
        )

    if lsm and lsm.plan_status == "active" and lsm.continuation_due:
        return Assessment(
            SupervisorState.SUSPECT,
            "Goal plan execution lease expired with no durable work in flight",
            "high",
            evidence,
            requires_reconcile=True,
        )

    stale_browser = dom_silence is not None and dom_silence >= policy.browser_suspect_after_s
    stale_lsm = lsm_silence is not None and lsm_silence >= policy.lsm_suspect_after_s
    if stale_browser and stale_lsm:
        hard = max(dom_silence or 0, lsm_silence or 0) >= policy.hard_stall_after_s
        return Assessment(
            SupervisorState.SUSPECT,
            "both browser DOM and durable LSM activity are silent",
            "high" if hard else "medium",
            evidence,
            requires_reconcile=True,
        )

    if browser and browser.generating is True:
        return Assessment(SupervisorState.RUNNING, "browser reports active generation", "medium", evidence)

    if lsm and lsm.plan_status == "active":
        return Assessment(
            SupervisorState.RUNNING,
            "Goal plan remains active and no stall threshold has been crossed",
            "medium",
            evidence,
        )

    if task.state in {SupervisorState.QUEUED, SupervisorState.STARTING}:
        return Assessment(task.state, "no contradictory runtime evidence", "medium", evidence)

    return Assessment(
        SupervisorState.SUSPECT,
        "insufficient fresh evidence to prove progress or completion",
        "low",
        evidence,
        requires_reconcile=True,
    )
