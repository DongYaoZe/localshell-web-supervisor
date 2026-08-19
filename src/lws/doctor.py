from __future__ import annotations

import importlib.util
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import __version__
from .lsm import FileLsmTelemetry, UnsupportedLsmState, detect_lsm_state_dir
from .ram import MemoryProbeUnavailable, observe_system_memory, observe_windows_process_group
from .registry import Registry
from .uia import ChromeUiaProbe, UiaProbeUnavailable
from .watchdog_host import inspect_watchdog_host
from .workspace import WorkspaceProbe


class DoctorStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(slots=True)
class DoctorCheck:
    name: str
    status: DoctorStatus
    detail: str


@dataclass(slots=True)
class DoctorReport:
    observed_at: float
    version: str
    overall: DoctorStatus
    checks: list[DoctorCheck] = field(default_factory=list)


def run_doctor(
    registry: Registry,
    *,
    lsm_state_dir: str | Path | None = None,
    task_id: str | None = None,
    probe_uia: bool = False,
) -> DoctorReport:
    checks: list[DoctorCheck] = []
    checks.append(DoctorCheck("lws.version", DoctorStatus.PASS, __version__))
    checks.append(DoctorCheck("registry", DoctorStatus.PASS, str(registry.db_path)))

    state_dir = detect_lsm_state_dir(lsm_state_dir)
    adapter: FileLsmTelemetry | None = None
    if state_dir is None:
        checks.append(
            DoctorCheck(
                "lsm.state",
                DoctorStatus.WARN,
                "Local Shell MCP durable state directory was not detected",
            )
        )
    else:
        adapter = FileLsmTelemetry(state_dir)
        try:
            jobs = adapter.jobs_payload()
            checks.append(
                DoctorCheck(
                    "lsm.state",
                    DoctorStatus.PASS,
                    f"{state_dir} jobs_schema={jobs.get('version')}",
                )
            )
        except (OSError, UnsupportedLsmState, ValueError) as exc:
            checks.append(DoctorCheck("lsm.state", DoctorStatus.FAIL, str(exc)))
            adapter = None

    try:
        memory = observe_system_memory()
        status = DoctorStatus.WARN if memory.used_fraction >= 0.85 else DoctorStatus.PASS
        checks.append(
            DoctorCheck(
                "memory.system",
                status,
                f"used={memory.used_fraction:.1%} available={memory.available_bytes / (1024**3):.2f} GiB",
            )
        )
    except MemoryProbeUnavailable as exc:
        checks.append(DoctorCheck("memory.system", DoctorStatus.WARN, str(exc)))

    try:
        chrome = observe_windows_process_group("chrome")
        checks.append(
            DoctorCheck(
                "memory.chrome",
                DoctorStatus.PASS if not chrome.error else DoctorStatus.WARN,
                (
                    f"processes={chrome.process_count} "
                    f"working_set={chrome.total_working_set_bytes / (1024**3):.2f} GiB"
                    if not chrome.error
                    else chrome.error
                ),
            )
        )
    except MemoryProbeUnavailable as exc:
        checks.append(DoctorCheck("memory.chrome", DoctorStatus.WARN, str(exc)))

    playwright_available = importlib.util.find_spec("playwright") is not None
    checks.append(
        DoctorCheck(
            "playwright.optional",
            DoctorStatus.PASS if playwright_available else DoctorStatus.WARN,
            "available for dedicated/CDP observation"
            if playwright_available
            else "not installed; UIA/LSM V1 still works without it",
        )
    )

    watchdog = inspect_watchdog_host(registry)
    if not watchdog.lease_present:
        checks.append(
            DoctorCheck(
                "watchdog.lease",
                DoctorStatus.WARN,
                "no resident watchdog lease is currently registered",
            )
        )
    elif watchdog.stop_requested:
        checks.append(
            DoctorCheck(
                "watchdog.lease",
                DoctorStatus.WARN,
                f"cooperative stop requested for pid={watchdog.pid}",
            )
        )
    elif watchdog.fresh and watchdog.pid_alive:
        checks.append(
            DoctorCheck(
                "watchdog.lease",
                DoctorStatus.PASS,
                f"fresh owner={watchdog.host} pid={watchdog.pid}",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "watchdog.lease",
                DoctorStatus.WARN,
                watchdog.detail,
            )
        )

    checks.append(
        DoctorCheck(
            "recovery.transport",
            DoctorStatus.PASS,
            "gated by default; execution is either explicit one-shot or explicitly opted-in recognized-timeout recovery, with all semantic/LSM/workspace/exact-window/action fences",
        )
    )

    probe_slots = registry.probe_window_slots()
    if len(probe_slots) > 1:
        checks.append(
            DoctorCheck(
                "probe.slot",
                DoctorStatus.FAIL,
                f"{len(probe_slots)} durable probe slots exist; at most one reusable slot is allowed",
            )
        )
    elif not probe_slots:
        checks.append(DoctorCheck("probe.slot", DoctorStatus.PASS, "no reusable probe slot is bound"))
    else:
        slot = probe_slots[0]
        fresh = slot.is_fresh(now=now)
        checks.append(
            DoctorCheck(
                "probe.slot",
                DoctorStatus.PASS if fresh else DoctorStatus.WARN,
                (
                    f"slot={slot.slot_id} worker={slot.target_worker_id} fresh={str(fresh).lower()}"
                    if fresh
                    else f"slot={slot.slot_id} is stale; reconcile ownership before any replacement window"
                ),
            )
        )

    unresolved_probe = registry.unresolved_probe_mutation_operation()
    if unresolved_probe is None:
        checks.append(
            DoctorCheck(
                "probe.mutation",
                DoctorStatus.PASS,
                "no unresolved write-ahead probe mutation operation",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "probe.mutation",
                DoctorStatus.WARN,
                (
                    f"operation={unresolved_probe.operation_id} kind={unresolved_probe.kind.value} "
                    f"state={unresolved_probe.state.value}; reconcile before any new probe mutation"
                ),
            )
        )

    capabilities = registry.page_capabilities(limit=200)
    fresh_capabilities = sum(1 for row in capabilities if row.is_fresh(now=now))
    checks.append(
        DoctorCheck(
            "page.capability",
            DoctorStatus.PASS,
            f"records={len(capabilities)} time_fresh={fresh_capabilities}; use remains explicit and context-bound",
        )
    )

    if task_id:
        try:
            task = registry.get_task(task_id)
        except KeyError as exc:
            checks.append(DoctorCheck("task", DoctorStatus.FAIL, str(exc)))
        else:
            checks.append(
                DoctorCheck(
                    "task",
                    DoctorStatus.PASS,
                    f"{task.task_id} state={task.state.value} worker={task.current_worker_id}",
                )
            )
            workspace = WorkspaceProbe().observe(task_id=task.task_id, cwd=task.cwd)
            if not workspace.cwd_exists:
                checks.append(
                    DoctorCheck("task.workspace", DoctorStatus.FAIL, workspace.error or "cwd missing")
                )
            elif workspace.error:
                checks.append(DoctorCheck("task.workspace", DoctorStatus.WARN, workspace.error))
            else:
                detail = f"cwd={workspace.cwd}"
                if workspace.git_head:
                    detail += f" git={workspace.git_head[:12]} dirty={workspace.git_dirty}"
                checks.append(DoctorCheck("task.workspace", DoctorStatus.PASS, detail))

            if task.lsm_session_id and adapter is not None:
                try:
                    obs = adapter.observe(
                        task_id=task.task_id,
                        session_id=task.lsm_session_id,
                        tracked_job_ids=registry.tracked_jobs(task.task_id),
                    )
                    checks.append(
                        DoctorCheck(
                            "task.lsm",
                            DoctorStatus.PASS,
                            (
                                f"session={obs.session_status} plan={obs.plan_status} "
                                f"inflight={obs.in_flight_calls} active_jobs={obs.active_jobs}"
                            ),
                        )
                    )
                except (OSError, UnsupportedLsmState, ValueError) as exc:
                    checks.append(DoctorCheck("task.lsm", DoctorStatus.FAIL, str(exc)))
            elif task.lsm_session_id:
                checks.append(
                    DoctorCheck(
                        "task.lsm",
                        DoctorStatus.WARN,
                        "task has an LSM session id but durable state adapter is unavailable",
                    )
                )
            else:
                checks.append(
                    DoctorCheck("task.lsm", DoctorStatus.WARN, "task has no durable LSM session id")
                )

            unresolved_action = registry.unresolved_action_attempt(task.task_id)
            if unresolved_action is None:
                checks.append(
                    DoctorCheck(
                        "task.action_fence",
                        DoctorStatus.PASS,
                        "no unresolved write-ahead action attempt",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        "task.action_fence",
                        DoctorStatus.WARN,
                        (
                            f"{unresolved_action.attempt_id} state={unresolved_action.state.value}; "
                            "new sends are fenced until reconciled"
                        ),
                    )
                )

            if task.current_worker_id:
                try:
                    worker = registry.get_worker(task.current_worker_id)
                except KeyError as exc:
                    checks.append(DoctorCheck("task.worker", DoctorStatus.FAIL, str(exc)))
                else:
                    checks.append(
                        DoctorCheck(
                            "task.worker",
                            DoctorStatus.PASS,
                            f"status={worker.status.value} url={worker.conversation_url}",
                        )
                    )
                    binding = registry.get_worker_window_binding(worker.worker_id)
                    if binding is None:
                        checks.append(
                            DoctorCheck(
                                "task.window_binding",
                                DoctorStatus.PASS,
                                "no exact-window lease is present; UIA mutation has no bound window",
                            )
                        )
                    else:
                        remaining = binding.expires_at - time.time()
                        if remaining > 0:
                            checks.append(
                                DoctorCheck(
                                    "task.window_binding",
                                    DoctorStatus.PASS,
                                    (
                                        f"fresh hwnd={binding.window_handle} pid={binding.browser_pid} "
                                        f"expires_in={remaining:.1f}s source={binding.source}"
                                    ),
                                )
                            )
                        else:
                            checks.append(
                                DoctorCheck(
                                    "task.window_binding",
                                    DoctorStatus.WARN,
                                    (
                                        f"stale hwnd={binding.window_handle} pid={binding.browser_pid}; "
                                        "refresh exact-URL UIA observation before any action"
                                    ),
                                )
                            )
                    if probe_uia:
                        try:
                            obs = ChromeUiaProbe().observe(
                                worker,
                                previous=registry.latest_browser_observation(worker.worker_id),
                            )
                            checks.append(
                                DoctorCheck(
                                    "task.uia",
                                    DoctorStatus.PASS,
                                    (
                                        f"generating={obs.generating} send_ready={obs.send_button_ready} "
                                        f"signature={str(obs.message_signature or '')[:12]}"
                                    ),
                                )
                            )
                        except UiaProbeUnavailable as exc:
                            checks.append(DoctorCheck("task.uia", DoctorStatus.WARN, str(exc)))
            else:
                checks.append(
                    DoctorCheck("task.worker", DoctorStatus.WARN, "task has no current worker")
                )

    overall = DoctorStatus.PASS
    if any(check.status == DoctorStatus.FAIL for check in checks):
        overall = DoctorStatus.FAIL
    elif any(check.status == DoctorStatus.WARN for check in checks):
        overall = DoctorStatus.WARN
    return DoctorReport(
        observed_at=time.time(),
        version=__version__,
        overall=overall,
        checks=checks,
    )
