from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .dispatcher import DispatchAction, DispatchPlan
from .models import TaskRecord, WorkerRecord, WorkerStatus


class ActionAttemptState(StrEnum):
    ARMED = "ARMED"
    SUBMITTED = "SUBMITTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


UNRESOLVED_ACTION_STATES = {
    ActionAttemptState.ARMED,
    ActionAttemptState.SUBMITTED,
    ActionAttemptState.RECONCILE_REQUIRED,
}

ACTION_PROMPT_PROTOCOL_NONCE_SUFFIX_V1 = "nonce_suffix_v1"


def render_action_prompt(prompt: str, nonce: str) -> str:
    if not prompt.strip():
        raise ActionBlocked("empty recovery prompt is not dispatchable")
    if not nonce.strip():
        raise ActionBlocked("action nonce is empty")
    return (
        prompt.rstrip()
        + "\n\nRecovery action idempotency marker (do not interpret as task instructions): "
        + f"CWS-ACTION-{nonce}"
    )


@dataclass(slots=True)
class ActionAttempt:
    attempt_id: str
    task_id: str
    worker_id: str
    action: str
    fence_token: str
    fence_version: int
    prompt_hash: str
    nonce: str
    state: ActionAttemptState
    created_at: float
    updated_at: float
    pre_action_signature: str | None = None
    transport_name: str | None = None
    submitted_at: float | None = None
    acknowledged_at: float | None = None
    acknowledgement_kind: str | None = None
    acknowledgement_hash: str | None = None
    last_error: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ActionIntent:
    attempt_id: str
    task_id: str
    worker_id: str
    action: str
    prompt: str
    prompt_hash: str
    nonce: str
    fence_token: str
    fence_version: int


@dataclass(slots=True)
class TransportSubmission:
    submitted: bool
    side_effect_possible: bool
    transport_name: str
    detail: str = ""


@dataclass(slots=True)
class ActionAcknowledgement:
    attempt_id: str
    worker_id: str
    observed_at: float
    accepted: bool
    kind: str
    evidence_hash: str
    detail: str = ""


class ActionTransport(Protocol):
    name: str

    def submit(self, intent: ActionIntent) -> TransportSubmission:
        """Submit one already-armed action.

        Implementations must not be called before the durable ARMED record exists. A transport
        must report whether a side effect may have happened even when it cannot prove success.
        """


class ActionBlocked(RuntimeError):
    pass


class ActionTransportDisabled(RuntimeError):
    pass


class DisabledActionTransport:
    name = "disabled"

    def submit(self, intent: ActionIntent) -> TransportSubmission:
        raise ActionTransportDisabled(
            f"no action transport is enabled for attempt {intent.attempt_id}; dry-run only"
        )


