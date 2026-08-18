from __future__ import annotations

import json

from .models import (
    Assessment,
    LsmObservation,
    RecoveryRecommendation,
    SupervisorState,
    TaskRecord,
    WorkspaceObservation,
)
from .prompts import with_safety_preamble

RECOVERY_PROMPT = """Resume the existing task.

First inspect the current Local Shell logical session, Goal plan, tracked jobs, and actual workspace state.
The previous ChatGPT message may have failed after side effects were already executed.
Do not repeat completed operations.
Continue from the first genuinely incomplete step.

Task id: {task_id}
Local Shell logical session: {session_id}
Working directory: {cwd}
Recorded checkpoint: {checkpoint}
Observed workspace: {workspace}
"""


def recommend(
    task: TaskRecord,
    assessment: Assessment,
    lsm: LsmObservation | None,
    workspace: WorkspaceObservation | None = None,
) -> RecoveryRecommendation:
    if assessment.state == SupervisorState.COMPLETED:
        return RecoveryRecommendation(
            task.task_id,
            "none",
            False,
            "task is durably complete",
            evidence=assessment.evidence,
        )
    if assessment.state == SupervisorState.ABANDONED:
        return RecoveryRecommendation(
            task.task_id,
            "none",
            False,
            "task was abandoned/cancelled",
            evidence=assessment.evidence,
        )
    if assessment.state == SupervisorState.NEEDS_HUMAN:
        return RecoveryRecommendation(
            task.task_id,
            "human_decision",
            False,
            "task requires a human decision before recovery",
            evidence=assessment.evidence,
        )
    if assessment.state == SupervisorState.BLOCKED:
        return RecoveryRecommendation(
            task.task_id,
            "human_decision",
            False,
            "Goal plan is blocked; automatic continuation would bypass an explicit blocker",
            evidence=assessment.evidence,
        )
    if assessment.state in {
        SupervisorState.RUNNING,
        SupervisorState.STARTING,
        SupervisorState.RECOVERING,
    }:
        return RecoveryRecommendation(
            task.task_id,
            "observe",
            False,
            "work still appears active; do not compete with the current worker",
            evidence=assessment.evidence,
        )
    if task.recovery_attempts >= task.max_recovery_attempts:
        return RecoveryRecommendation(
            task.task_id,
            "human_decision",
            False,
            "recovery-attempt budget exhausted",
            evidence=assessment.evidence,
        )
    if not task.lsm_session_id or not lsm:
        return RecoveryRecommendation(
            task.task_id,
            "human_decision",
            False,
            "no durable LSM session observation is available for reconciliation",
            evidence=assessment.evidence,
        )
    if lsm.in_flight_calls > 0 or lsm.active_jobs > 0 or lsm.continuation_pending:
        return RecoveryRecommendation(
            task.task_id,
            "observe",
            False,
            "durable LSM work/continuation is active; takeover or continue is unsafe",
            evidence=assessment.evidence,
        )
    prompt = with_safety_preamble(
        RECOVERY_PROMPT.format(
            task_id=task.task_id,
            session_id=task.lsm_session_id,
            cwd=task.cwd,
            checkpoint=json.dumps(task.checkpoint, ensure_ascii=False, sort_keys=True),
            workspace=_workspace_summary(workspace),
        )
    )
    return RecoveryRecommendation(
        task.task_id,
        "reconcile_then_continue",
        False,  # V0 is deliberately advisory only.
        "candidate for a new ChatGPT turn after explicit reconciliation; V0 does not auto-dispatch",
        prompt=prompt,
        evidence=assessment.evidence,
    )


def _workspace_summary(workspace: WorkspaceObservation | None) -> str:
    if workspace is None:
        return "unavailable"
    parts = [f"cwd_exists={workspace.cwd_exists}"]
    if workspace.is_git_repo is not None:
        parts.append(f"git_repo={workspace.is_git_repo}")
    if workspace.git_head:
        parts.append(f"git_head={workspace.git_head}")
    if workspace.git_dirty is not None:
        parts.append(f"git_dirty={workspace.git_dirty}")
    if workspace.git_status_hash:
        parts.append(f"git_status_hash={workspace.git_status_hash}")
    if workspace.error:
        parts.append(f"error={workspace.error}")
    return ", ".join(parts)
