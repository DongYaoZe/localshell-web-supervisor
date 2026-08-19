from __future__ import annotations

from dataclasses import dataclass

from .actions import (
    ActionBlocked,
    ActionIntent,
    ActionTransport,
    ActionTransportDisabled,
    TransportSubmission,
    intent_from_attempt,
)
from .registry import Registry


@dataclass(slots=True)
class SubmitOutcome:
    attempt_id: str
    state: str
    submitted: bool
    side_effect_possible: bool
    transport_name: str | None
    detail: str


def submit_armed_action(
    registry: Registry,
    *,
    attempt_id: str,
    prompt: str,
    transport: ActionTransport,
) -> SubmitOutcome:
    """Submit one already-armed action with crash-conservative state transitions.

    The durable ARMED row must exist before this function is called. Any exception whose
    side-effect status cannot be proven is converted to RECONCILE_REQUIRED rather than
    allowing an automatic retry.
    """
    attempt = registry.get_action_attempt(attempt_id)
    intent: ActionIntent = intent_from_attempt(attempt, prompt)

    try:
        result: TransportSubmission = transport.submit(intent)
    except ActionTransportDisabled:
        # DisabledActionTransport fails before any external side effect. Preserve ARMED so
        # the operator can inspect/cancel the dry-run intent without laundering it as sent.
        raise
    except BaseException as exc:
        updated = registry.mark_action_reconcile_required(
            attempt_id,
            error=f"transport raised {type(exc).__name__}: {exc}",
            transport_name=getattr(transport, "name", type(transport).__name__),
        )
        return SubmitOutcome(
            attempt_id=attempt_id,
            state=updated.state.value,
            submitted=False,
            side_effect_possible=True,
            transport_name=updated.transport_name,
            detail=updated.last_error or "transport outcome ambiguous",
        )

    if result.submitted:
        updated = registry.mark_action_submitted(
            attempt_id,
            transport_name=result.transport_name,
        )
        return SubmitOutcome(
            attempt_id=attempt_id,
            state=updated.state.value,
            submitted=True,
            side_effect_possible=True,
            transport_name=result.transport_name,
            detail=result.detail,
        )

    if result.side_effect_possible:
        updated = registry.mark_action_reconcile_required(
            attempt_id,
            error=result.detail or "transport could not prove whether the side effect occurred",
            transport_name=result.transport_name,
        )
        return SubmitOutcome(
            attempt_id=attempt_id,
            state=updated.state.value,
            submitted=False,
            side_effect_possible=True,
            transport_name=result.transport_name,
            detail=result.detail,
        )

    updated = registry.fail_action_attempt(
        attempt_id,
        error=result.detail or "transport proved no side effect occurred",
        transport_name=result.transport_name,
    )
    return SubmitOutcome(
        attempt_id=attempt_id,
        state=updated.state.value,
        submitted=False,
        side_effect_possible=False,
        transport_name=result.transport_name,
        detail=result.detail,
    )


def require_no_unresolved_action(registry: Registry, task_id: str) -> None:
    unresolved = registry.unresolved_action_attempt(task_id)
    if unresolved is not None:
        raise ActionBlocked(
            f"task {task_id} has unresolved action {unresolved.attempt_id} "
            f"in state {unresolved.state.value}; reconcile or cancel it before any new send"
        )
