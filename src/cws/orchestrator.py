from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from .models import (
    BrowserObservation,
    LsmObservation,
    SupervisorState,
    TaskRecord,
    WorkerRecord,
    WorkerStatus,
)
from .ram import BrowserMemoryObservation, SystemMemoryObservation


class PageDisposition(StrEnum):
    KEEP_ACTIVE = "KEEP_ACTIVE"
    DO_NOT_CLOSE = "DO_NOT_CLOSE"
    PARK_CANDIDATE = "PARK_CANDIDATE"
    NO_PAGE = "NO_PAGE"


@dataclass(slots=True)
class BrowserPoolPolicy:
    max_active_workers: int = 4
    max_probe_pages: int = 1
    min_available_bytes: int = 1024 * 1024 * 1024
    high_memory_fraction: float = 0.85
    page_close_experiment_passed: bool = False


@dataclass(slots=True)
class WorkerPoolItem:
    task_id: str
    worker_id: str | None
    task_state: str
    worker_status: str | None
    disposition: PageDisposition
    reason: str
    close_allowed: bool
    browser_pid: int | None = None
    observed_process_working_set_bytes: int | None = None
    lsm_in_flight_calls: int = 0
    lsm_active_jobs: int = 0
    lsm_continuation_pending: bool = False
    browser_generating: bool | None = None


@dataclass(slots=True)
class BrowserPoolPlan:
    observed_at: float
    policy: BrowserPoolPolicy
    memory_pressure: str
    system_memory: SystemMemoryObservation | None
    browser_memory: BrowserMemoryObservation | None
    active_worker_count: int
    pinned_worker_count: int
    park_candidate_count: int
    items: list[WorkerPoolItem] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def classify_worker_for_pool(
    task: TaskRecord,
    worker: WorkerRecord | None,
    *,
    browser: BrowserObservation | None,
    lsm: LsmObservation | None,
    policy: BrowserPoolPolicy,
) -> WorkerPoolItem:
    if worker is None:
        return WorkerPoolItem(
            task_id=task.task_id,
            worker_id=None,
            task_state=task.state.value,
            worker_status=None,
            disposition=PageDisposition.NO_PAGE,
            reason="task has no registered conversation worker",
            close_allowed=False,
        )

    live_lsm = bool(
        lsm
        and (
            lsm.in_flight_calls > 0
            or lsm.active_jobs > 0
            or bool(lsm.continuation_pending)
        )
    )
    generating = browser.generating if browser else None
    pid = None
    working_set = None
    if browser:
        raw = browser.raw or {}
        if raw.get("browser_pid") is not None:
            try:
                pid = int(raw["browser_pid"])
            except (TypeError, ValueError):
                pid = None
        if raw.get("process_working_set_bytes") is not None:
            try:
                working_set = int(raw["process_working_set_bytes"])
            except (TypeError, ValueError):
                working_set = None

    common = dict(
        task_id=task.task_id,
        worker_id=worker.worker_id,
        task_state=task.state.value,
        worker_status=worker.status.value,
        browser_pid=pid,
        observed_process_working_set_bytes=working_set,
        lsm_in_flight_calls=lsm.in_flight_calls if lsm else 0,
        lsm_active_jobs=lsm.active_jobs if lsm else 0,
        lsm_continuation_pending=bool(lsm.continuation_pending) if lsm else False,
        browser_generating=generating,
    )

    if worker.status in {WorkerStatus.PARKED, WorkerStatus.SUPERSEDED, WorkerStatus.DEAD}:
        return WorkerPoolItem(
            **common,
            disposition=PageDisposition.NO_PAGE,
            reason=f"worker status {worker.status.value} is not an active page lease",
            close_allowed=False,
        )

    if live_lsm:
        return WorkerPoolItem(
            **common,
            disposition=PageDisposition.DO_NOT_CLOSE,
            reason="durable LSM work/continuation is active",
            close_allowed=False,
        )
    if generating is True:
        return WorkerPoolItem(
            **common,
            disposition=PageDisposition.DO_NOT_CLOSE,
            reason="browser reports active generation",
            close_allowed=False,
        )
    if task.state in {
        SupervisorState.STARTING,
        SupervisorState.RUNNING,
        SupervisorState.RECOVERING,
        SupervisorState.RECONCILING,
        SupervisorState.SUSPECT,
        SupervisorState.NEEDS_HUMAN,
    }:
        return WorkerPoolItem(
            **common,
            disposition=PageDisposition.DO_NOT_CLOSE,
            reason=f"task state {task.state.value} requires observation or may still be live",
            close_allowed=False,
        )

    park_reason = {
        SupervisorState.COMPLETED: "task is durably complete",
        SupervisorState.ABANDONED: "task is abandoned/cancelled",
        SupervisorState.QUEUED: "task is queued with no live execution evidence",
        SupervisorState.BLOCKED: "task is blocked with no live LSM work",
    }.get(task.state, "task has no live execution evidence")
    close_allowed = bool(policy.page_close_experiment_passed)
    return WorkerPoolItem(
        **common,
        disposition=PageDisposition.PARK_CANDIDATE,
        reason=park_reason,
        close_allowed=close_allowed,
    )


