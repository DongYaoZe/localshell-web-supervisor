from __future__ import annotations

import time
import uuid
from dataclasses import asdict

from .models import LsmObservation, ReplacementAttempt, ReplacementAttemptState, WorkspaceObservation
from .registry import Registry
from .worker_protocol import (
    TakeoverReason,
    WorkerLeaseStatus,
    takeover_eligibility,
    worker_by_id,
)


class ReplacementBlocked(RuntimeError):
    def __init__(self, blockers: list[str]):
        self.blockers = tuple(blockers)
        super().__init__("; ".join(blockers))


def _fresh(observed_at: float, *, now: float, max_age_s: float) -> bool:
    age = float(now) - float(observed_at)
    return 0 <= age <= float(max_age_s)


def _safe_lsm(
    task_session_id: str | None,
    lsm: LsmObservation | None,
    *,
    now: float,
    max_age_s: float,
) -> list[str]:
    blockers: list[str] = []
    if not task_session_id:
        return ["child has no bound durable LSM logical session"]
    if lsm is None:
        return ["fresh LSM observation is required"]
    if lsm.session_id != task_session_id:
        blockers.append("LSM observation belongs to a different logical session")
    if not _fresh(lsm.observed_at, now=now, max_age_s=max_age_s):
        blockers.append("LSM observation is stale or from the future")
    if lsm.in_flight_calls:
        blockers.append(f"LSM has {lsm.in_flight_calls} in-flight tool call(s)")
    if lsm.active_jobs:
        blockers.append(f"LSM has {lsm.active_jobs} active tracked job(s)")
    if lsm.continuation_pending:
        blockers.append("LSM has a pending continuation")
    return blockers


def _safe_workspace(
    dispatch,
    workspace: WorkspaceObservation | None,
    *,
    now: float,
    max_age_s: float,
) -> list[str]:
    blockers: list[str] = []
    if workspace is None:
        return ["fresh workspace observation is required"]
    if not _fresh(workspace.observed_at, now=now, max_age_s=max_age_s):
        blockers.append("workspace observation is stale or from the future")
    if not workspace.cwd_exists:
        blockers.append("child working directory does not exist")
    if workspace.error:
        blockers.append(f"workspace reconciliation failed: {workspace.error}")
    if dispatch.expected_branch:
        if workspace.is_git_repo is not True:
            blockers.append("dispatch expects a Git branch but workspace is not a verified Git repository")
        elif workspace.git_branch != dispatch.expected_branch:
            blockers.append(
                f"workspace branch changed: expected {dispatch.expected_branch}, "
                f"observed {workspace.git_branch or '<detached>'}"
            )
    return blockers


def arm_replacement(
    registry: Registry,
    *,
    task_id: str,
    candidate_worker_id: str,
    lsm: LsmObservation | None,
    workspace: WorkspaceObservation | None,
    now: float | None = None,
    max_evidence_age_s: float = 60.0,
) -> ReplacementAttempt:
    """Durably arm one replacement before any LSM takeover side effect is authorized."""

    ts = time.time() if now is None else float(now)
    dispatch = registry.get_child_dispatch(task_id)
    task = registry.get_task(task_id)
    state = registry.load_worker_protocol(task_id)
    candidate = worker_by_id(state, candidate_worker_id)
    blockers: list[str] = []

    if registry.unresolved_replacement_attempt(task_id) is not None:
        blockers.append("an unresolved replacement attempt already exists")
    unresolved_action = registry.unresolved_action_attempt(task_id)
    if unresolved_action is not None:
        blockers.append(
            f"unresolved external browser action {unresolved_action.attempt_id} "
            f"is {unresolved_action.state.value}"
        )
    unresolved_probe = registry.unresolved_probe_mutation_operation()
    if unresolved_probe is not None:
        blockers.append(
            f"global probe mutation {unresolved_probe.operation_id} is {unresolved_probe.state.value}"
        )
    if state.task_status.value != "open":
        blockers.append("durable child task is not open")
    if candidate is None:
        blockers.append("replacement candidate is not registered on this child task")
    elif candidate.status != WorkerLeaseStatus.CANDIDATE:
        blockers.append(f"replacement worker is {candidate.status.value}, not candidate")
    blockers.extend(_safe_lsm(task.lsm_session_id, lsm, now=ts, max_age_s=max_evidence_age_s))
    blockers.extend(_safe_workspace(dispatch, workspace, now=ts, max_age_s=max_evidence_age_s))

    mode = ""
    takeover_reason = ""
    if candidate is not None and candidate.status == WorkerLeaseStatus.CANDIDATE:
        if state.current_worker_id is None:
            mode = "CLAIM_AFTER_LSM_RESUME"
            takeover_reason = TakeoverReason.NO_ACTIVE_WORKER.value
        else:
            eligibility = takeover_eligibility(state, candidate_worker_id, now=ts)
            takeover_reason = eligibility.reason.value
            if eligibility.eligible:
                mode = "PROTOCOL_TAKEOVER"
            elif eligibility.reason == TakeoverReason.ACTIVE_LEASE_FRESH:
                # LSM takeover is the stronger execution-authority fence. After it succeeds,
                # CWS may abandon the still-fresh old conversation lease and claim the candidate.
                mode = "LSM_FENCE_THEN_CLAIM"
            else:
                blockers.append(f"worker protocol replacement is not eligible: {eligibility.reason.value}")

    if state.current_worker_id is not None and (lsm is None or not lsm.active_run_id):
        blockers.append("active conversation worker has no observed LSM active run to supersede")

    if blockers:
        raise ReplacementBlocked(blockers)

    attempt = ReplacementAttempt(
        attempt_id=f"replace_{uuid.uuid4().hex[:16]}",
        task_id=task_id,
        candidate_worker_id=candidate_worker_id,
        state=ReplacementAttemptState.ARMED,
        expected_revision=state.revision,
        previous_worker_id=state.current_worker_id,
        previous_generation=state.generation,
        lsm_session_id=str(task.lsm_session_id),
        previous_active_run_id=lsm.active_run_id if lsm else None,
        workspace_git_head=workspace.git_head if workspace else None,
        workspace_status_hash=workspace.git_status_hash if workspace else None,
        expected_branch=dispatch.expected_branch,
        mode=mode,
        takeover_reason=takeover_reason,
        created_at=ts,
        updated_at=ts,
        metadata={
            "candidate_conversation_url": candidate.conversation_ref if candidate else None,
            "workspace_cwd": workspace.cwd if workspace else task.cwd,
            "lsm_observed_at": lsm.observed_at if lsm else None,
            "workspace_observed_at": workspace.observed_at if workspace else None,
        },
    )
    return registry.record_replacement_attempt(attempt)


