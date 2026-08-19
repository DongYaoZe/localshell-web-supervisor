from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class WorkerLeaseStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class DurableTaskStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"


class DecisionCode(StrEnum):
    REGISTERED = "registered"
    ALREADY_REGISTERED = "already_registered"
    CLAIMED = "claimed"
    HEARTBEAT_RECORDED = "heartbeat_recorded"
    HANDOFF_REQUESTED = "handoff_requested"
    TAKEOVER_GRANTED = "takeover_granted"
    WORKER_COMPLETED = "worker_completed"
    WORKER_ABANDONED = "worker_abandoned"
    TASK_COMPLETED = "task_completed"
    STALE_REVISION = "stale_revision"
    TASK_CLOSED = "task_closed"
    UNKNOWN_WORKER = "unknown_worker"
    NOT_CANDIDATE = "not_candidate"
    ACTIVE_WORKER_PRESENT = "active_worker_present"
    NO_ACTIVE_WORKER = "no_active_worker"
    FENCED_WORKER = "fenced_worker"
    STALE_GENERATION = "stale_generation"
    LEASE_EXPIRED = "lease_expired"
    CLOCK_ROLLBACK = "clock_rollback"
    INVALID_HANDOFF_TARGET = "invalid_handoff_target"
    TAKEOVER_NOT_ELIGIBLE = "takeover_not_eligible"
    ACTIVE_WORKER_MUST_END = "active_worker_must_end"
    COMPLETION_REF_REQUIRED = "completion_ref_required"


class EventKind(StrEnum):
    WORKER_REGISTERED = "worker_registered"
    LEASE_CLAIMED = "lease_claimed"
    HEARTBEAT_ACCEPTED = "heartbeat_accepted"
    HANDOFF_REQUESTED = "handoff_requested"
    WORKER_SUPERSEDED = "worker_superseded"
    LEASE_TAKEN_OVER = "lease_taken_over"
    WORKER_COMPLETED = "worker_completed"
    WORKER_ABANDONED = "worker_abandoned"
    TASK_COMPLETED = "task_completed"


class TakeoverReason(StrEnum):
    HANDOFF = "handoff"
    LEASE_EXPIRED = "lease_expired"
    ACTIVE_LEASE_FRESH = "active_lease_fresh"
    NO_ACTIVE_WORKER = "no_active_worker"
    UNKNOWN_CANDIDATE = "unknown_candidate"
    NOT_CANDIDATE = "not_candidate"
    TASK_CLOSED = "task_closed"


class ProtocolInvariantError(RuntimeError):
    """Raised for corrupt/impossible persisted protocol snapshots.

    Operational conflicts are returned as rejected decisions. An invariant error means a
    persistence adapter must fail closed and reconcile rather than inventing a transition.
    """


@dataclass(frozen=True, slots=True)
class TaskLineage:
    task_id: str
    parent_task_id: str | None
    root_task_id: str
    child_key: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerLease:
    worker_id: str
    task_id: str
    conversation_ref: str | None
    status: WorkerLeaseStatus
    registered_at: float
    generation: int | None = None
    claimed_at: float | None = None
    last_heartbeat_at: float | None = None
    lease_expires_at: float | None = None
    ended_at: float | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerTaskState:
    lineage: TaskLineage
    revision: int = 0
    generation: int = 0
    task_status: DurableTaskStatus = DurableTaskStatus.OPEN
    current_worker_id: str | None = None
    handoff_target_worker_id: str | None = None
    handoff_requested_at: float | None = None
    workers: tuple[WorkerLease, ...] = ()
    completed_at: float | None = None
    completion_ref: str | None = None

    @property
    def task_id(self) -> str:
        return self.lineage.task_id


