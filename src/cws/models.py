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
class WorkerWindowBinding:
    worker_id: str
    window_handle: int
    browser_pid: int
    chrome_executable: str
    conversation_url: str
    source: str
    bound_at: float
    observed_at: float
    expires_at: float

    def is_fresh(self, *, now: float) -> bool:
        return self.expires_at > float(now)


class PageCapabilityKind(StrEnum):
    GENERATION = "page_close_generation"
    TOOL_EXECUTION = "page_close_tool"


@dataclass(slots=True)
class PageCapabilityRecord:
    capability_id: str
    kind: PageCapabilityKind
    scope_host: str
    browser_family: str
    browser_major: int
    platform: str
    surface: str
    isolation_mode: str
    evaluator_version: str
    evidence_digest: str
    source_experiment_id: str
    observed_at: float
    recorded_at: float
    expires_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_fresh(self, *, now: float) -> bool:
        return self.observed_at <= float(now) < self.expires_at


@dataclass(slots=True)
class ProbeWindowSlotBinding:
    slot_id: str
    owner_token: str
    target_worker_id: str
    target_conversation_url: str
    actual_url: str
    window_handle: int
    browser_pid: int
    chrome_executable: str
    source: str
    bound_at: float
    observed_at: float
    expires_at: float

    def is_fresh(self, *, now: float) -> bool:
        return self.expires_at > float(now)


class ProbeMutationKind(StrEnum):
    OPEN = "OPEN"
    ROTATE = "ROTATE"
    CLOSE = "CLOSE"


class ProbeMutationState(StrEnum):
    ARMED = "ARMED"
    CLOSE_SUBMITTED = "CLOSE_SUBMITTED"
    READY_TO_OPEN = "READY_TO_OPEN"
    OPEN_SUBMITTED = "OPEN_SUBMITTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class ProbeMutationOperation:
    operation_id: str
    nonce: str
    kind: ProbeMutationKind
    state: ProbeMutationState
    slot_id: str
    target_task_id: str
    target_worker_id: str
    target_conversation_url: str
    owner_token: str
    expected_actual_url: str
    expected_chrome_executable: str
    source: str
    prior_slot: dict[str, Any] | None
    created_at: float
    updated_at: float
    slot_ttl_s: float = 120.0
    reconcile_attempts: int = 0
    last_reconcile_at: float | None = None
    last_outcome: str | None = None
    last_error: str | None = None
    resume_state: str | None = None


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
class NetworkObservation:
    worker_id: str
    observed_at: float
    source: str
    sample_started_at: float
    sample_ended_at: float
    page_url: str | None = None
    event_count: int = 0
    request_count: int = 0
    response_count: int = 0
    data_event_count: int = 0
    encoded_data_bytes: int = 0
    loading_finished: int = 0
    loading_failed: int = 0
    websocket_frames: int = 0
    last_activity_at: float | None = None
    quiet_since_at: float | None = None
    inflight_requests: int = 0
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
class ReconciliationRecord:
    reconcile_id: str
    task_id: str
    created_at: float
    state: str
    confidence: str
    reason: str
    requires_reconcile: bool
    current_worker_id: str | None
    fence_token: str
    fence_version: int = 1
    evidence: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RecoveryRecommendation:
    task_id: str
    action: str
    safe_to_dispatch: bool
    reason: str
    prompt: str | None = None
    evidence: list[str] = field(default_factory=list)
