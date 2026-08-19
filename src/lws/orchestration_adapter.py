from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Protocol

from .capabilities import CapabilityContext, capability_matches_context
from .lsm import FileLsmTelemetry
from .models import (
    BrowserObservation,
    LsmObservation,
    NetworkObservation,
    PageCapabilityKind,
    PageCapabilityRecord,
    ProbeMutationOperation,
    TaskRecord,
    WorkerRecord,
    WorkspaceObservation,
)
from .orchestration import (
    OrchestrationDecision,
    OrchestrationPolicy,
    TaskOrchestrationInput,
    evaluate_task,
    plan_orchestration,
)
from .reconcile import build_reconciliation_record, fence_matches
from .registry import Registry
from .watcher import WatchPolicy, assess
from .workspace import WorkspaceProbe


class LsmTelemetryProvider(Protocol):
    def observe(
        self,
        *,
        task_id: str,
        session_id: str,
        tracked_job_ids: list[str],
    ) -> LsmObservation: ...


class WorkspaceEvidenceProvider(Protocol):
    def observe(self, *, task_id: str, cwd: str) -> WorkspaceObservation: ...


@dataclass(frozen=True, slots=True)
class PageContinuityRequest:
    context: CapabilityContext | None
    kind: PageCapabilityKind = PageCapabilityKind.GENERATION


@dataclass(frozen=True, slots=True)
class TaskRuntimeHints:
    """Schema-neutral runtime facts that the current Registry cannot derive durably."""

    worker_lease_expires_at: float | None = None
    last_recovery_at: float | None = None
    requires_exact_window_binding: bool = True
    page_continuity: PageContinuityRequest | None = None


@dataclass(frozen=True, slots=True)
class TaskSchedulingHistory:
    """Explicit scheduler history supplied by the caller; absence withholds selection."""

    last_scheduled_at: float | None = None
    consecutive_waits: int = 0


_UNSET = object()