@dataclass(frozen=True, slots=True)
class ProtocolEvent:
    kind: EventKind
    task_id: str
    revision: int
    at: float
    worker_id: str | None = None
    generation: int | None = None
    related_worker_id: str | None = None
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class ProtocolDecision:
    accepted: bool
    code: DecisionCode
    expected_revision: int
    state: WorkerTaskState
    events: tuple[ProtocolEvent, ...] = ()

    @property
    def mutated(self) -> bool:
        return self.accepted and self.state.revision == self.expected_revision + 1


@dataclass(frozen=True, slots=True)
class TakeoverEligibility:
    eligible: bool
    reason: TakeoverReason
    active_worker_id: str | None
    generation: int


def new_task_state(
    task_id: str,
    *,
    parent_task_id: str | None = None,
    root_task_id: str | None = None,
    child_key: str | None = None,
) -> WorkerTaskState:
    """Create a protocol snapshot without binding durable task identity to a browser tab.

    A root task derives its root identity from ``task_id``. Child tasks must receive the
    durable root id explicitly from their parent snapshot so arbitrary-depth trees do not
    silently collapse to one level.
    """

    task_id = task_id.strip()
    if not task_id:
        raise ValueError("task_id must be non-empty")
    if parent_task_id is None:
        if root_task_id not in (None, task_id):
            raise ValueError("a root task's root_task_id must equal task_id")
        root = task_id
    else:
        parent_task_id = parent_task_id.strip()
        if not parent_task_id:
            raise ValueError("parent_task_id must be non-empty when supplied")
        if not root_task_id or not root_task_id.strip():
            raise ValueError("child tasks require an explicit root_task_id")
        root = root_task_id.strip()
    state = WorkerTaskState(
        lineage=TaskLineage(
            task_id=task_id,
            parent_task_id=parent_task_id,
            root_task_id=root,
            child_key=child_key,
        )
    )
    validate_state(state)
    return state


def worker_by_id(state: WorkerTaskState, worker_id: str) -> WorkerLease | None:
    return next((worker for worker in state.workers if worker.worker_id == worker_id), None)


def lease_is_fresh(state: WorkerTaskState, worker_id: str, *, now: float) -> bool:
    worker = worker_by_id(state, worker_id)
    return bool(
        worker
        and state.task_status == DurableTaskStatus.OPEN
        and state.current_worker_id == worker_id
        and worker.status == WorkerLeaseStatus.ACTIVE
        and worker.generation == state.generation
        and worker.lease_expires_at is not None
        and float(now) < worker.lease_expires_at
    )


def takeover_eligibility(
    state: WorkerTaskState,
    candidate_worker_id: str,
    *,
    now: float,
) -> TakeoverEligibility:
    validate_state(state)
    if state.task_status != DurableTaskStatus.OPEN:
        return TakeoverEligibility(
            False,
            TakeoverReason.TASK_CLOSED,
            state.current_worker_id,
            state.generation,
        )
    candidate = worker_by_id(state, candidate_worker_id)
    if candidate is None:
        return TakeoverEligibility(
            False, TakeoverReason.UNKNOWN_CANDIDATE, state.current_worker_id, state.generation
        )
    if candidate.status != WorkerLeaseStatus.CANDIDATE:
        return TakeoverEligibility(
            False,
            TakeoverReason.NOT_CANDIDATE,
            state.current_worker_id,
            state.generation,
        )
    if state.current_worker_id is None:
        return TakeoverEligibility(False, TakeoverReason.NO_ACTIVE_WORKER, None, state.generation)
    if state.handoff_target_worker_id == candidate_worker_id:
        return TakeoverEligibility(
            True,
            TakeoverReason.HANDOFF,
            state.current_worker_id,
            state.generation,
        )
    if not lease_is_fresh(state, state.current_worker_id, now=now):
        return TakeoverEligibility(
            True, TakeoverReason.LEASE_EXPIRED, state.current_worker_id, state.generation
        )
    return TakeoverEligibility(
        False, TakeoverReason.ACTIVE_LEASE_FRESH, state.current_worker_id, state.generation
    )


