from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .action_runtime import SubmitOutcome, require_no_unresolved_action, submit_armed_action
from .actions import ActionAttemptState, ActionBlocked, build_action_attempt
from .dispatcher import DispatchAction, DispatchDisabled, DispatchPlan
from .models import RecoveryRecommendation, WorkerWindowBinding, WorkerStatus
from .registry import Registry
from .uia_actions import (
    ChromeUiaAckObserver,
    UiaAckObservation,
    acknowledgement_from_uia_observation,
)


@dataclass(slots=True)
class DispatchExecutionPolicy:
    enabled: bool = False
    confirmed_task_id: str | None = None


@dataclass(slots=True)
class DispatchExecutionResult:
    task_id: str
    attempt_id: str
    state: str
    submitted: bool
    side_effect_possible: bool
    detail: str


@dataclass(slots=True)
class ActionReconcileResult:
    attempt_id: str
    state: str
    acknowledged: bool
    detail: str
    observation: UiaAckObservation | None = None


TransportFactory = Callable[[WorkerWindowBinding], object]
AckObserverFactory = Callable[[str], ChromeUiaAckObserver]


def execute_current_worker_recovery(
    registry: Registry,
    *,
    plan: DispatchPlan,
    recommendation: RecoveryRecommendation,
    policy: DispatchExecutionPolicy,
    transport_factory: TransportFactory,
    now: float | None = None,
) -> DispatchExecutionResult:
    """Execute one explicitly enabled current-worker recovery turn.

    This function assumes no browser transport semantics beyond the supplied factory. It
    rechecks durable identity immediately before arming, then persists ARMED plus recovery
    budget atomically before any external submission is attempted.
    """
    now = time.time() if now is None else float(now)
    if not policy.enabled:
        raise DispatchDisabled("recovery execution is disabled; explicit opt-in is required")
    if policy.confirmed_task_id != plan.task_id:
        raise DispatchDisabled("explicit task confirmation does not match the dispatch plan")
    if plan.action != DispatchAction.CONTINUE_CURRENT_WORKER:
        raise ActionBlocked("only current-worker continuation is supported by the explicit executor")
    if not plan.candidate_ready or not plan.transport_enabled or not plan.would_dispatch:
        raise ActionBlocked("dispatch plan is not execution-ready")
    if not recommendation.prompt or recommendation.action != "reconcile_then_continue":
        raise ActionBlocked("recovery recommendation does not contain the canonical continuation prompt")

    task = registry.get_task(plan.task_id)
    if not task.current_worker_id:
        raise ActionBlocked("task has no current worker")
    worker = registry.get_worker(task.current_worker_id)
    if worker.status != WorkerStatus.ACTIVE:
        raise ActionBlocked("current worker is not active")
    require_no_unresolved_action(registry, task.task_id)

    current = registry.latest_reconciliation(task.task_id)
    if current is None:
        raise ActionBlocked("latest reconciliation disappeared before execution")
    if current.reconcile_id != plan.current_reconcile_id:
        raise ActionBlocked("a newer reconciliation exists; rebuild the dispatch plan")
    if current.fence_token != plan.fence_token:
        raise ActionBlocked("reconciliation fence changed before execution")

    binding = registry.get_worker_window_binding(worker.worker_id, now=now, require_fresh=True)
    if binding is None:
        raise ActionBlocked("fresh exact-window lease is required before recovery execution")
    if binding.conversation_url.rstrip("/") != worker.conversation_url.rstrip("/"):
        raise ActionBlocked("fresh window lease no longer matches the current worker URL")

    browser = registry.latest_browser_observation(worker.worker_id)
    if browser is None or not browser.message_signature:
        raise ActionBlocked("fresh browser signature is required before write-ahead arming")
    if browser.observed_at > now + 1.0:
        raise ActionBlocked("browser observation timestamp is from the future")

    # Construct the transport before ARMED. A correct factory performs no external mutation.
    transport = transport_factory(binding)
    attempt = build_action_attempt(
        plan,
        task,
        worker,
        recommendation.prompt,
        fence_version=current.fence_version,
        pre_action_signature=browser.message_signature,
        now=now,
    )
    registry.record_recovery_action_attempt(attempt)
    outcome: SubmitOutcome = submit_armed_action(
        registry,
        attempt_id=attempt.attempt_id,
        prompt=recommendation.prompt,
        transport=transport,
    )
    return DispatchExecutionResult(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        state=outcome.state,
        submitted=outcome.submitted,
        side_effect_possible=outcome.side_effect_possible,
        detail=outcome.detail,
    )


def reconcile_action_with_uia(
    registry: Registry,
    *,
    attempt_id: str,
    observer_factory: AckObserverFactory,
    now: float | None = None,
) -> ActionReconcileResult:
    """Try to positively acknowledge one unresolved action from fresh exact-window UIA evidence."""
    now = time.time() if now is None else float(now)
    attempt = registry.get_action_attempt(attempt_id)
    if attempt.state not in {
        ActionAttemptState.ARMED,
        ActionAttemptState.SUBMITTED,
        ActionAttemptState.RECONCILE_REQUIRED,
    }:
        return ActionReconcileResult(
            attempt_id=attempt.attempt_id,
            state=attempt.state.value,
            acknowledged=attempt.state == ActionAttemptState.ACKNOWLEDGED,
            detail="action attempt is already terminal",
        )

    task = registry.get_task(attempt.task_id)
    if task.current_worker_id != attempt.worker_id:
        raise ActionBlocked("action attempt no longer belongs to the task's current worker")
    worker = registry.get_worker(attempt.worker_id)
    if worker.status != WorkerStatus.ACTIVE:
        raise ActionBlocked("action acknowledgement requires an active current worker")
    binding = registry.get_worker_window_binding(worker.worker_id, now=now, require_fresh=True)
    if binding is None:
        raise ActionBlocked("fresh exact-window lease is required for action acknowledgement")

    observer = observer_factory(binding.chrome_executable)
    observation = observer.observe(
        worker_id=worker.worker_id,
        conversation_url=worker.conversation_url,
        expected_nonce=f"CWS-ACTION-{attempt.nonce}",
        expected_hwnd=binding.window_handle,
        expected_browser_pid=binding.browser_pid,
    )
    ack = acknowledgement_from_uia_observation(
        attempt,
        observation,
        min_nonce_occurrences=1,
        max_nonce_occurrences=1,
        require_generation_complete=True,
    )
    if ack is None:
        return ActionReconcileResult(
            attempt_id=attempt.attempt_id,
            state=attempt.state.value,
            acknowledged=False,
            detail="positive single-turn completion acknowledgement is not yet available",
            observation=observation,
        )
    updated = registry.acknowledge_action(ack)
    return ActionReconcileResult(
        attempt_id=updated.attempt_id,
        state=updated.state.value,
        acknowledged=True,
        detail="action acknowledged from exact-window nonce/hash evidence",
        observation=observation,
    )
