from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import StrEnum

from .actions import ActionAttempt, UNRESOLVED_ACTION_STATES
from .capabilities import CapabilityContext, capability_matches_context
from .models import (
    Assessment,
    LsmObservation,
    PageCapabilityKind,
    PageCapabilityRecord,
    ProbeMutationOperation,
    ReconciliationRecord,
    SupervisorState,
    TaskRecord,
    WorkerRecord,
    WorkerStatus,
    WorkerWindowBinding,
    WorkspaceObservation,
)
from .probe_ops import UNRESOLVED_PROBE_MUTATION_STATES
from .reconcile import fence_matches


class OrchestrationDecisionKind(StrEnum):
    OBSERVE = "observe"
    RECONCILE = "reconcile"
    RECOMMEND_ACTION = "recommend-action"
    WAIT_COOLDOWN = "wait/cooldown"
    BLOCKED_HUMAN = "blocked/human"


@dataclass(slots=True, frozen=True)
class OrchestrationPolicy:
    max_lsm_observation_age_s: float = 30.0
    max_workspace_observation_age_s: float = 30.0
    max_reconciliation_age_s: float = 120.0
    min_reconciliation_separation_s: float = 3.0
    recovery_cooldown_s: float = 60.0
    base_backoff_s: float = 5.0
    max_backoff_s: float = 60.0
    max_selected_tasks: int = 1


@dataclass(slots=True)
class TaskOrchestrationInput:
    """Pure policy input assembled by a read-only durable-evidence adapter.

    `worker_lease_expires_at`, `last_recovery_at`, `last_scheduled_at`, and
    `consecutive_waits` intentionally remain adapter inputs until durable persistence owns
    those fields. Callers must not invent missing lease or scheduler history.
    """

    task: TaskRecord
    assessment: Assessment
    worker: WorkerRecord | None = None
    lsm: LsmObservation | None = None
    workspace: WorkspaceObservation | None = None
    previous_reconciliation: ReconciliationRecord | None = None
    current_reconciliation: ReconciliationRecord | None = None
    unresolved_action: ActionAttempt | None = None
    unresolved_probe_mutation: ProbeMutationOperation | None = None
    worker_lease_expires_at: float | None = None
    last_recovery_at: float | None = None
    last_scheduled_at: float | None = None
    consecutive_waits: int = 0
    requires_exact_window_binding: bool = True
    window_binding: WorkerWindowBinding | None = None
    page_continuity_relevant: bool = False
    page_capability: PageCapabilityRecord | None = None
    capability_context: CapabilityContext | None = None
    page_capability_kind: PageCapabilityKind = PageCapabilityKind.GENERATION
    reconcile_blockers: tuple[str, ...] = ()
    human_blockers: tuple[str, ...] = ()
    scheduling_history_known: bool = True


@dataclass(slots=True)
class OrchestrationDecision:
    task_id: str
    kind: OrchestrationDecisionKind
    reason: str
    selected: bool = False
    retry_after_s: float | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    deferred_kind: OrchestrationDecisionKind | None = None
    mutation_allowed: bool = False


_RECOVERY_STATES = {
    SupervisorState.SUSPECT,
    SupervisorState.RECONCILING,
    SupervisorState.RECOVERING,
}

_FAIRNESS_STATE_RANK = {
    SupervisorState.RECOVERING: 0,
    SupervisorState.RECONCILING: 1,
    SupervisorState.SUSPECT: 2,
}


def _age_is_fresh(observed_at: float, *, now: float, max_age_s: float) -> bool:
    age = now - float(observed_at)
    return 0.0 <= age <= max(1.0, float(max_age_s))


def _bounded_backoff(policy: OrchestrationPolicy, consecutive_waits: int) -> float:
    base = max(0.1, float(policy.base_backoff_s))
    maximum = max(base, float(policy.max_backoff_s))
    exponent = min(16, max(0, int(consecutive_waits)))
    return min(maximum, base * (2**exponent))


def _decision(
    item: TaskOrchestrationInput,
    kind: OrchestrationDecisionKind,
    reason: str,
    *,
    checks: dict[str, bool],
    blockers: list[str] | None = None,
    retry_after_s: float | None = None,
) -> OrchestrationDecision:
    return OrchestrationDecision(
        task_id=item.task.task_id,
        kind=kind,
        reason=reason,
        selected=False,
        retry_after_s=retry_after_s,
        checks=checks,
        blockers=list(blockers or []),
        mutation_allowed=False,
    )