def register_worker(
    state: WorkerTaskState,
    worker_id: str,
    *,
    conversation_ref: str | None,
    now: float,
    expected_revision: int,
) -> ProtocolDecision:
    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    if state.task_status != DurableTaskStatus.OPEN:
        return _reject(state, DecisionCode.TASK_CLOSED)
    worker_id = worker_id.strip()
    if not worker_id:
        raise ValueError("worker_id must be non-empty")
    existing = worker_by_id(state, worker_id)
    if existing is not None:
        return ProtocolDecision(True, DecisionCode.ALREADY_REGISTERED, state.revision, state)
    worker = WorkerLease(
        worker_id=worker_id,
        task_id=state.task_id,
        conversation_ref=conversation_ref,
        status=WorkerLeaseStatus.CANDIDATE,
        registered_at=float(now),
    )
    next_state = replace(state, workers=_sorted_workers((*state.workers, worker)))
    return _accept(
        state,
        next_state,
        DecisionCode.REGISTERED,
        ProtocolEvent(
            EventKind.WORKER_REGISTERED,
            state.task_id,
            0,
            float(now),
            worker_id=worker_id,
        ),
    )


def claim_worker(
    state: WorkerTaskState,
    worker_id: str,
    *,
    now: float,
    lease_seconds: float,
    expected_revision: int,
) -> ProtocolDecision:
    _require_positive_lease(lease_seconds)
    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    if state.task_status != DurableTaskStatus.OPEN:
        return _reject(state, DecisionCode.TASK_CLOSED)
    worker = worker_by_id(state, worker_id)
    if worker is None:
        return _reject(state, DecisionCode.UNKNOWN_WORKER)
    if worker.status != WorkerLeaseStatus.CANDIDATE:
        return _reject(state, DecisionCode.NOT_CANDIDATE)
    if state.current_worker_id is not None:
        return _reject(state, DecisionCode.ACTIVE_WORKER_PRESENT)
    generation = state.generation + 1
    claimed = replace(
        worker,
        status=WorkerLeaseStatus.ACTIVE,
        generation=generation,
        claimed_at=float(now),
        last_heartbeat_at=float(now),
        lease_expires_at=float(now) + float(lease_seconds),
    )
    next_state = replace(
        state,
        generation=generation,
        current_worker_id=worker_id,
        handoff_target_worker_id=None,
        handoff_requested_at=None,
        workers=_replace_worker(state.workers, claimed),
    )
    return _accept(
        state,
        next_state,
        DecisionCode.CLAIMED,
        ProtocolEvent(
            EventKind.LEASE_CLAIMED,
            state.task_id,
            0,
            float(now),
            worker_id=worker_id,
            generation=generation,
        ),
    )


def heartbeat_worker(
    state: WorkerTaskState,
    worker_id: str,
    *,
    generation: int,
    now: float,
    lease_seconds: float,
    expected_revision: int,
) -> ProtocolDecision:
    _require_positive_lease(lease_seconds)
    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    authority_rejection = _authority_rejection(state, worker_id, generation=generation, now=now)
    if authority_rejection:
        return authority_rejection
    worker = worker_by_id(state, worker_id)
    assert worker is not None
    updated = replace(
        worker,
        last_heartbeat_at=float(now),
        lease_expires_at=float(now) + float(lease_seconds),
    )
    next_state = replace(state, workers=_replace_worker(state.workers, updated))
    return _accept(
        state,
        next_state,
        DecisionCode.HEARTBEAT_RECORDED,
        ProtocolEvent(
            EventKind.HEARTBEAT_ACCEPTED,
            state.task_id,
            0,
            float(now),
            worker_id=worker_id,
            generation=generation,
        ),
    )