def lsm_takeover_request(attempt: ReplacementAttempt) -> dict:
    return {
        "tool": "Local_Shell_MCP_new.session_manage",
        "arguments": {
            "action": "resume",
            "session_id": attempt.lsm_session_id,
            "takeover": bool(attempt.previous_active_run_id),
        },
        "safety": (
            "Call exactly once after replacement-submit. If the tool result is lost or ambiguous, "
            "do not replay; run replacement-reconcile."
        ),
    }


def submit_replacement(registry: Registry, attempt_id: str, *, now: float | None = None) -> ReplacementAttempt:
    attempt = registry.get_replacement_attempt(attempt_id)
    if attempt.state != ReplacementAttemptState.ARMED:
        raise RuntimeError(f"replacement {attempt_id} is not ARMED")
    return registry.update_replacement_attempt(
        attempt_id,
        state=ReplacementAttemptState.LSM_TAKEOVER_SUBMITTED,
        now=now,
    )


def _completion_blockers(
    registry: Registry,
    attempt: ReplacementAttempt,
    *,
    lsm: LsmObservation | None,
    workspace: WorkspaceObservation | None,
    new_active_run_id: str,
    now: float,
    max_evidence_age_s: float,
) -> list[str]:
    blockers: list[str] = []
    unresolved_action = registry.unresolved_action_attempt(attempt.task_id)
    if unresolved_action is not None:
        blockers.append(
            f"unresolved external browser action {unresolved_action.attempt_id} "
            f"is {unresolved_action.state.value}"
        )
    unresolved_probe = registry.unresolved_probe_mutation_operation()
    if unresolved_probe is not None:
        blockers.append(
            f"global probe mutation {unresolved_probe.operation_id} is {unresolved_probe.state.value}"
        )
    blockers.extend(
        _safe_lsm(attempt.lsm_session_id, lsm, now=now, max_age_s=max_evidence_age_s)
    )
    dispatch = registry.get_child_dispatch(attempt.task_id)
    blockers.extend(_safe_workspace(dispatch, workspace, now=now, max_age_s=max_evidence_age_s))
    if lsm is not None:
        if not new_active_run_id.strip():
            blockers.append("new LSM active run id is required")
        elif lsm.active_run_id != new_active_run_id:
            blockers.append("observed LSM active run does not match the reported takeover result")
        if attempt.previous_active_run_id and new_active_run_id == attempt.previous_active_run_id:
            blockers.append("LSM active run did not change across takeover")
    if workspace is not None:
        if workspace.git_head != attempt.workspace_git_head:
            blockers.append("workspace HEAD changed while replacement was in flight")
        if workspace.git_status_hash != attempt.workspace_status_hash:
            blockers.append("workspace status changed while replacement was in flight")
        if attempt.expected_branch and workspace.git_branch != attempt.expected_branch:
            blockers.append("workspace branch changed while replacement was in flight")
    return blockers


