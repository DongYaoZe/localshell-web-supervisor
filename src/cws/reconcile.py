from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .models import (
    Assessment,
    BrowserObservation,
    LsmObservation,
    NetworkObservation,
    ReconciliationRecord,
    TaskRecord,
    WorkspaceObservation,
)


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_reconciliation_record(
    task: TaskRecord,
    assessment: Assessment,
    *,
    browser: BrowserObservation | None,
    network: NetworkObservation | None,
    lsm: LsmObservation | None,
    workspace: WorkspaceObservation | None,
    created_at: float | None = None,
) -> ReconciliationRecord:
    """Build a sanitized point-in-time evidence fence for future recovery actions.

    The snapshot deliberately excludes conversation text, request/response bodies,
    headers, cookies, tokens, and full LSM activity history. It stores only identifiers,
    timestamps, state flags, and content-independent digests needed to detect whether the
    world changed after reconciliation.
    """
    created_at = time.time() if created_at is None else float(created_at)
    checkpoint_digest = _digest_json(task.checkpoint)
    snapshot: dict[str, Any] = {
        "task": {
            "task_id": task.task_id,
            "state": task.state.value,
            "current_worker_id": task.current_worker_id,
            "lsm_session_id": task.lsm_session_id,
            "recovery_attempts": task.recovery_attempts,
            "max_recovery_attempts": task.max_recovery_attempts,
            "checkpoint_digest": checkpoint_digest,
        },
        "browser": (
            {
                "worker_id": browser.worker_id,
                "observed_at": browser.observed_at,
                "url": browser.url,
                "generating": browser.generating,
                "send_button_ready": browser.send_button_ready,
                "pending_tool_calls": browser.pending_tool_calls,
                "visible_error": browser.visible_error,
                "last_dom_change_at": browser.last_dom_change_at,
                "message_signature": browser.message_signature,
            }
            if browser
            else None
        ),
        "network": (
            {
                "worker_id": network.worker_id,
                "observed_at": network.observed_at,
                "source": network.source,
                "page_url": network.page_url,
                "event_count": network.event_count,
                "loading_failed": network.loading_failed,
                "last_activity_at": network.last_activity_at,
                "quiet_since_at": network.quiet_since_at,
                "inflight_requests": network.inflight_requests,
            }
            if network
            else None
        ),
        "lsm": (
            {
                "observed_at": lsm.observed_at,
                "session_id": lsm.session_id,
                "session_status": lsm.session_status,
                "active_run_id": lsm.active_run_id,
                "plan_status": lsm.plan_status,
                "plan_last_agent_activity": lsm.plan_last_agent_activity,
                "continuation_due": lsm.continuation_due,
                "continuation_pending": lsm.continuation_pending,
                "in_flight_calls": lsm.in_flight_calls,
                "freshest_in_flight_heartbeat": lsm.freshest_in_flight_heartbeat,
                "active_jobs": lsm.active_jobs,
                "failed_jobs": lsm.failed_jobs,
                "succeeded_jobs": lsm.succeeded_jobs,
                "recent_event_type": lsm.recent_event_type,
                "recent_event_at": lsm.recent_event_at,
                "completed_steps": lsm.completed_steps,
                "total_steps": lsm.total_steps,
            }
            if lsm
            else None
        ),
        "workspace": (
            {
                "observed_at": workspace.observed_at,
                "cwd": workspace.cwd,
                "cwd_exists": workspace.cwd_exists,
                "is_git_repo": workspace.is_git_repo,
                "git_root": workspace.git_root,
                "git_head": workspace.git_head,
                "git_dirty": workspace.git_dirty,
                "git_status_hash": workspace.git_status_hash,
                "error": workspace.error,
            }
            if workspace
            else None
        ),
    }
    fence_token = _digest_json(snapshot)
    return ReconciliationRecord(
        reconcile_id=f"rec_{uuid.uuid4().hex[:16]}",
        task_id=task.task_id,
        created_at=created_at,
        state=assessment.state.value,
        confidence=assessment.confidence,
        reason=assessment.reason,
        requires_reconcile=assessment.requires_reconcile,
        current_worker_id=task.current_worker_id,
        fence_token=fence_token,
        evidence=list(assessment.evidence),
        snapshot=snapshot,
    )


def fence_matches(a: ReconciliationRecord, b: ReconciliationRecord) -> bool:
    """Return True only when two reconciliations describe the same actionable world state."""
    return a.task_id == b.task_id and a.fence_token == b.fence_token