def request_handoff(
    state: WorkerTaskState,
    worker_id: str,
    target_worker_id: str,
    *,
    generation: int,
    now: float,
    expected_revision: int,
) -> ProtocolDecision:
    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    authority_rejection = _authority_rejection(state, worker_id, generation=generation, now=now)
    if authority_rejection:
        return authority_rejection
    target = worker_by_id(state, target_worker_id)
    if target is None or target.status != WorkerLeaseStatus.CANDIDATE:
        return _reject(state, DecisionCode.INVALID_HANDOFF_TARGET)
    next_state = replace(
        state,
        handoff_target_worker_id=target_worker_id,
        handoff_requested_at=float(now),
    )
    return _accept(
        state,
        next_state,
        DecisionCode.HANDOFF_REQUESTED,
        ProtocolEvent(
            EventKind.HANDOFF_REQUESTED,
            state.task_id,
            0,
            float(now),
            worker_id=worker_id,
            generation=generation,
            related_worker_id=target_worker_id,
        ),
    )


def takeover_worker(
    state: WorkerTaskState,
    candidate_worker_id: str,
    *,
    now: float,
    lease_seconds: float,
    expected_revision: int,
) -> ProtocolDecision:
    _require_positive_lease(lease_seconds)
    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    eligibility = takeover_eligibility(state, candidate_worker_id, now=now)
    if not eligibility.eligible:
        if eligibility.reason == TakeoverReason.TASK_CLOSED:
            return _reject(state, DecisionCode.TASK_CLOSED)
        if eligibility.reason == TakeoverReason.UNKNOWN_CANDIDATE:
            return _reject(state, DecisionCode.UNKNOWN_WORKER)
        if eligibility.reason == TakeoverReason.NOT_CANDIDATE:
            return _reject(state, DecisionCode.NOT_CANDIDATE)
        if eligibility.reason == TakeoverReason.NO_ACTIVE_WORKER:
            return _reject(state, DecisionCode.NO_ACTIVE_WORKER)
        return _reject(state, DecisionCode.TAKEOVER_NOT_ELIGIBLE)
    old_worker_id = state.current_worker_id
    assert old_worker_id is not None
    old_worker = worker_by_id(state, old_worker_id)
    candidate = worker_by_id(state, candidate_worker_id)
    assert old_worker is not None and candidate is not None
    generation = state.generation + 1
    superseded = replace(
        old_worker,
        status=WorkerLeaseStatus.SUPERSEDED,
        ended_at=float(now),
        superseded_by=candidate_worker_id,
    )
    claimed = replace(
        candidate,
        status=WorkerLeaseStatus.ACTIVE,
        generation=generation,
        claimed_at=float(now),
        last_heartbeat_at=float(now),
        lease_expires_at=float(now) + float(lease_seconds),
    )
    workers = _replace_worker(state.workers, superseded)
    workers = _replace_worker(workers, claimed)
    next_state = replace(
        state,
        generation=generation,
        current_worker_id=candidate_worker_id,
        handoff_target_worker_id=None,
        handoff_requested_at=None,
        workers=workers,
    )
    return _accept(
        state,
        next_state,
        DecisionCode.TAKEOVER_GRANTED,
        ProtocolEvent(
            EventKind.WORKER_SUPERSEDED,
            state.task_id,
            0,
            float(now),
            worker_id=old_worker_id,
            generation=old_worker.generation,
            related_worker_id=candidate_worker_id,
        ),
        ProtocolEvent(
            EventKind.LEASE_TAKEN_OVER,
            state.task_id,
            0,
            float(now),
            worker_id=candidate_worker_id,
            generation=generation,
            related_worker_id=old_worker_id,
            ref=eligibility.reason.value,
        ),
    )


