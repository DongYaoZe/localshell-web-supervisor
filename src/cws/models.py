from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SupervisorState(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUSPECT = "SUSPECT"
    RECONCILING = "RECONCILING"
    RECOVERING = "RECOVERING"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class WorkerStatus(StrEnum):
    ACTIVE = "active"
    PARKED = "parked"
    SUPERSEDED = "superseded"
    DEAD = "dead"


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    project: str
    objective: str
    cwd: str
    state: SupervisorState
    lsm_session_id: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    current_worker_id: str | None = None
    recovery_attempts: int = 0
    max_recovery_attempts: int = 3
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(slots=True)
class WorkerRecord:
    worker_id: str
    task_id: str
    conversation_url: str
    conversation_id: str | None
    status: WorkerStatus
    started_at: float
    last_seen_at: float | None = None
    ended_at: float | None = None


@dataclass(slots=True)
class BrowserObservation:
    worker_id: str
    observed_at: float
    url: str | None = None
    generating: bool | None = None
    send_button_ready: bool | None = None
    pending_tool_calls: int | None = None
    visible_error: str | None = None
    last_dom_change_at: float | None = None
    message_signature: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LsmObservation:
    task_id: str
    observed_at: float
    session_id: str | None = None
    session_status: str | None = None
    active_run_id: str | None = None
    plan_status: str | None = None
    plan_last_agent_activity: float | None = None
    continuation_due: bool | None = None
    continuation_pending: bool | None = None
    in_flight_calls: int = 0
    freshest_in_flight_heartbeat: float | None = None
    active_jobs: int = 0
    failed_jobs: int = 0
    succeeded_jobs: int = 0
    recent_event_type: str | None = None
    recent_event_at: float | None = None
    completed_steps: int | None = None
    total_steps: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkspaceObservation:
    task_id: str
    observed_at: float
    cwd: str
    cwd_exists: bool
    is_git_repo: bool | None = None
    git_root: str | None = None
    git_head: str | None = None
    git_dirty: bool | None = None
    git_status_hash: str | None = None
    git_status_entries: list[str] = field(default_factory=list)
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Assessment:
    state: SupervisorState
    reason: str
    confidence: str
    evidence: list[str] = field(default_factory=list)
    requires_reconcile: bool = False
    may_auto_continue: bool = False


@dataclass(slots=True)
class RecoveryRecommendation:
    task_id: str
    action: str
    safe_to_dispatch: bool
    reason: str
    prompt: str | None = None
    evidence: list[str] = field(default_factory=list)