def complete_replacement(
    registry: Registry,
    *,
    attempt_id: str,
    new_active_run_id: str,
    lsm: LsmObservation | None,
    workspace: WorkspaceObservation | None,
    lease_seconds: float = 7200.0,
    now: float | None = None,
    max_evidence_age_s: float = 60.0,
) -> ReplacementAttempt:
    """Publish CWS generation takeover only after the supported LSM takeover is observable."""

    ts = time.time() if now is None else float(now)
    attempt = registry.get_replacement_attempt(attempt_id)
    if attempt.state not in {
        ReplacementAttemptState.LSM_TAKEOVER_SUBMITTED,
        ReplacementAttemptState.RECONCILE_REQUIRED,
    }:
        raise RuntimeError(f"replacement {attempt_id} is not awaiting LSM reconciliation")
    blockers = _completion_blockers(
        registry,
        attempt,
        lsm=lsm,
        workspace=workspace,
        new_active_run_id=new_active_run_id,
        now=ts,
        max_evidence_age_s=max_evidence_age_s,
    )
    state = registry.load_worker_protocol(attempt.task_id)
    candidate = worker_by_id(state, attempt.candidate_worker_id)
    if candidate is None:
        blockers.append("replacement candidate disappeared from worker protocol")
    elif state.current_worker_id == attempt.candidate_worker_id and candidate.status == WorkerLeaseStatus.ACTIVE:
        # Crash recovery: protocol generation already moved but attempt completion did not persist.
        if state.generation <= attempt.previous_generation:
            blockers.append("candidate is active without a newer protocol generation")
        if blockers:
            registry.update_replacement_attempt(
                attempt_id,
                state=ReplacementAttemptState.RECONCILE_REQUIRED,
                new_active_run_id=new_active_run_id,
                last_error="; ".join(blockers),
                now=ts,
            )
            raise ReplacementBlocked(blockers)
        return registry.update_replacement_attempt(
            attempt_id,
            state=ReplacementAttemptState.COMPLETED,
            new_active_run_id=new_active_run_id,
            now=ts,
        )
    elif state.revision != attempt.expected_revision:
        blockers.append(
            f"worker protocol revision changed from {attempt.expected_revision} to {state.revision}"
        )

    if blockers:
        registry.update_replacement_attempt(
            attempt_id,
            state=ReplacementAttemptState.RECONCILE_REQUIRED,
            new_active_run_id=new_active_run_id,
            last_error="; ".join(blockers),
            now=ts,
        )
        raise ReplacementBlocked(blockers)

    if state.current_worker_id is None:
        claimed = registry.protocol_claim_worker(
            attempt.task_id,
            attempt.candidate_worker_id,
            expected_revision=state.revision,
            lease_seconds=lease_seconds,
            now=ts,
        )
        if not claimed.accepted:
            raise RuntimeError(f"replacement claim rejected: {claimed.code.value}")
        state = claimed.state
    else:
        if state.current_worker_id != attempt.previous_worker_id or state.generation != attempt.previous_generation:
            raise ReplacementBlocked(["current worker/generation changed since replacement was armed"])
        eligibility = takeover_eligibility(state, attempt.candidate_worker_id, now=ts)
        if eligibility.eligible:
            takeover = registry.protocol_takeover_worker(
                attempt.task_id,
                attempt.candidate_worker_id,
                expected_revision=state.revision,
                lease_seconds=lease_seconds,
                now=ts,
            )
            if not takeover.accepted:
                raise RuntimeError(f"replacement takeover rejected: {takeover.code.value}")
            state = takeover.state
        elif eligibility.reason == TakeoverReason.ACTIVE_LEASE_FRESH:
            abandoned = registry.protocol_abandon_worker(
                attempt.task_id,
                state.current_worker_id,
                generation=state.generation,
                expected_revision=state.revision,
                now=ts,
            )
            if not abandoned.accepted:
                raise RuntimeError(f"old worker fence rejected: {abandoned.code.value}")
            claimed = registry.protocol_claim_worker(
                attempt.task_id,
                attempt.candidate_worker_id,
                expected_revision=abandoned.state.revision,
                lease_seconds=lease_seconds,
                now=ts,
            )
            if not claimed.accepted:
                raise RuntimeError(f"replacement claim rejected: {claimed.code.value}")
            state = claimed.state
        else:
            raise ReplacementBlocked(
                [f"worker protocol replacement ceased to be eligible: {eligibility.reason.value}"]
            )

    if state.current_worker_id != attempt.candidate_worker_id:
        raise RuntimeError("replacement transition did not publish candidate authority")
    return registry.update_replacement_attempt(
        attempt_id,
        state=ReplacementAttemptState.COMPLETED,
        new_active_run_id=new_active_run_id,
        now=ts,
    )


def replacement_payload(attempt: ReplacementAttempt) -> dict:
    payload = asdict(attempt)
    payload["state"] = attempt.state.value
    payload["lsm_takeover_request"] = lsm_takeover_request(attempt)
    return payload
