from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum

from .models import (
    ProbeMutationKind,
    ProbeMutationOperation,
    ProbeMutationState,
    ProbeWindowSlotBinding,
)


UNRESOLVED_PROBE_MUTATION_STATES = frozenset(
    {
        ProbeMutationState.ARMED,
        ProbeMutationState.CLOSE_SUBMITTED,
        ProbeMutationState.READY_TO_OPEN,
        ProbeMutationState.OPEN_SUBMITTED,
        ProbeMutationState.RECONCILE_REQUIRED,
    }
)


class ProbeReconcileOutcome(StrEnum):
    EXACT_TARGET_ABSENT = "EXACT_TARGET_ABSENT"
    EXACT_UNIQUE_OWNED_TARGET_PRESENT = "EXACT_UNIQUE_OWNED_TARGET_PRESENT"
    OLD_TARGET_STILL_PRESENT = "OLD_TARGET_STILL_PRESENT"
    BOTH_OLD_AND_NEW_PRESENT = "BOTH_OLD_AND_NEW_PRESENT"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    STALE_OR_CHANGED_IDENTITY = "STALE_OR_CHANGED_IDENTITY"
    UNKNOWN_OBSERVATION = "UNKNOWN_OBSERVATION"


@dataclass(frozen=True, slots=True)
class ProbeWindowMatch:
    window_handle: int
    browser_pid: int
    chrome_executable: str
    actual_url: str


@dataclass(slots=True)
class ProbeMutationObservation:
    observed_at: float
    old_matches: list[ProbeWindowMatch] = field(default_factory=list)
    new_matches: list[ProbeWindowMatch] = field(default_factory=list)
    complete: bool = True
    error: str | None = None


@dataclass(slots=True)
class ProbeReconcileDecision:
    outcome: ProbeReconcileOutcome
    next_state: ProbeMutationState
    reason: str
    may_close: bool = False
    may_open: bool = False
    adopt_binding: ProbeWindowSlotBinding | None = None


def snapshot_probe_slot(slot: ProbeWindowSlotBinding | None) -> dict[str, object] | None:
    if slot is None:
        return None
    return asdict(slot)


def slot_from_snapshot(snapshot: dict[str, object] | None) -> ProbeWindowSlotBinding | None:
    if snapshot is None:
        return None
    return ProbeWindowSlotBinding(
        slot_id=str(snapshot["slot_id"]),
        owner_token=str(snapshot["owner_token"]),
        target_worker_id=str(snapshot["target_worker_id"]),
        target_conversation_url=str(snapshot["target_conversation_url"]),
        actual_url=str(snapshot["actual_url"]),
        window_handle=int(snapshot["window_handle"]),
        browser_pid=int(snapshot["browser_pid"]),
        chrome_executable=str(snapshot["chrome_executable"]),
        source=str(snapshot["source"]),
        bound_at=float(snapshot["bound_at"]),
        observed_at=float(snapshot["observed_at"]),
        expires_at=float(snapshot["expires_at"]),
    )


def _effective_state(operation: ProbeMutationOperation) -> ProbeMutationState:
    if operation.state != ProbeMutationState.RECONCILE_REQUIRED:
        return operation.state
    if not operation.resume_state:
        return operation.state
    try:
        return ProbeMutationState(operation.resume_state)
    except ValueError:
        return operation.state


def _match_is_exact_new(operation: ProbeMutationOperation, match: ProbeWindowMatch) -> bool:
    return (
        match.window_handle > 0
        and match.browser_pid > 0
        and match.actual_url.rstrip("/") == operation.expected_actual_url.rstrip("/")
        and match.chrome_executable.casefold() == operation.expected_chrome_executable.casefold()
    )


def _match_is_exact_old(operation: ProbeMutationOperation, match: ProbeWindowMatch) -> bool:
    prior = slot_from_snapshot(operation.prior_slot)
    if prior is None:
        return False
    return (
        match.window_handle == prior.window_handle
        and match.browser_pid == prior.browser_pid
        and match.actual_url.rstrip("/") == prior.actual_url.rstrip("/")
        and match.chrome_executable.casefold() == prior.chrome_executable.casefold()
    )


def _binding_from_new_match(
    operation: ProbeMutationOperation,
    match: ProbeWindowMatch,
    *,
    observed_at: float,
) -> ProbeWindowSlotBinding:
    return ProbeWindowSlotBinding(
        slot_id=operation.slot_id,
        owner_token=operation.owner_token,
        target_worker_id=operation.target_worker_id,
        target_conversation_url=operation.target_conversation_url,
        actual_url=operation.expected_actual_url,
        window_handle=match.window_handle,
        browser_pid=match.browser_pid,
        chrome_executable=operation.expected_chrome_executable,
        source=operation.source,
        bound_at=observed_at,
        observed_at=observed_at,
        expires_at=observed_at + max(1.0, float(operation.slot_ttl_s)),
    )