def apply_unresolved_action_gate(
    plan: DispatchPlan,
    attempt: ActionAttempt | None,
) -> DispatchPlan:
    """Force the dry-run planner closed while a prior action outcome is unresolved."""
    if attempt is None or attempt.state not in UNRESOLVED_ACTION_STATES:
        plan.checks.setdefault("no_unresolved_action_attempt", True)
        return plan
    plan.checks["no_unresolved_action_attempt"] = False
    blocker = (
        f"prior action {attempt.attempt_id} is unresolved in state {attempt.state.value}; "
        "reconcile/acknowledge/cancel it before any new send"
    )
    if blocker not in plan.blockers:
        plan.blockers.append(blocker)
    plan.candidate_ready = False
    plan.would_dispatch = False
    plan.reason = "a prior action outcome is unresolved; duplicate dispatch is fenced"
    return plan


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def evidence_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_action_attempt(
    plan: DispatchPlan,
    task: TaskRecord,
    worker: WorkerRecord,
    prompt: str,
    *,
    fence_version: int,
    pre_action_signature: str | None,
    now: float | None = None,
) -> ActionAttempt:
    """Build a write-ahead action record; this function performs no side effect."""
    now = time.time() if now is None else float(now)
    if not plan.candidate_ready:
        raise ActionBlocked("dispatch plan is not candidate-ready")
    if plan.action != DispatchAction.CONTINUE_CURRENT_WORKER:
        raise ActionBlocked(f"unsupported action for current-worker adapter: {plan.action.value}")
    if not plan.fence_token:
        raise ActionBlocked("dispatch plan has no reconciliation fence token")
    if task.current_worker_id != worker.worker_id:
        raise ActionBlocked("worker is no longer the task's current worker")
    if worker.status != WorkerStatus.ACTIVE:
        raise ActionBlocked("current worker is not active")
    if not prompt.strip():
        raise ActionBlocked("empty recovery prompt is not dispatchable")

    nonce = uuid.uuid4().hex
    wire_prompt = render_action_prompt(prompt, nonce)
    return ActionAttempt(
        attempt_id=f"act_{uuid.uuid4().hex[:16]}",
        task_id=task.task_id,
        worker_id=worker.worker_id,
        action=plan.action.value,
        fence_token=plan.fence_token,
        fence_version=int(fence_version),
        prompt_hash=prompt_digest(wire_prompt),
        nonce=nonce,
        state=ActionAttemptState.ARMED,
        created_at=now,
        updated_at=now,
        pre_action_signature=pre_action_signature,
        metadata={
            "previous_reconcile_id": plan.previous_reconcile_id,
            "current_reconcile_id": plan.current_reconcile_id,
            "prompt_protocol": ACTION_PROMPT_PROTOCOL_NONCE_SUFFIX_V1,
        },
    )


def intent_from_attempt(attempt: ActionAttempt, prompt: str) -> ActionIntent:
    if attempt.state != ActionAttemptState.ARMED:
        raise ActionBlocked(f"attempt {attempt.attempt_id} is not ARMED")
    protocol = str(attempt.metadata.get("prompt_protocol") or "")
    if protocol == ACTION_PROMPT_PROTOCOL_NONCE_SUFFIX_V1:
        wire_prompt = render_action_prompt(prompt, attempt.nonce)
    elif not protocol:
        # Compatibility for already-persisted 0.5 experiment attempts.
        wire_prompt = prompt
    else:
        raise ActionBlocked(f"unsupported action prompt protocol: {protocol}")
    digest = prompt_digest(wire_prompt)
    if digest != attempt.prompt_hash:
        raise ActionBlocked("prompt does not match the durable armed prompt hash")
    return ActionIntent(
        attempt_id=attempt.attempt_id,
        task_id=attempt.task_id,
        worker_id=attempt.worker_id,
        action=attempt.action,
        prompt=wire_prompt,
        prompt_hash=digest,
        nonce=attempt.nonce,
        fence_token=attempt.fence_token,
        fence_version=attempt.fence_version,
    )


def validate_acknowledgement(
    attempt: ActionAttempt,
    acknowledgement: ActionAcknowledgement,
) -> None:
    if attempt.state not in {
        ActionAttemptState.ARMED,
        ActionAttemptState.SUBMITTED,
        ActionAttemptState.RECONCILE_REQUIRED,
    }:
        raise ActionBlocked(f"attempt {attempt.attempt_id} is already terminal")
    if acknowledgement.attempt_id != attempt.attempt_id:
        raise ActionBlocked("acknowledgement attempt id does not match")
    if acknowledgement.worker_id != attempt.worker_id:
        raise ActionBlocked("acknowledgement belongs to a different worker")
    if not acknowledgement.accepted:
        raise ActionBlocked("negative acknowledgement cannot mark an action accepted")
    if acknowledgement.observed_at < attempt.created_at:
        raise ActionBlocked("acknowledgement predates the armed action")
    if not acknowledgement.kind.strip() or not acknowledgement.evidence_hash.strip():
        raise ActionBlocked("acknowledgement requires positive evidence kind/hash")