def evaluate_task(
    item: TaskOrchestrationInput,
    *,
    now: float | None = None,
    policy: OrchestrationPolicy | None = None,
) -> OrchestrationDecision:
    """Evaluate one task without executing, dispatching, or mutating anything."""

    now = time.time() if now is None else float(now)
    policy = policy or OrchestrationPolicy()
    checks: dict[str, bool] = {}
    task = item.task
    assessment = item.assessment
    backoff = _bounded_backoff(policy, item.consecutive_waits)

    if task.state in {SupervisorState.BLOCKED, SupervisorState.NEEDS_HUMAN}:
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            f"durable task state {task.state.value} requires explicit human resolution",
            checks=checks,
        )
    if assessment.state in {SupervisorState.BLOCKED, SupervisorState.NEEDS_HUMAN}:
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            f"current assessment {assessment.state.value} requires explicit human resolution",
            checks=checks,
        )
    if task.state in {SupervisorState.COMPLETED, SupervisorState.ABANDONED} or assessment.state in {
        SupervisorState.COMPLETED,
        SupervisorState.ABANDONED,
    }:
        return _decision(
            item,
            OrchestrationDecisionKind.OBSERVE,
            "task is terminal; no recovery scheduling is appropriate",
            checks=checks,
        )
    if assessment.state in {
        SupervisorState.RUNNING,
        SupervisorState.STARTING,
        SupervisorState.QUEUED,
    }:
        return _decision(
            item,
            OrchestrationDecisionKind.OBSERVE,
            "current assessment still indicates live or not-yet-stalled work",
            checks=checks,
            retry_after_s=backoff,
        )
    if task.state not in _RECOVERY_STATES and assessment.state not in _RECOVERY_STATES:
        return _decision(
            item,
            OrchestrationDecisionKind.OBSERVE,
            "task is not in a recovery-eligible state",
            checks=checks,
            retry_after_s=backoff,
        )

    probe_operation = item.unresolved_probe_mutation
    probe_state = (
        getattr(probe_operation.state, "value", probe_operation.state)
        if probe_operation is not None
        else None
    )
    unresolved_probe_values = {state.value for state in UNRESOLVED_PROBE_MUTATION_STATES}
    checks["no_unresolved_probe_mutation"] = not (
        probe_operation is not None and probe_state in unresolved_probe_values
    )
    if not checks["no_unresolved_probe_mutation"]:
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            "a global probe-window mutation outcome is unresolved; do not overlap recovery mutation",
            checks=checks,
            blockers=[
                f"unresolved probe mutation {probe_operation.operation_id} is {probe_state}"
            ],
        )

    attempt = item.unresolved_action
    checks["no_unresolved_action_attempt"] = not (
        attempt is not None and attempt.state in UNRESOLVED_ACTION_STATES
    )
    if not checks["no_unresolved_action_attempt"]:
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            "a prior external action outcome is unresolved; never replay an ambiguous action",
            checks=checks,
            blockers=[f"unresolved action attempt {attempt.attempt_id} is {attempt.state.value}"],
        )

    checks["recovery_budget_available"] = task.recovery_attempts < task.max_recovery_attempts
    if not checks["recovery_budget_available"]:
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            "recovery budget is exhausted",
            checks=checks,
            blockers=["recovery-attempt budget exhausted"],
        )

    checks["no_external_human_blockers"] = not item.human_blockers
    if item.human_blockers:
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            "external durable evidence requires explicit human resolution",
            checks=checks,
            blockers=list(item.human_blockers),
        )

    checks["no_external_reconcile_blockers"] = not item.reconcile_blockers
    if item.reconcile_blockers:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "external durable evidence must be reconciled before recovery",
            checks=checks,
            blockers=list(item.reconcile_blockers),
            retry_after_s=backoff,
        )

    lsm = item.lsm
    checks["lsm_present"] = bool(
        lsm and lsm.task_id == task.task_id and lsm.session_id == task.lsm_session_id
    )
    if not checks["lsm_present"]:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "durable LSM session evidence is missing or belongs to another session",
            checks=checks,
            retry_after_s=backoff,
        )
    assert lsm is not None
    checks["lsm_fresh"] = _age_is_fresh(
        lsm.observed_at,
        now=now,
        max_age_s=policy.max_lsm_observation_age_s,
    )
    if not checks["lsm_fresh"]:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "LSM evidence is stale; refresh durable runtime state before recovery",
            checks=checks,
            retry_after_s=backoff,
        )
    if lsm.session_status in {"completed", "cancelled"}:
        return _decision(
            item,
            OrchestrationDecisionKind.OBSERVE,
            f"durable LSM session is already {lsm.session_status}",
            checks=checks,
        )
    if lsm.plan_status == "blocked":
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            "durable Goal plan is blocked",
            checks=checks,
        )

    checks["lsm_no_inflight_calls"] = lsm.in_flight_calls == 0
    checks["lsm_no_active_jobs"] = lsm.active_jobs == 0
    checks["lsm_no_continuation_pending"] = not bool(lsm.continuation_pending)
    if not checks["lsm_no_inflight_calls"] or not checks["lsm_no_active_jobs"]:
        return _decision(
            item,
            OrchestrationDecisionKind.OBSERVE,
            "durable Local Shell work is still active; do not compete with it",
            checks=checks,
            retry_after_s=backoff,
        )
    if not checks["lsm_no_continuation_pending"]:
        return _decision(
            item,
            OrchestrationDecisionKind.OBSERVE,
            "LSM continuation is already pending; supervisor must not race it",
            checks=checks,
            retry_after_s=backoff,
        )

    workspace = item.workspace
    checks["workspace_present"] = workspace is not None
    if workspace is None:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "workspace/Git evidence is missing",
            checks=checks,
            retry_after_s=backoff,
        )
    checks["workspace_task_matches"] = workspace.task_id == task.task_id
    checks["workspace_cwd_matches"] = (
        workspace.cwd.replace("\\", "/").rstrip("/").casefold()
        == task.cwd.replace("\\", "/").rstrip("/").casefold()
    )
    if not checks["workspace_task_matches"] or not checks["workspace_cwd_matches"]:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "workspace evidence belongs to a different task or working directory",
            checks=checks,
            retry_after_s=backoff,
        )
    checks["workspace_fresh"] = _age_is_fresh(
        workspace.observed_at,
        now=now,
        max_age_s=policy.max_workspace_observation_age_s,
    )
    if not checks["workspace_fresh"]:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "workspace/Git evidence is stale",
            checks=checks,
            retry_after_s=backoff,
        )
    checks["workspace_exists"] = workspace.cwd_exists
    if not checks["workspace_exists"]:
        return _decision(
            item,
            OrchestrationDecisionKind.BLOCKED_HUMAN,
            "registered workspace no longer exists",
            checks=checks,
        )
    checks["workspace_probe_clean"] = workspace.error is None
    if not checks["workspace_probe_clean"]:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "workspace/Git probe reported an error",
            checks=checks,
            retry_after_s=backoff,
        )
    checkpoint_head = task.checkpoint.get("git_head")
    checks["checkpoint_git_head_matches"] = not checkpoint_head or (
        workspace.git_head is not None and str(checkpoint_head) == workspace.git_head
    )
    if not checks["checkpoint_git_head_matches"]:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "Git HEAD changed relative to the durable task checkpoint",
            checks=checks,
            retry_after_s=backoff,
        )

    worker = item.worker
    checks["worker_present"] = worker is not None
    checks["worker_is_current"] = bool(
        worker
        and task.current_worker_id
        and worker.worker_id == task.current_worker_id
        and worker.task_id == task.task_id
    )
    checks["worker_active"] = bool(worker and worker.status == WorkerStatus.ACTIVE)
    checks["worker_lease_fresh"] = bool(
        item.worker_lease_expires_at is not None and item.worker_lease_expires_at > now
    )
    if not all(
        checks[name]
        for name in ("worker_present", "worker_is_current", "worker_active", "worker_lease_fresh")
    ):
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "current worker lease is missing, stale, or no longer owns the task",
            checks=checks,
            retry_after_s=backoff,
        )
    assert worker is not None

    if item.last_recovery_at is not None:
        elapsed = now - float(item.last_recovery_at)
        if elapsed < 0:
            return _decision(
                item,
                OrchestrationDecisionKind.RECONCILE,
                "recorded recovery timestamp is in the future",
                checks=checks,
                retry_after_s=backoff,
            )
        remaining = max(0.0, float(policy.recovery_cooldown_s) - elapsed)
        checks["recovery_cooldown_elapsed"] = remaining <= 0.0
        if remaining > 0.0:
            return _decision(
                item,
                OrchestrationDecisionKind.WAIT_COOLDOWN,
                "recovery cooldown has not elapsed",
                checks=checks,
                retry_after_s=min(remaining, max(0.1, policy.max_backoff_s)),
            )
    else:
        checks["recovery_cooldown_elapsed"] = True

    previous = item.previous_reconciliation
    current = item.current_reconciliation
    checks["previous_reconciliation_present"] = previous is not None
    checks["current_reconciliation_present"] = current is not None
    if previous is None or current is None:
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "two semantic reconciliation samples are required before recovery",
            checks=checks,
            retry_after_s=backoff,
        )
    checks["distinct_reconciliation_samples"] = previous.reconcile_id != current.reconcile_id
    checks["previous_reconciliation_fresh"] = _age_is_fresh(
        previous.created_at,
        now=now,
        max_age_s=policy.max_reconciliation_age_s,
    )
    checks["current_reconciliation_fresh"] = _age_is_fresh(
        current.created_at,
        now=now,
        max_age_s=policy.max_reconciliation_age_s,
    )
    separation = current.created_at - previous.created_at
    checks["reconciliation_separation_sufficient"] = (
        separation >= max(0.0, float(policy.min_reconciliation_separation_s))
    )
    checks["reconciliation_fence_stable"] = fence_matches(previous, current)
    checks["reconciliation_task_matches"] = (
        previous.task_id == task.task_id and current.task_id == task.task_id
    )
    checks["reconciliation_worker_matches"] = (
        current.current_worker_id == task.current_worker_id
        and previous.current_worker_id == task.current_worker_id
    )
    checks["reconciliation_state_recovery_eligible"] = (
        previous.state in {state.value for state in _RECOVERY_STATES}
        and current.state in {state.value for state in _RECOVERY_STATES}
        and previous.requires_reconcile
        and current.requires_reconcile
    )
    if not all(
        checks[name]
        for name in (
            "distinct_reconciliation_samples",
            "previous_reconciliation_fresh",
            "current_reconciliation_fresh",
            "reconciliation_separation_sufficient",
            "reconciliation_fence_stable",
            "reconciliation_task_matches",
            "reconciliation_worker_matches",
            "reconciliation_state_recovery_eligible",
        )
    ):
        return _decision(
            item,
            OrchestrationDecisionKind.RECONCILE,
            "semantic reconciliation is not yet stable and fresh across two samples",
            checks=checks,
            retry_after_s=backoff,
        )

    if item.requires_exact_window_binding:
        binding = item.window_binding
        checks["window_binding_present"] = binding is not None
        checks["window_binding_current_worker"] = bool(binding and binding.worker_id == worker.worker_id)
        checks["window_binding_fresh"] = bool(binding and binding.is_fresh(now=now))
        checks["window_binding_url_matches"] = bool(
            binding and binding.conversation_url == worker.conversation_url
        )
        if not all(
            checks[name]
            for name in (
                "window_binding_present",
                "window_binding_current_worker",
                "window_binding_fresh",
                "window_binding_url_matches",
            )
        ):
            return _decision(
                item,
                OrchestrationDecisionKind.RECONCILE,
                "exact-window binding is missing or stale for the current worker",
                checks=checks,
                retry_after_s=backoff,
            )

    if item.page_continuity_relevant:
        checks["page_capability_present"] = (
            item.page_capability is not None and item.capability_context is not None
        )
        if not checks["page_capability_present"]:
            return _decision(
                item,
                OrchestrationDecisionKind.BLOCKED_HUMAN,
                "page-continuity behavior lacks durable capability provenance",
                checks=checks,
                blockers=["a dedicated accepted page-close/reopen experiment is required"],
            )
        assert item.page_capability is not None
        assert item.capability_context is not None
        capability_ok, capability_blockers = capability_matches_context(
            item.page_capability,
            item.capability_context,
            expected_kind=item.page_capability_kind,
            now=now,
        )
        checks["page_capability_matches_runtime"] = capability_ok
        if not capability_ok:
            return _decision(
                item,
                OrchestrationDecisionKind.BLOCKED_HUMAN,
                "page-continuity capability provenance is stale or mismatched",
                checks=checks,
                blockers=[f"page capability check failed: {name}" for name in capability_blockers],
            )

    return _decision(
        item,
        OrchestrationDecisionKind.RECOMMEND_ACTION,
        "all deterministic recovery gates are satisfied; surface an explicit action recommendation only",
        checks=checks,
    )