def plan_browser_pool(
    rows: list[
        tuple[TaskRecord, WorkerRecord | None, BrowserObservation | None, LsmObservation | None]
    ],
    *,
    system_memory: SystemMemoryObservation | None,
    browser_memory: BrowserMemoryObservation | None,
    policy: BrowserPoolPolicy | None = None,
    observed_at: float | None = None,
) -> BrowserPoolPlan:
    policy = policy or BrowserPoolPolicy()
    observed_at = time.time() if observed_at is None else float(observed_at)
    items = [
        classify_worker_for_pool(task, worker, browser=browser, lsm=lsm, policy=policy)
        for task, worker, browser, lsm in rows
    ]
    live_items = [item for item in items if item.disposition != PageDisposition.NO_PAGE]
    pinned = [item for item in items if item.disposition == PageDisposition.DO_NOT_CLOSE]
    candidates = [item for item in items if item.disposition == PageDisposition.PARK_CANDIDATE]

    memory_pressure = "unknown"
    recommendations: list[str] = []
    warnings: list[str] = []
    if system_memory is not None:
        high_fraction = system_memory.used_fraction >= policy.high_memory_fraction
        low_available = system_memory.available_bytes <= policy.min_available_bytes
        memory_pressure = "high" if (high_fraction or low_available) else "normal"
        if memory_pressure == "high":
            recommendations.append(
                "memory pressure is high; prioritize parking terminal/queued/blocked candidates"
            )
    if browser_memory and browser_memory.error:
        warnings.append(f"browser memory probe: {browser_memory.error}")

    if len(live_items) > policy.max_active_workers:
        recommendations.append(
            f"registered worker count {len(live_items)} exceeds active-worker target "
            f"{policy.max_active_workers}"
        )
    if len(pinned) > policy.max_active_workers:
        warnings.append(
            "pinned live/ambiguous workers alone exceed the configured active-worker target; "
            "do not close them automatically"
        )
    if candidates and not policy.page_close_experiment_passed:
        recommendations.append(
            "park candidates exist, but actual page-close dispatch is disabled until a dedicated "
            "ChatGPT close/reopen experiment proves it safe"
        )
    if not candidates and memory_pressure == "high":
        warnings.append(
            "memory pressure is high but no worker is conservatively parkable; preserve task safety "
            "and surface the pressure rather than closing live pages"
        )

    # Rank terminal workers before queued/blocked candidates. This is only advice; it does
    # not mutate worker status or close a browser page.
    rank = {
        SupervisorState.COMPLETED.value: 0,
        SupervisorState.ABANDONED.value: 1,
        SupervisorState.QUEUED.value: 2,
        SupervisorState.BLOCKED.value: 3,
    }
    items.sort(
        key=lambda item: (
            0 if item.disposition == PageDisposition.PARK_CANDIDATE else 1,
            rank.get(item.task_state, 99),
            item.task_id,
        )
    )
    return BrowserPoolPlan(
        observed_at=observed_at,
        policy=policy,
        memory_pressure=memory_pressure,
        system_memory=system_memory,
        browser_memory=browser_memory,
        active_worker_count=len(live_items),
        pinned_worker_count=len(pinned),
        park_candidate_count=len(candidates),
        items=items,
        recommendations=recommendations,
        warnings=warnings,
    )
