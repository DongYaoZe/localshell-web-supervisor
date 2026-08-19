from __future__ import annotations

from dataclasses import dataclass, replace
import time

from .actions import ActionAttemptState
from .dispatcher import DispatchPlan
from .models import BrowserObservation
from .registry import Registry


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