def _fairness_key(item: TaskOrchestrationInput) -> tuple[float, int, str]:
    fairness_stamp = (
        float(item.last_scheduled_at)
        if item.last_scheduled_at is not None
        else float(item.task.created_at)
    )
    return (
        fairness_stamp,
        _FAIRNESS_STATE_RANK.get(item.task.state, 99),
        item.task.task_id,
    )


def plan_orchestration(
    items: list[TaskOrchestrationInput],
    *,
    now: float | None = None,
    policy: OrchestrationPolicy | None = None,
) -> list[OrchestrationDecision]:
    """Evaluate a batch and fairly select bounded work without enabling mutation.

    The returned `selected` bit is advisory scheduler ownership for this cycle. Even selected
    `recommend-action` decisions keep `mutation_allowed=False`; an integrator must route any
    future explicit action through the existing reconciliation/action fences.
    """

    now = time.time() if now is None else float(now)
    policy = policy or OrchestrationPolicy()
    unique_items: dict[str, TaskOrchestrationInput] = {}
    duplicate_ids: set[str] = set()
    for item in items:
        task_id = item.task.task_id
        if task_id in unique_items:
            duplicate_ids.add(task_id)
            continue
        unique_items[task_id] = item
    canonical_items = [unique_items[task_id] for task_id in sorted(unique_items)]
    decisions = [evaluate_task(item, now=now, policy=policy) for item in canonical_items]
    by_task = {item.task.task_id: item for item in canonical_items}
    decisions = [
        replace(
            decision,
            kind=OrchestrationDecisionKind.RECONCILE,
            reason="duplicate orchestration inputs exist for one durable task; reconcile caller state",
            selected=False,
            blockers=[*decision.blockers, "duplicate durable task input"],
            mutation_allowed=False,
        )
        if decision.task_id in duplicate_ids
        else decision
        for decision in decisions
    ]
    actionable_kinds = {
        OrchestrationDecisionKind.RECONCILE,
        OrchestrationDecisionKind.RECOMMEND_ACTION,
    }
    unknown_schedule_ids = {
        decision.task_id
        for decision in decisions
        if decision.kind in actionable_kinds
        and not by_task[decision.task_id].scheduling_history_known
    }
    actionable = [
        decision
        for decision in decisions
        if decision.kind in actionable_kinds and decision.task_id not in unknown_schedule_ids
    ]
    actionable.sort(key=lambda decision: _fairness_key(by_task[decision.task_id]))
    selected_ids = {
        decision.task_id for decision in actionable[: max(0, int(policy.max_selected_tasks))]
    }

    result: list[OrchestrationDecision] = []
    for decision in decisions:
        if decision.task_id in unknown_schedule_ids:
            result.append(
                replace(
                    decision,
                    kind=OrchestrationDecisionKind.WAIT_COOLDOWN,
                    reason=(
                        "scheduler selection withheld because durable scheduling history is "
                        "unavailable; supply it explicitly to the adapter"
                    ),
                    selected=False,
                    retry_after_s=max(0.1, float(policy.max_backoff_s)),
                    deferred_kind=decision.kind,
                    blockers=[*decision.blockers, "durable scheduling history unavailable"],
                )
            )
            continue
        if decision.task_id in selected_ids:
            result.append(replace(decision, selected=True))
            continue
        if decision in actionable:
            item = by_task[decision.task_id]
            result.append(
                replace(
                    decision,
                    kind=OrchestrationDecisionKind.WAIT_COOLDOWN,
                    reason=(
                        f"fairness slot deferred {decision.kind.value}; another recovery task "
                        "has older scheduler service"
                    ),
                    selected=False,
                    retry_after_s=_bounded_backoff(policy, item.consecutive_waits),
                    deferred_kind=decision.kind,
                )
            )
            continue
        result.append(decision)
    return result
