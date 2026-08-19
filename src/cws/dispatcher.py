from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from .models import ReconciliationRecord, RecoveryRecommendation, SupervisorState, TaskRecord
from .reconcile import fence_matches


class DispatchAction(StrEnum):
    NONE = "NONE"
    OBSERVE = "OBSERVE"
    HUMAN_DECISION = "HUMAN_DECISION"
    CONTINUE_CURRENT_WORKER = "CONTINUE_CURRENT_WORKER"
    TAKEOVER_NEW_WORKER = "TAKEOVER_NEW_WORKER"


@dataclass(slots=True)
class DispatchPolicy:
    max_reconciliation_age_s: float = 120.0
    min_reconciliation_separation_s: float = 3.0
    max_browser_observation_age_s: float = 30.0
    max_network_observation_age_s: float = 30.0
    min_dom_quiet_s: float = 5.0
    min_network_quiet_s: float = 5.0
    transport_enabled: bool = False


@dataclass(slots=True)
class DispatchPlan:
    task_id: str
    created_at: float
    action: DispatchAction
    candidate_ready: bool
    transport_enabled: bool
    would_dispatch: bool
    reason: str
    previous_reconcile_id: str | None = None
    current_reconcile_id: str | None = None
    fence_token: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)


class DispatchDisabled(RuntimeError):
    pass


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def build_dispatch_plan(
    task: TaskRecord,
    recommendation: RecoveryRecommendation,
    *,
    previous: ReconciliationRecord | None,
    current: ReconciliationRecord | None,
    policy: DispatchPolicy | None = None,
    now: float | None = None,
) -> DispatchPlan:
    """Evaluate a recovery action without executing it.

    CWS keeps this planner transport-neutral. Normal `dispatch-plan` calls set
    `transport_enabled=false`; the separate explicit executor may set it true only after its
    own opt-in/task-confirmation checks. This planner prevents any transport from bypassing
    reconciliation and fence checks by accident.
    """
    policy = policy or DispatchPolicy()
    now = time.time() if now is None else float(now)
    checks: dict[str, bool] = {}
    blockers: list[str] = []

    action = _action_from_recommendation(recommendation)
    if action in {DispatchAction.NONE, DispatchAction.OBSERVE, DispatchAction.HUMAN_DECISION}:
        return DispatchPlan(
            task_id=task.task_id,
            created_at=now,
            action=action,
            candidate_ready=False,
            transport_enabled=policy.transport_enabled,
            would_dispatch=False,
            reason=recommendation.reason,
            previous_reconcile_id=previous.reconcile_id if previous else None,
            current_reconcile_id=current.reconcile_id if current else None,
            fence_token=current.fence_token if current else None,
            checks=checks,
            blockers=["recovery recommendation does not request a continue/takeover action"],
        )

    checks["recovery_budget_available"] = task.recovery_attempts < task.max_recovery_attempts
    if not checks["recovery_budget_available"]:
        blockers.append("recovery attempt budget is exhausted")

    checks["current_reconciliation_present"] = current is not None
    if current is None:
        blockers.append("no current reconciliation record")

    if current is not None:
        checks["current_reconciliation_fresh"] = (
            0.0 <= now - current.created_at <= max(1.0, policy.max_reconciliation_age_s)
        )
        if not checks["current_reconciliation_fresh"]:
            blockers.append("current reconciliation record is stale or from the future")
        checks["current_worker_matches_task"] = current.current_worker_id == task.current_worker_id
        if not checks["current_worker_matches_task"]:
            blockers.append("current worker changed after task registration/reconciliation")
        checks["recovery_state_allowed"] = current.state in {
            SupervisorState.SUSPECT.value,
            SupervisorState.RECONCILING.value,
        }
        if not checks["recovery_state_allowed"]:
            blockers.append(f"current state {current.state} is not a recoverable stalled state")

    # Two-phase semantic-fence confirmation is mandatory in V3. There is deliberately no
    # production option to downgrade this to one sample.
    checks["previous_reconciliation_present"] = previous is not None
    if previous is None:
        blockers.append("two-phase dispatch requires a previous reconciliation record")
    checks["distinct_reconciliation_samples"] = bool(
        previous and current and previous.reconcile_id != current.reconcile_id
    )
    if not checks["distinct_reconciliation_samples"]:
        blockers.append("dispatch requires two distinct reconciliation samples")
    checks["previous_reconciliation_fresh"] = bool(
        previous
        and 0.0 <= now - previous.created_at <= max(1.0, policy.max_reconciliation_age_s)
    )
    if not checks["previous_reconciliation_fresh"]:
        blockers.append("previous reconciliation record is stale or from the future")
    separation = current.created_at - previous.created_at if previous and current else None
    checks["reconciliation_separation_sufficient"] = bool(
        separation is not None
        and separation >= max(0.0, policy.min_reconciliation_separation_s)
    )
    if not checks["reconciliation_separation_sufficient"]:
        blockers.append("reconciliation samples are too close together to prove stability")
    checks["fence_stable"] = bool(previous and current and fence_matches(previous, current))
    if not checks["fence_stable"]:
        blockers.append("reconciliation fence changed between samples")

    snapshot = current.snapshot if current is not None else {}
    task_snapshot = snapshot.get("task") or {}
    lsm = snapshot.get("lsm") or {}
    browser = snapshot.get("browser") or {}
    network = snapshot.get("network") or {}
    workspace = snapshot.get("workspace") or {}

    checks["lsm_present"] = bool(lsm) and lsm.get("session_id") == task.lsm_session_id
    if not checks["lsm_present"]:
        blockers.append("durable LSM session evidence is missing or belongs to a different session")
    checks["lsm_session_active"] = lsm.get("session_status") == "active"
    if not checks["lsm_session_active"]:
        blockers.append("durable LSM session is not active")
    checks["lsm_no_inflight_calls"] = int(lsm.get("in_flight_calls") or 0) == 0
    checks["lsm_no_active_jobs"] = int(lsm.get("active_jobs") or 0) == 0
    checks["lsm_no_continuation_pending"] = not bool(lsm.get("continuation_pending"))
    if not checks["lsm_no_inflight_calls"]:
        blockers.append("LSM has an in-flight tool call")
    if not checks["lsm_no_active_jobs"]:
        blockers.append("LSM has a tracked active job")
    if not checks["lsm_no_continuation_pending"]:
        blockers.append("LSM continuation is already pending")

    checks["workspace_exists"] = bool(workspace.get("cwd_exists"))
    if not checks["workspace_exists"]:
        blockers.append("registered workspace does not exist")

    checks["registered_worker_active"] = task_snapshot.get("registered_worker_status") == "active"
    if not checks["registered_worker_active"]:
        blockers.append("registered current worker is not marked active")
    registered_worker_url = task_snapshot.get("registered_worker_url")
    checks["registered_worker_url_present"] = bool(registered_worker_url)
    if not checks["registered_worker_url_present"]:
        blockers.append("registered current worker URL is missing from the reconciliation fence")

    if not browser:
        action = DispatchAction.TAKEOVER_NEW_WORKER
        checks["browser_worker_observed"] = False
        blockers.append(
            "current browser worker is not positively observed; automatic takeover requires a "
            "separate explicit new-worker binding protocol"
        )
    else:
        checks["browser_worker_observed"] = browser.get("worker_id") == task.current_worker_id
        if not checks["browser_worker_observed"]:
            blockers.append("browser observation does not belong to the current worker")
        checks["browser_url_matches_registered"] = bool(
            registered_worker_url
            and _normalize_url(str(browser.get("url") or ""))
            == _normalize_url(str(registered_worker_url))
        )
        if not checks["browser_url_matches_registered"]:
            blockers.append("browser URL does not exactly match the registered worker URL")
        browser_observed_at = browser.get("observed_at")
        checks["browser_observation_fresh"] = bool(
            browser_observed_at is not None
            and 0.0
            <= now - float(browser_observed_at)
            <= max(1.0, policy.max_browser_observation_age_s)
        )
        if not checks["browser_observation_fresh"]:
            blockers.append("browser observation is too stale for recovery dispatch")
        checks["browser_not_generating"] = browser.get("generating") is not True
        if not checks["browser_not_generating"]:
            blockers.append("browser still reports active generation")
        checks["composer_ready"] = browser.get("send_button_ready") is True
        if not checks["composer_ready"]:
            blockers.append("no positive ready-Send/composer evidence")
        last_dom_change_at = browser.get("last_dom_change_at")
        checks["dom_quiet_enough"] = bool(
            last_dom_change_at is not None
            and now - float(last_dom_change_at) >= max(0.0, policy.min_dom_quiet_s)
        )
        if not checks["dom_quiet_enough"]:
            blockers.append("DOM/UI has not been stably quiet for the required interval")

    if network:
        network_observed_at = network.get("observed_at")
        checks["network_observation_fresh"] = bool(
            network_observed_at is not None
            and 0.0
            <= now - float(network_observed_at)
            <= max(1.0, policy.max_network_observation_age_s)
        )
        if not checks["network_observation_fresh"]:
            blockers.append("network observation is too stale for recovery dispatch")
        quiet_since = network.get("quiet_since_at")
        checks["network_quiet_enough"] = bool(
            quiet_since is not None
            and now - float(quiet_since) >= max(0.0, policy.min_network_quiet_s)
        )
        if not checks["network_quiet_enough"]:
            blockers.append("network lifecycle is not stably quiet for the required interval")
    else:
        # Network observation is optional. The stronger LSM/browser/workspace fences still
        # apply when normal Chrome has no CDP endpoint.
        checks["network_quiet_enough"] = True

    candidate_ready = not blockers and action == DispatchAction.CONTINUE_CURRENT_WORKER
    would_dispatch = candidate_ready and policy.transport_enabled
    if candidate_ready and not policy.transport_enabled:
        reason = (
            "all deterministic preconditions are satisfied, but transport is disabled for "
            "this plan; dry-run only"
        )
    elif candidate_ready:
        reason = "all deterministic preconditions are satisfied"
    elif action == DispatchAction.TAKEOVER_NEW_WORKER:
        reason = "takeover requires a separately bound replacement worker and remains disabled"
    else:
        reason = "recovery dispatch is blocked by one or more deterministic preconditions"

    return DispatchPlan(
        task_id=task.task_id,
        created_at=now,
        action=action,
        candidate_ready=candidate_ready,
        transport_enabled=policy.transport_enabled,
        would_dispatch=would_dispatch,
        reason=reason,
        previous_reconcile_id=previous.reconcile_id if previous else None,
        current_reconcile_id=current.reconcile_id if current else None,
        fence_token=current.fence_token if current else None,
        checks=checks,
        blockers=blockers,
    )


def _action_from_recommendation(recommendation: RecoveryRecommendation) -> DispatchAction:
    if recommendation.action == "none":
        return DispatchAction.NONE
    if recommendation.action == "observe":
        return DispatchAction.OBSERVE
    if recommendation.action == "human_decision":
        return DispatchAction.HUMAN_DECISION
    if recommendation.action == "reconcile_then_continue":
        return DispatchAction.CONTINUE_CURRENT_WORKER
    return DispatchAction.HUMAN_DECISION


def execute_dispatch(_plan: DispatchPlan) -> None:
    """Legacy generic executor remains disabled; use the fenced dispatch_runtime path."""
    raise DispatchDisabled(
        "generic recovery dispatch is disabled; use dispatch-plan or the explicit fenced executor"
    )
