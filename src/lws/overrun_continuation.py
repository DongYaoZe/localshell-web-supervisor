from __future__ import annotations

from dataclasses import dataclass
import time

from .actions import ActionAttemptState
from .dispatcher import DispatchAction, DispatchPlan
from .models import BrowserObservation, ReconciliationRecord, SupervisorState, TaskRecord, WorkerStatus
from .reconcile import fence_matches
from .registry import Registry
from .uia import normalize_url


DEFAULT_OVERRUN_AFTER_S = 25 * 60 + 20
OVERRUN_CONTINUE_PROMPT = "continue"
OVERRUN_TRIGGER_KIND = "hard_overrun_25m20"

_EFFECTIVE_CONTINUE_STATES = {
    ActionAttemptState.SUBMITTED,
    ActionAttemptState.RECONCILE_REQUIRED,
    ActionAttemptState.ACKNOWLEDGED,
}
_TERMINAL_STATES = {SupervisorState.COMPLETED, SupervisorState.ABANDONED}
_BLOCKED_ASSESSMENT_STATES = {
    SupervisorState.COMPLETED.value,
    SupervisorState.ABANDONED.value,
    SupervisorState.BLOCKED.value,
    SupervisorState.NEEDS_HUMAN.value,
}


@dataclass(frozen=True, slots=True)
class OverrunContinuationPolicy:
    """Policy for the hard wall-clock continuation nudge.

    This is intentionally separate from fault recovery. A hard-overrun continuation does
    not consume the bounded recovery budget, but still requires two stable reconciliation
    samples plus an exact current-worker UIA binding before any browser mutation.
    """

    enabled: bool = False
    overrun_after_s: float = float(DEFAULT_OVERRUN_AFTER_S)
    sample_lead_s: float = 60.0
    retry_cooldown_s: float = 120.0
    max_reconciliation_age_s: float = 120.0
    min_reconciliation_separation_s: float = 3.0
    max_browser_observation_age_s: float = 30.0
    min_dom_quiet_s: float = 2.0
    auto_discover_visible_conversations: bool = False


@dataclass(frozen=True, slots=True)
class OverrunClock:
    task_id: str
    worker_id: str
    anchor_at: float
    elapsed_s: float
    due_at: float
    due: bool
    sample_due: bool


def latest_observed_generation_started_at(
    registry: Registry,
    worker_id: str,
) -> float | None:
    """Return the start of the most recent observed generating=True episode.

    History is newest-first. Initial idle samples are skipped, then the newest contiguous
    generating run is walked backwards to its earliest observed sample. This lets a manual
    user turn reset the hard timer without letting streaming DOM changes continuously reset it.
    """

    history = registry.browser_observation_history(worker_id, limit=2000)
    found_generation = False
    earliest = None
    for observed in history:
        if not found_generation:
            if observed.generating is True:
                found_generation = True
                earliest = float(observed.observed_at)
            continue
        if observed.generating is True:
            earliest = float(observed.observed_at)
            continue
        break
    return earliest


def _effective_continue_at(registry: Registry, task_id: str, worker_id: str) -> float | None:
    for attempt in registry.action_attempts(task_id, limit=200):
        if attempt.worker_id != worker_id or attempt.action != DispatchAction.CONTINUE_CURRENT_WORKER.value:
            continue
        if attempt.state not in _EFFECTIVE_CONTINUE_STATES:
            continue
        if attempt.submitted_at is not None:
            return float(attempt.submitted_at)
        # RECONCILE_REQUIRED may represent a send whose return path was lost. Treat the
        # durable ambiguity timestamp as a conservative new-turn anchor and do not replay.
        return float(attempt.updated_at or attempt.created_at)
    return None