class AdvisoryOrchestrationAdapter:
    """Read-only bridge from durable/runtime evidence to the pure orchestration policy.

    The adapter never records observations, reconciliations, actions, leases, probe operations,
    or scheduler history. It refreshes Local Shell and workspace evidence through read-only
    providers and reads every other fact from the Registry. Any missing or contradictory fact
    is expressed as a pure reconcile/human blocker.
    """

    def __init__(
        self,
        registry: Registry,
        *,
        lsm_telemetry: LsmTelemetryProvider | FileLsmTelemetry,
        workspace_probe: WorkspaceEvidenceProvider | WorkspaceProbe,
        watch_policy: WatchPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.lsm_telemetry = lsm_telemetry
        self.workspace_probe = workspace_probe
        self.watch_policy = watch_policy or WatchPolicy()

    def build_candidate(
        self,
        task_id: str,
        *,
        runtime: TaskRuntimeHints | None = None,
        scheduling: TaskSchedulingHistory | None = None,
        now: float | None = None,
        unresolved_probe: ProbeMutationOperation | None | object = _UNSET,
    ) -> TaskOrchestrationInput:
        now = time.time() if now is None else float(now)
        runtime = runtime or TaskRuntimeHints()
        task = self.registry.get_task(task_id)
        worker = self._current_worker(task)
        browser = self.registry.latest_browser_observation(task.current_worker_id)
        network = self.registry.latest_network_observation(task.current_worker_id)

        reconcile_blockers: list[str] = []
        human_blockers: list[str] = []

        lsm = self._refresh_lsm(task, reconcile_blockers)
        workspace = self._refresh_workspace(task, reconcile_blockers)
        current_assessment = assess(
            task,
            browser,
            lsm,
            workspace=workspace,
            network=network,
            now=now,
            policy=self.watch_policy,
        )

        history = self.registry.reconciliation_history(task.task_id, limit=2)
        current_reconciliation = history[0] if history else None
        previous_reconciliation = history[1] if len(history) > 1 else None
        unresolved_action = self.registry.unresolved_action_attempt(task.task_id)
        window_binding = (
            self.registry.get_worker_window_binding(worker.worker_id, now=now, require_fresh=False)
            if worker is not None
            else None
        )

        last_recovery_at = self._last_recovery_at(task, runtime, reconcile_blockers)
        self._validate_runtime_timestamps(
            now=now,
            worker=worker,
            browser=browser,
            network=network,
            lsm=lsm,
            workspace=workspace,
            window_binding=window_binding,
            reconcile_blockers=reconcile_blockers,
        )

        if workspace is not None and workspace.git_dirty is True:
            reconcile_blockers.append(
                "workspace/Git is dirty; reconcile durable side effects before recovery"
            )

        if current_reconciliation is not None and lsm is not None and workspace is not None:
            live_record = build_reconciliation_record(
                task,
                current_assessment,
                worker=worker,
                browser=browser,
                network=network,
                lsm=lsm,
                workspace=workspace,
                created_at=now,
            )
            if not fence_matches(current_reconciliation, live_record):
                reconcile_blockers.append(
                    "live durable evidence no longer matches the latest reconciliation fence"
                )

        page_request = runtime.page_continuity
        page_capability = self._page_capability(page_request, now=now)
        if unresolved_probe is _UNSET:
            unresolved_probe = self.registry.unresolved_probe_mutation_operation()
        if page_request is not None and unresolved_probe is not None:
            state = getattr(unresolved_probe.state, "value", unresolved_probe.state)
            human_blockers.append(
                "page continuity is fenced by unresolved probe mutation "
                f"{unresolved_probe.operation_id} in state {state}"
            )

        scheduling_known = scheduling is not None
        if scheduling is None:
            last_scheduled_at = None
            consecutive_waits = 0
        else:
            last_scheduled_at = scheduling.last_scheduled_at
            consecutive_waits = int(scheduling.consecutive_waits)
            if last_scheduled_at is not None and (
                float(last_scheduled_at) < 0.0 or float(last_scheduled_at) > now
            ):
                scheduling_known = False
                reconcile_blockers.append("scheduler history timestamp is invalid or in the future")
            if consecutive_waits < 0:
                scheduling_known = False
                reconcile_blockers.append("scheduler wait count is negative")

        return TaskOrchestrationInput(
            task=task,
            assessment=current_assessment,
            worker=worker,
            lsm=lsm,
            workspace=workspace,
            previous_reconciliation=previous_reconciliation,
            current_reconciliation=current_reconciliation,
            unresolved_action=unresolved_action,
            unresolved_probe_mutation=(
                unresolved_probe if unresolved_probe is not _UNSET else None
            ),
            worker_lease_expires_at=runtime.worker_lease_expires_at,
            last_recovery_at=last_recovery_at,
            last_scheduled_at=last_scheduled_at,
            consecutive_waits=consecutive_waits,
            requires_exact_window_binding=runtime.requires_exact_window_binding,
            window_binding=window_binding,
            page_continuity_relevant=page_request is not None,
            page_capability=page_capability,
            capability_context=page_request.context if page_request is not None else None,
            page_capability_kind=(
                page_request.kind if page_request is not None else PageCapabilityKind.GENERATION
            ),
            reconcile_blockers=tuple(dict.fromkeys(reconcile_blockers)),
            human_blockers=tuple(dict.fromkeys(human_blockers)),
            scheduling_history_known=scheduling_known,
        )

    def evaluate(
        self,
        task_id: str,
        *,
        runtime: TaskRuntimeHints | None = None,
        scheduling: TaskSchedulingHistory | None = None,
        now: float | None = None,
        policy: OrchestrationPolicy | None = None,
    ) -> OrchestrationDecision:
        now = time.time() if now is None else float(now)
        candidate = self.build_candidate(
            task_id,
            runtime=runtime,
            scheduling=scheduling,
            now=now,
        )
        decision = evaluate_task(candidate, now=now, policy=policy)
        return replace(decision, mutation_allowed=False, selected=False)

    def plan(
        self,
        task_ids: Iterable[str] | None = None,
        *,
        runtime_hints: Mapping[str, TaskRuntimeHints] | None = None,
        scheduling_history: Mapping[str, TaskSchedulingHistory] | None = None,
        now: float | None = None,
        policy: OrchestrationPolicy | None = None,
    ) -> list[OrchestrationDecision]:
        """Return a stable, duplicate-free, bounded advisory plan for multiple tasks."""

        now = time.time() if now is None else float(now)
        runtime_hints = runtime_hints or {}
        scheduling_history = scheduling_history or {}
        if task_ids is None:
            ids = sorted({task.task_id for task in self.registry.list_tasks()})
        else:
            ids = sorted({str(task_id) for task_id in task_ids})

        unresolved_probe = self.registry.unresolved_probe_mutation_operation()
        candidates = [
            self.build_candidate(
                task_id,
                runtime=runtime_hints.get(task_id),
                scheduling=scheduling_history.get(task_id),
                now=now,
                unresolved_probe=unresolved_probe,
            )
            for task_id in ids
        ]
        decisions = plan_orchestration(candidates, now=now, policy=policy)
        return sorted(
            (replace(decision, mutation_allowed=False) for decision in decisions),
            key=lambda decision: decision.task_id,
        )

    def _current_worker(self, task: TaskRecord) -> WorkerRecord | None:
        if not task.current_worker_id:
            return None
        try:
            return self.registry.get_worker(task.current_worker_id)
        except KeyError:
            return None

    def _refresh_lsm(
        self,
        task: TaskRecord,
        reconcile_blockers: list[str],
    ) -> LsmObservation | None:
        if not task.lsm_session_id:
            return None
        try:
            return self.lsm_telemetry.observe(
                task_id=task.task_id,
                session_id=task.lsm_session_id,
                tracked_job_ids=self.registry.tracked_jobs(task.task_id),
            )
        except Exception as exc:
            reconcile_blockers.append(
                f"Local Shell telemetry refresh failed ({type(exc).__name__})"
            )
            return None

    def _refresh_workspace(
        self,
        task: TaskRecord,
        reconcile_blockers: list[str],
    ) -> WorkspaceObservation | None:
        try:
            return self.workspace_probe.observe(task_id=task.task_id, cwd=task.cwd)
        except Exception as exc:
            reconcile_blockers.append(f"workspace/Git refresh failed ({type(exc).__name__})")
            return None

    def _last_recovery_at(
        self,
        task: TaskRecord,
        runtime: TaskRuntimeHints,
        reconcile_blockers: list[str],
    ) -> float | None:
        recent_actions = self.registry.action_attempts(task.task_id, limit=1)
        durable = recent_actions[0].created_at if recent_actions else None
        supplied = runtime.last_recovery_at
        candidates = [float(value) for value in (durable, supplied) if value is not None]
        if task.recovery_attempts > 0 and not candidates:
            reconcile_blockers.append(
                "recovery budget records prior attempts but cooldown timestamp is unavailable"
            )
            return None
        return max(candidates) if candidates else None

    def _page_capability(
        self,
        request: PageContinuityRequest | None,
        *,
        now: float,
    ) -> PageCapabilityRecord | None:
        if request is None or request.context is None:
            return None
        candidates = self.registry.page_capabilities(kind=request.kind, limit=100)
        for capability in candidates:
            ok, _ = capability_matches_context(
                capability,
                request.context,
                expected_kind=request.kind,
                now=now,
            )
            if ok:
                return capability
        return candidates[0] if candidates else None

    @staticmethod
    def _validate_runtime_timestamps(
        *,
        now: float,
        worker: WorkerRecord | None,
        browser: BrowserObservation | None,
        network: NetworkObservation | None,
        lsm: LsmObservation | None,
        workspace: WorkspaceObservation | None,
        window_binding,
        reconcile_blockers: list[str],
    ) -> None:
        observed = {
            "worker last-seen": worker.last_seen_at if worker is not None else None,
            "browser observation": browser.observed_at if browser is not None else None,
            "network observation": network.observed_at if network is not None else None,
            "Local Shell observation": lsm.observed_at if lsm is not None else None,
            "workspace observation": workspace.observed_at if workspace is not None else None,
        }
        for label, value in observed.items():
            if value is not None and float(value) > now:
                reconcile_blockers.append(f"{label} timestamp is in the future")
        if window_binding is not None:
            sane = (
                float(window_binding.bound_at)
                <= float(window_binding.observed_at)
                <= now
                and float(window_binding.expires_at) >= float(window_binding.observed_at)
            )
            if not sane:
                reconcile_blockers.append("exact-window binding timestamps are inconsistent")
