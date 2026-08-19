from __future__ import annotations

from dataclasses import dataclass, replace
import time

from .actions import ActionAttemptState
from .dispatcher import DispatchPlan
from .models import BrowserObservation
from .registry import OBSERVATION_RETENTION_PER_ENTITY, Registry


RECOVERABLE_DELIVERY_ERRORS = frozenset(
    {
        "message delivery timed out",
        "there was an error generating a response",
        "something went wrong",
    }
)


@dataclass(frozen=True, slots=True)
class TimeoutRecoveryPolicy:
    """Narrow opt-in gate for resident recovery of explicit ChatGPT Web delivery errors.

    This policy does not grant transport authority by itself. The ordinary two-sample
    reconciliation, LSM/workspace, exact-window, action-attempt, recovery-budget, and
    nonce acknowledgement fences still have to pass.
    """

    enabled: bool = False
    cooldown_s: float = 60.0


def normalized_visible_error(browser: BrowserObservation | None) -> str | None:
    if browser is None or not browser.visible_error:
        return None
    return " ".join(browser.visible_error.strip().lower().split())


def is_recoverable_delivery_error(browser: BrowserObservation | None) -> bool:
    marker = normalized_visible_error(browser)
    if marker is None:
        return False
    return any(known in marker for known in RECOVERABLE_DELIVERY_ERRORS)


def error_shadowed_by_active_generation(
    registry: Registry,
    *,
    task_id: str,
    browser: BrowserObservation | None,
) -> bool:
    """Treat an error banner as stale/ambiguous after it coexists with a newer generation.

    ChatGPT can leave an old delivery-timeout banner visible after the user has manually
    sent a new turn. If the current contiguous run of the same visible error ever coexisted
    with `generating=True`, resident recovery must not send again until that error disappears
    (or changes) and a later fresh error is observed.
    """

    marker = normalized_visible_error(browser)
    if browser is None or marker is None:
        return False
    task = registry.get_task(task_id)
    worker_id = task.current_worker_id
    if not worker_id or browser.worker_id != worker_id:
        return True
    history = registry.browser_observation_history(
        worker_id,
        limit=OBSERVATION_RETENTION_PER_ENTITY,
    )
    for observed in history:
        observed_marker = normalized_visible_error(observed)
        if observed_marker != marker:
            return False
        if observed.generating is True:
            return True

    # If the entire retained window is still the same error episode, its true start is
    # no longer observable. Fail closed instead of treating an aged-out active-generation
    # sample as proof that this is a fresh timeout.
    return len(history) >= OBSERVATION_RETENTION_PER_ENTITY


def acknowledged_state_is_unchanged(
    registry: Registry,
    *,
    task_id: str,
    browser: BrowserObservation | None,
) -> bool:
    """Suppress replay while the UI is unchanged from a positively acknowledged recovery.

    Old ChatGPT error banners can remain visible after a successful continuation. The
    acknowledgement observer records the post-action text signature in action metadata;
    seeing that exact signature again is evidence to observe, not send another `continue`.
    """

    if browser is None or not browser.message_signature:
        return False
    for attempt in registry.action_attempts(task_id, limit=20):
        if attempt.state != ActionAttemptState.ACKNOWLEDGED:
            continue
        ack_signature = str(attempt.metadata.get("ack_browser_signature") or "")
        if ack_signature:
            return ack_signature == browser.message_signature
        return False
    return False


def gate_timeout_dispatch_plan(
    registry: Registry,
    plan: DispatchPlan,
    *,
    browser: BrowserObservation | None,
    policy: TimeoutRecoveryPolicy,
    now: float | None = None,
) -> DispatchPlan:
    """Apply the narrow resident-autorecovery gate without weakening normal dispatch fences."""

    current = time.time() if now is None else float(now)
    blockers = list(plan.blockers)
    checks = dict(plan.checks)
    checks["timeout_autorecovery_enabled"] = bool(policy.enabled)
    checks["explicit_recoverable_delivery_error"] = is_recoverable_delivery_error(browser)
    checks["timeout_state_not_already_acknowledged"] = not acknowledged_state_is_unchanged(
        registry,
        task_id=plan.task_id,
        browser=browser,
    )
    checks["timeout_error_not_shadowed_by_newer_generation"] = not error_shadowed_by_active_generation(
        registry,
        task_id=plan.task_id,
        browser=browser,
    )
    recent = registry.action_attempts(plan.task_id, limit=1)
    last_action_at = recent[0].created_at if recent else None
    elapsed = current - float(last_action_at) if last_action_at is not None else None
    checks["timeout_recovery_cooldown_elapsed"] = bool(
        elapsed is None
        or (elapsed >= max(0.0, float(policy.cooldown_s)) and elapsed >= 0.0)
    )

    if not checks["timeout_autorecovery_enabled"]:
        blockers.append("resident timeout autorecovery is not explicitly enabled")
    if not checks["explicit_recoverable_delivery_error"]:
        blockers.append("no explicit recoverable ChatGPT Web delivery error is present")
    if not checks["timeout_state_not_already_acknowledged"]:
        blockers.append("current UI signature already has a positive recovery acknowledgement")
    if not checks["timeout_error_not_shadowed_by_newer_generation"]:
        blockers.append("visible delivery error coexisted with a newer active generation; wait for the stale banner to clear")
    if not checks["timeout_recovery_cooldown_elapsed"]:
        blockers.append("resident timeout recovery cooldown has not elapsed")

    allowed = plan.candidate_ready and not blockers
    return replace(
        plan,
        candidate_ready=allowed,
        would_dispatch=allowed and plan.transport_enabled,
        checks=checks,
        blockers=blockers,
        reason=(
            "explicit delivery timeout is stable across all recovery fences"
            if allowed
            else "resident timeout recovery is blocked by deterministic safety gates"
        ),
    )