def overrun_clock(
    registry: Registry,
    task_id: str,
    *,
    policy: OverrunContinuationPolicy,
    now: float | None = None,
) -> OverrunClock | None:
    current = time.time() if now is None else float(now)
    task = registry.get_task(task_id)
    if task.state in _TERMINAL_STATES or not task.current_worker_id:
        return None
    worker = registry.get_worker(task.current_worker_id)
    if worker.status != WorkerStatus.ACTIVE:
        return None

    anchor = float(worker.started_at)
    effective_continue = _effective_continue_at(registry, task_id, worker.worker_id)
    if effective_continue is not None:
        anchor = max(anchor, effective_continue)
    generation_start = latest_observed_generation_started_at(registry, worker.worker_id)
    if generation_start is not None:
        anchor = max(anchor, generation_start)

    threshold = max(1.0, float(policy.overrun_after_s))
    elapsed = current - anchor
    due_at = anchor + threshold
    lead = max(0.0, min(float(policy.sample_lead_s), threshold))
    return OverrunClock(
        task_id=task_id,
        worker_id=worker.worker_id,
        anchor_at=anchor,
        elapsed_s=elapsed,
        due_at=due_at,
        due=elapsed >= threshold and elapsed >= 0.0,
        sample_due=elapsed >= threshold - lead and elapsed >= 0.0,
    )


def _latest_failed_action_at(registry: Registry, task_id: str, worker_id: str) -> float | None:
    for attempt in registry.action_attempts(task_id, limit=50):
        if attempt.worker_id != worker_id:
            continue
        if attempt.state == ActionAttemptState.FAILED:
            return float(attempt.updated_at or attempt.created_at)
        if attempt.state in _EFFECTIVE_CONTINUE_STATES:
            return None
    return None