def decide_probe_reconciliation(
    operation: ProbeMutationOperation,
    observation: ProbeMutationObservation,
) -> ProbeReconcileDecision:
    """Classify one bounded read-only observation of a durable probe mutation.

    The decision is intentionally fail-closed. A write-ahead OPEN/CLOSE intent means that
    absence alone cannot prove that the external action never happened or is not still in
    flight. Exact unique CWS-owned evidence may be adopted; multiple, changed, or incomplete
    evidence must remain fenced.
    """
    if not observation.complete or observation.error:
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.UNKNOWN_OBSERVATION,
            ProbeMutationState.RECONCILE_REQUIRED,
            "probe observation was incomplete or failed",
        )

    old_matches = list(observation.old_matches)
    new_matches = list(observation.new_matches)
    if len(old_matches) > 1 or len(new_matches) > 1:
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.MULTIPLE_MATCHES,
            ProbeMutationState.RECONCILE_REQUIRED,
            "more than one window matched an ownership tag",
        )

    if old_matches and not _match_is_exact_old(operation, old_matches[0]):
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
            ProbeMutationState.RECONCILE_REQUIRED,
            "old ownership tag no longer matches its durable HWND/PID/executable identity",
        )
    if new_matches and not _match_is_exact_new(operation, new_matches[0]):
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
            ProbeMutationState.RECONCILE_REQUIRED,
            "new ownership tag no longer matches the expected executable/target identity",
        )

    old_present = bool(old_matches)
    new_present = bool(new_matches)
    effective_state = _effective_state(operation)

    if old_present and new_present:
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.BOTH_OLD_AND_NEW_PRESENT,
            ProbeMutationState.RECONCILE_REQUIRED,
            "both old and replacement probe targets are present",
        )

    if operation.kind == ProbeMutationKind.OPEN:
        if old_present:
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
                ProbeMutationState.RECONCILE_REQUIRED,
                "OPEN operation unexpectedly observed a prior target",
            )
        if new_present:
            if effective_state != ProbeMutationState.OPEN_SUBMITTED:
                return ProbeReconcileDecision(
                    ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
                    ProbeMutationState.RECONCILE_REQUIRED,
                    "expected target appeared before durable OPEN authority was issued",
                )
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.EXACT_UNIQUE_OWNED_TARGET_PRESENT,
                ProbeMutationState.COMPLETED,
                "exactly one expected CWS-owned probe target is present; adopt it",
                adopt_binding=_binding_from_new_match(
                    operation, new_matches[0], observed_at=observation.observed_at
                ),
            )
        if effective_state == ProbeMutationState.ARMED:
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.EXACT_TARGET_ABSENT,
                ProbeMutationState.ARMED,
                "no target exists and no OPEN authority has been issued",
                may_open=True,
            )
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.EXACT_TARGET_ABSENT,
            ProbeMutationState.RECONCILE_REQUIRED,
            "target is absent after OPEN authority was issued; do not replay blindly",
        )

    if operation.kind == ProbeMutationKind.CLOSE:
        if new_present:
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
                ProbeMutationState.RECONCILE_REQUIRED,
                "CLOSE operation unexpectedly observed a replacement target",
            )
        if not old_present:
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.EXACT_TARGET_ABSENT,
                ProbeMutationState.COMPLETED,
                "the exact old CWS-owned probe target is absent; close is reconciled",
            )
        if effective_state == ProbeMutationState.ARMED:
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.OLD_TARGET_STILL_PRESENT,
                ProbeMutationState.ARMED,
                "the exact old target remains and no CLOSE authority has been issued",
                may_close=True,
            )
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.OLD_TARGET_STILL_PRESENT,
            ProbeMutationState.RECONCILE_REQUIRED,
            "old target remains after CLOSE authority was issued; do not replay blindly",
        )

    if operation.kind != ProbeMutationKind.ROTATE:
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.UNKNOWN_OBSERVATION,
            ProbeMutationState.RECONCILE_REQUIRED,
            "unsupported probe mutation kind",
        )

    if new_present and not old_present:
        if effective_state != ProbeMutationState.OPEN_SUBMITTED:
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
                ProbeMutationState.RECONCILE_REQUIRED,
                "replacement appeared before durable OPEN authority was issued",
            )
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.EXACT_UNIQUE_OWNED_TARGET_PRESENT,
            ProbeMutationState.COMPLETED,
            "replacement is the unique expected owned target and the old target is absent; adopt it",
            adopt_binding=_binding_from_new_match(
                operation, new_matches[0], observed_at=observation.observed_at
            ),
        )

    if old_present and not new_present:
        if effective_state == ProbeMutationState.ARMED:
            return ProbeReconcileDecision(
                ProbeReconcileOutcome.OLD_TARGET_STILL_PRESENT,
                ProbeMutationState.ARMED,
                "old target remains and no CLOSE authority has been issued",
                may_close=True,
            )
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.OLD_TARGET_STILL_PRESENT,
            ProbeMutationState.RECONCILE_REQUIRED,
            "old target remains after CLOSE/OPEN progress; do not replay blindly",
        )

    # Neither old nor replacement exists. This is the safe crash point after an exact old
    # close and before replacement OPEN authority. It is also safe when ARMED because the old
    # target disappeared independently before any authority was issued.
    if effective_state in {
        ProbeMutationState.ARMED,
        ProbeMutationState.CLOSE_SUBMITTED,
        ProbeMutationState.READY_TO_OPEN,
    }:
        return ProbeReconcileDecision(
            ProbeReconcileOutcome.EXACT_TARGET_ABSENT,
            ProbeMutationState.READY_TO_OPEN,
            "old target is absent and replacement has not appeared; replacement may be opened once",
            may_open=True,
        )
    return ProbeReconcileDecision(
        ProbeReconcileOutcome.EXACT_TARGET_ABSENT,
        ProbeMutationState.RECONCILE_REQUIRED,
        "both targets are absent after OPEN authority was issued; do not replay blindly",
    )