def complete_worker(
    state: WorkerTaskState,
    worker_id: str,
    *,
    generation: int,
    now: float,
    expected_revision: int,
) -> ProtocolDecision:
    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    authority_rejection = _authority_rejection(state, worker_id, generation=generation, now=now)
    if authority_rejection:
        return authority_rejection
    worker = worker_by_id(state, worker_id)
    assert worker is not None
    completed = replace(
        worker,
        status=WorkerLeaseStatus.COMPLETED,
        ended_at=float(now),
        lease_expires_at=float(now),
    )
    next_state = replace(
        state,
        current_worker_id=None,
        handoff_target_worker_id=None,
        handoff_requested_at=None,
        workers=_replace_worker(state.workers, completed),
    )
    return _accept(
        state,
        next_state,
        DecisionCode.WORKER_COMPLETED,
        ProtocolEvent(
            EventKind.WORKER_COMPLETED,
            state.task_id,
            0,
            float(now),
            worker_id=worker_id,
            generation=generation,
        ),
    )


def abandon_worker(
    state: WorkerTaskState,
    worker_id: str,
    *,
    generation: int,
    now: float,
    expected_revision: int,
) -> ProtocolDecision:
    """Fence a disappeared current conversation while leaving the durable task recoverable."""

    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    if state.task_status != DurableTaskStatus.OPEN:
        return _reject(state, DecisionCode.TASK_CLOSED)
    worker = worker_by_id(state, worker_id)
    if worker is None:
        return _reject(state, DecisionCode.UNKNOWN_WORKER)
    if state.current_worker_id != worker_id or worker.status != WorkerLeaseStatus.ACTIVE:
        return _reject(state, DecisionCode.FENCED_WORKER)
    if generation != state.generation or worker.generation != generation:
        return _reject(state, DecisionCode.STALE_GENERATION)
    abandoned = replace(
        worker,
        status=WorkerLeaseStatus.ABANDONED,
        ended_at=float(now),
        lease_expires_at=float(now),
    )
    next_state = replace(
        state,
        current_worker_id=None,
        handoff_target_worker_id=None,
        handoff_requested_at=None,
        workers=_replace_worker(state.workers, abandoned),
    )
    return _accept(
        state,
        next_state,
        DecisionCode.WORKER_ABANDONED,
        ProtocolEvent(
            EventKind.WORKER_ABANDONED,
            state.task_id,
            0,
            float(now),
            worker_id=worker_id,
            generation=generation,
        ),
    )


def complete_task(
    state: WorkerTaskState,
    *,
    completion_ref: str,
    now: float,
    expected_revision: int,
) -> ProtocolDecision:
    rejected = _preflight(state, expected_revision)
    if rejected:
        return rejected
    if state.task_status != DurableTaskStatus.OPEN:
        return _reject(state, DecisionCode.TASK_CLOSED)
    if state.current_worker_id is not None:
        return _reject(state, DecisionCode.ACTIVE_WORKER_MUST_END)
    completion_ref = completion_ref.strip()
    if not completion_ref:
        return _reject(state, DecisionCode.COMPLETION_REF_REQUIRED)
    next_state = replace(
        state,
        task_status=DurableTaskStatus.COMPLETED,
        completed_at=float(now),
        completion_ref=completion_ref,
    )
    return _accept(
        state,
        next_state,
        DecisionCode.TASK_COMPLETED,
        ProtocolEvent(
            EventKind.TASK_COMPLETED,
            state.task_id,
            0,
            float(now),
            generation=state.generation,
            ref=completion_ref,
        ),
    )