def build_overrun_dispatch_plan(
    registry: Registry,
    task: TaskRecord,
    *,
    clock: OverrunClock,
    previous: ReconciliationRecord | None,
    current: ReconciliationRecord | None,
    browser: BrowserObservation | None,
    policy: OverrunContinuationPolicy,
    transport_enabled: bool,
    now: float | None = None,
) -> DispatchPlan:
    """Build the hard-overrun continuation plan without fault-recovery budget/LSM-idle gates."""

    ts = time.time() if now is None else float(now)
    checks: dict[str, bool] = {}
    blockers: list[str] = []

    checks["overrun_autocontinue_enabled"] = bool(policy.enabled)
    checks["hard_overrun_due"] = bool(clock.due)
    checks["task_not_terminal"] = task.state not in _TERMINAL_STATES
    checks["current_worker_matches_clock"] = task.current_worker_id == clock.worker_id

    if not checks["overrun_autocontinue_enabled"]:
        blockers.append("hard-overrun auto-continue is not explicitly enabled")
    if not checks["hard_overrun_due"]:
        blockers.append("current work turn has not exceeded the hard-overrun threshold")
    if not checks["task_not_terminal"]:
        blockers.append("task is terminal")
    if not checks["current_worker_matches_clock"]:
        blockers.append("current worker changed after the overrun clock was evaluated")

    checks["current_reconciliation_present"] = current is not None
    checks["previous_reconciliation_present"] = previous is not None
    if current is None:
        blockers.append("no current reconciliation record")
    if previous is None:
        blockers.append("two-phase overrun continuation requires a previous reconciliation sample")

    if current is not None:
        checks["current_reconciliation_fresh"] = (
            0.0 <= ts - current.created_at <= max(1.0, float(policy.max_reconciliation_age_s))
        )
        checks["current_worker_matches_task"] = current.current_worker_id == task.current_worker_id
        checks["assessment_allows_overrun_nudge"] = current.state not in _BLOCKED_ASSESSMENT_STATES
        if not checks["current_reconciliation_fresh"]:
            blockers.append("current reconciliation record is stale or from the future")
        if not checks["current_worker_matches_task"]:
            blockers.append("current worker changed during reconciliation")
        if not checks["assessment_allows_overrun_nudge"]:
            blockers.append(f"current assessment state {current.state} forbids automatic continuation")

    checks["distinct_reconciliation_samples"] = bool(
        previous and current and previous.reconcile_id != current.reconcile_id
    )
    checks["previous_reconciliation_fresh"] = bool(
        previous
        and 0.0 <= ts - previous.created_at <= max(1.0, float(policy.max_reconciliation_age_s))
    )
    separation = current.created_at - previous.created_at if previous and current else None
    checks["reconciliation_separation_sufficient"] = bool(
        separation is not None
        and separation >= max(0.0, float(policy.min_reconciliation_separation_s))
    )
    checks["fence_stable"] = bool(previous and current and fence_matches(previous, current))
    if not checks["distinct_reconciliation_samples"]:
        blockers.append("overrun continuation requires two distinct reconciliation samples")
    if not checks["previous_reconciliation_fresh"]:
        blockers.append("previous reconciliation record is stale or from the future")
    if not checks["reconciliation_separation_sufficient"]:
        blockers.append("reconciliation samples are too close together")
    if not checks["fence_stable"]:
        blockers.append("reconciliation fence changed between overrun samples")

    worker = registry.get_worker(clock.worker_id)
    checks["registered_worker_active"] = worker.status == WorkerStatus.ACTIVE
    if not checks["registered_worker_active"]:
        blockers.append("registered current worker is not active")

    checks["browser_observed"] = browser is not None and browser.worker_id == clock.worker_id
    if not checks["browser_observed"]:
        blockers.append("current worker has no positive browser observation")
    if browser is not None:
        checks["browser_url_matches_registered"] = bool(
            browser.url and normalize_url(browser.url) == normalize_url(worker.conversation_url)
        )
        checks["browser_observation_fresh"] = (
            0.0
            <= ts - float(browser.observed_at)
            <= max(1.0, float(policy.max_browser_observation_age_s))
        )
        checks["browser_not_generating"] = browser.generating is not True
        checks["composer_available"] = bool(
            browser.raw.get("composer_present") is True or browser.send_button_ready is True
        )
        last_dom_change_at = browser.last_dom_change_at
        checks["dom_quiet_enough"] = bool(
            last_dom_change_at is not None
            and ts - float(last_dom_change_at) >= max(0.0, float(policy.min_dom_quiet_s))
        )
        if not checks["browser_url_matches_registered"]:
            blockers.append("browser URL does not exactly match the registered worker URL")
        if not checks["browser_observation_fresh"]:
            blockers.append("browser observation is too stale for overrun continuation")
        if not checks["browser_not_generating"]:
            blockers.append("browser still reports active generation; wait for the composer")
        if not checks["composer_available"]:
            blockers.append("no positive composer-presence evidence")
        if not checks["dom_quiet_enough"]:
            blockers.append("DOM/UI has not been stably quiet long enough")

    failed_at = _latest_failed_action_at(registry, task.task_id, clock.worker_id)
    retry_elapsed = None if failed_at is None else ts - failed_at
    checks["overrun_retry_cooldown_elapsed"] = bool(
        failed_at is None
        or (
            retry_elapsed is not None
            and retry_elapsed >= max(0.0, float(policy.retry_cooldown_s))
            and retry_elapsed >= 0.0
        )
    )
    if not checks["overrun_retry_cooldown_elapsed"]:
        blockers.append("hard-overrun continuation retry cooldown has not elapsed")

    candidate_ready = not blockers
    return DispatchPlan(
        task_id=task.task_id,
        created_at=ts,
        action=DispatchAction.CONTINUE_CURRENT_WORKER,
        candidate_ready=candidate_ready,
        transport_enabled=bool(transport_enabled),
        would_dispatch=candidate_ready and bool(transport_enabled),
        reason=(
            "hard-overrun threshold exceeded and exact current-worker fences are stable"
            if candidate_ready
            else "hard-overrun continuation is blocked by deterministic safety gates"
        ),
        previous_reconcile_id=previous.reconcile_id if previous else None,
        current_reconcile_id=current.reconcile_id if current else None,
        fence_token=current.fence_token if current else None,
        checks=checks,
        blockers=blockers,
    )