def validate_state(state: WorkerTaskState) -> None:
    if state.revision < 0 or state.generation < 0:
        raise ProtocolInvariantError("revision and generation must be non-negative")
    if state.lineage.parent_task_id is None and state.lineage.root_task_id != state.task_id:
        raise ProtocolInvariantError("root task must name itself as root_task_id")
    if len({worker.worker_id for worker in state.workers}) != len(state.workers):
        raise ProtocolInvariantError("worker ids must be unique per durable task")
    if any(worker.task_id != state.task_id for worker in state.workers):
        raise ProtocolInvariantError("every worker must be registered against this durable task")
    active = [worker for worker in state.workers if worker.status == WorkerLeaseStatus.ACTIVE]
    if state.current_worker_id is None:
        if active:
            raise ProtocolInvariantError("active worker exists without current_worker_id")
    else:
        if len(active) != 1 or active[0].worker_id != state.current_worker_id:
            raise ProtocolInvariantError(
                "current_worker_id must identify exactly one active worker"
            )
        if active[0].generation != state.generation or state.generation <= 0:
            raise ProtocolInvariantError("current worker must own the current positive generation")
        if active[0].lease_expires_at is None:
            raise ProtocolInvariantError("current worker requires a lease expiry")
    if state.handoff_target_worker_id is not None:
        if state.current_worker_id is None or state.handoff_requested_at is None:
            raise ProtocolInvariantError("handoff requires a current worker and request timestamp")
        target = worker_by_id(state, state.handoff_target_worker_id)
        if target is None or target.status != WorkerLeaseStatus.CANDIDATE:
            raise ProtocolInvariantError("handoff target must be a registered candidate")
    elif state.handoff_requested_at is not None:
        raise ProtocolInvariantError("handoff timestamp cannot exist without a target")
    if state.task_status == DurableTaskStatus.COMPLETED:
        if state.current_worker_id is not None:
            raise ProtocolInvariantError("completed durable task cannot retain an active worker")
        if state.completed_at is None or not state.completion_ref:
            raise ProtocolInvariantError("completed durable task requires completion metadata")


def _preflight(state: WorkerTaskState, expected_revision: int) -> ProtocolDecision | None:
    validate_state(state)
    if int(expected_revision) != state.revision:
        return ProtocolDecision(
            False,
            DecisionCode.STALE_REVISION,
            int(expected_revision),
            state,
        )
    return None


def _authority_rejection(
    state: WorkerTaskState,
    worker_id: str,
    *,
    generation: int,
    now: float,
) -> ProtocolDecision | None:
    if state.task_status != DurableTaskStatus.OPEN:
        return _reject(state, DecisionCode.TASK_CLOSED)
    worker = worker_by_id(state, worker_id)
    if worker is None:
        return _reject(state, DecisionCode.UNKNOWN_WORKER)
    if state.current_worker_id != worker_id or worker.status != WorkerLeaseStatus.ACTIVE:
        return _reject(state, DecisionCode.FENCED_WORKER)
    if generation != state.generation or worker.generation != generation:
        return _reject(state, DecisionCode.STALE_GENERATION)
    timeline = [
        float(value)
        for value in (worker.claimed_at, worker.last_heartbeat_at)
        if value is not None
    ]
    if timeline and float(now) < max(timeline):
        return _reject(state, DecisionCode.CLOCK_ROLLBACK)
    if not lease_is_fresh(state, worker_id, now=now):
        return _reject(state, DecisionCode.LEASE_EXPIRED)
    return None


def _accept(
    previous: WorkerTaskState,
    candidate: WorkerTaskState,
    code: DecisionCode,
    *events: ProtocolEvent,
) -> ProtocolDecision:
    revision = previous.revision + 1
    next_state = replace(candidate, revision=revision)
    stamped = tuple(replace(event, revision=revision) for event in events)
    validate_state(next_state)
    return ProtocolDecision(True, code, previous.revision, next_state, stamped)


def _reject(state: WorkerTaskState, code: DecisionCode) -> ProtocolDecision:
    return ProtocolDecision(False, code, state.revision, state)


def _replace_worker(
    workers: tuple[WorkerLease, ...], updated: WorkerLease
) -> tuple[WorkerLease, ...]:
    return _sorted_workers(
        tuple(updated if worker.worker_id == updated.worker_id else worker for worker in workers)
    )


def _sorted_workers(workers: tuple[WorkerLease, ...]) -> tuple[WorkerLease, ...]:
    return tuple(sorted(workers, key=lambda worker: worker.worker_id))


def _require_positive_lease(lease_seconds: float) -> None:
    if float(lease_seconds) <= 0:
        raise ValueError("lease_seconds must be positive")
