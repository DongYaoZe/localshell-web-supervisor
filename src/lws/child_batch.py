from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

from .child_spawn import (
    ChildSpawnBlocked,
    ChromeUiaChildSpawnTransport,
    arm_child_spawn,
    execute_child_spawn_open,
    execute_child_spawn_prompt,
)
from .models import (
    ChildSpawnAttempt,
    ChildSpawnAttemptState,
    SupervisorState,
    WorkerRecord,
    WorkerWindowBinding,
    WorkspaceObservation,
)
from .registry import Registry


class ChildBatchTransport(Protocol):
    def open_authorized(self, attempt: ChildSpawnAttempt): ...
    def reuse_authorized(
        self,
        attempt: ChildSpawnAttempt,
        *,
        source_worker: WorkerRecord,
        source_binding: WorkerWindowBinding,
    ): ...
    def reuse_completed_spawn_authorized(
        self,
        attempt: ChildSpawnAttempt,
        *,
        source_attempt: ChildSpawnAttempt,
    ): ...
    def send_authorized(self, attempt: ChildSpawnAttempt, prompt: str): ...
    def wait_for_conversation(self, attempt: ChildSpawnAttempt): ...
    def observe_bound_delivery(self, attempt: ChildSpawnAttempt): ...
    def wait_for_delivery(self, attempt: ChildSpawnAttempt, prompt: str): ...
    def close_worker_binding_authorized(
        self,
        *,
        worker: WorkerRecord,
        binding: WorkerWindowBinding,
    ): ...
    def close_completed_spawn_authorized(
        self,
        *,
        source_attempt: ChildSpawnAttempt,
    ): ...


WorkspaceLoader = Callable[[str], WorkspaceObservation]
TransportFactory = Callable[[str], ChildBatchTransport]


@dataclass(slots=True)
class ChildBatchEvent:
    child_task_id: str
    action: str
    detail: str
    attempt_id: str | None = None
    source_child_task_id: str | None = None
    window_handle: int | None = None


@dataclass(slots=True)
class ChildBatchReport:
    parent_task_id: str
    max_windows: int
    total_children: int
    dispatched_children: int = 0
    bound_session_children: int = 0
    pending_children: int = 0
    pool_windows: int = 0
    waiting_for_binding: list[str] = field(default_factory=list)
    stopped: bool = False
    stop_reason: str | None = None
    events: list[ChildBatchEvent] = field(default_factory=list)

    def payload(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class _ReusableWindow:
    child_task_id: str
    terminal: bool
    worker: WorkerRecord | None = None
    binding: WorkerWindowBinding | None = None
    source_spawn: ChildSpawnAttempt | None = None


_AMBIGUOUS_STATES = {
    ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
    ChildSpawnAttemptState.PROMPT_SUBMITTED,
    ChildSpawnAttemptState.RECONCILE_REQUIRED,
}


def _window_key(window_handle: int | None, browser_pid: int | None) -> tuple[int, int] | None:
    if not window_handle or not browser_pid:
        return None
    return int(window_handle), int(browser_pid)


def _spawn_page_unconsumed(attempt: ChildSpawnAttempt) -> bool:
    return not (
        attempt.metadata.get("window_recycled_to")
        or attempt.metadata.get("window_closed_at")
    )


def _pool_snapshot(
    registry: Registry,
    child_task_ids: list[str],
) -> tuple[set[tuple[int, int]], list[_ReusableWindow], list[str]]:
    windows: set[tuple[int, int]] = set()
    reusable: list[_ReusableWindow] = []
    waiting: list[str] = []

    # Current worker bindings and unresolved pre-send attempts are direct current owners.
    for child_task_id in child_task_ids:
        task = registry.get_task(child_task_id)
        if task.current_worker_id:
            worker = registry.get_worker(task.current_worker_id)
            binding = registry.get_worker_window_binding(worker.worker_id)
            if binding is not None:
                windows.add((binding.window_handle, binding.browser_pid))
                terminal = task.state == SupervisorState.COMPLETED
                if terminal or task.lsm_session_id:
                    reusable.append(
                        _ReusableWindow(
                            child_task_id=child_task_id,
                            terminal=terminal,
                            worker=worker,
                            binding=binding,
                        )
                    )
                else:
                    waiting.append(child_task_id)

        unresolved = registry.unresolved_child_spawn_attempt(child_task_id)
        if unresolved is not None:
            key = _window_key(unresolved.window_handle, unresolved.browser_pid)
            if key is not None:
                windows.add(key)

    # child-complete intentionally clears the legacy current worker + window binding. The
    # completed spawn record then becomes the only durable exact HWND/PID/conversation
    # identity. Pick only the newest unconsumed completed owner for each exact window so a
    # historical record from an earlier reuse chain can never reclaim the same HWND.
    latest_completed_by_window: dict[tuple[int, int], ChildSpawnAttempt] = {}
    for child_task_id in child_task_ids:
        for attempt in registry.child_spawn_attempts(child_task_id, limit=50):
            if attempt.state != ChildSpawnAttemptState.COMPLETED or not _spawn_page_unconsumed(attempt):
                continue
            key = _window_key(attempt.window_handle, attempt.browser_pid)
            if key is None or not attempt.conversation_url:
                continue
            current = latest_completed_by_window.get(key)
            if current is None or (attempt.updated_at, attempt.created_at, attempt.attempt_id) > (
                current.updated_at,
                current.created_at,
                current.attempt_id,
            ):
                latest_completed_by_window[key] = attempt

    for key, attempt in latest_completed_by_window.items():
        if key in windows:
            continue
        task = registry.get_task(attempt.child_task_id)
        if task.state != SupervisorState.COMPLETED:
            continue
        windows.add(key)
        reusable.append(
            _ReusableWindow(
                child_task_id=attempt.child_task_id,
                terminal=True,
                source_spawn=attempt,
            )
        )

    # Reclaim terminal pages before running-but-durably-bound pages, then preserve child
    # creation order for deterministic operator behavior.
    order = {child_task_id: index for index, child_task_id in enumerate(child_task_ids)}
    reusable.sort(key=lambda item: (not item.terminal, order[item.child_task_id]))
    return windows, reusable, waiting


def _stop(
    report: ChildBatchReport,
    *,
    child_task_id: str,
    reason: str,
    action: str,
    attempt: ChildSpawnAttempt | None = None,
) -> ChildBatchReport:
    report.stopped = True
    report.stop_reason = reason
    report.events.append(
        ChildBatchEvent(
            child_task_id=child_task_id,
            action=action,
            detail=reason,
            attempt_id=attempt.attempt_id if attempt else None,
            window_handle=attempt.window_handle if attempt else None,
        )
    )
    return report


def _refresh_counts(
    registry: Registry,
    child_task_ids: list[str],
    report: ChildBatchReport,
) -> None:
    windows, _reusable, waiting = _pool_snapshot(registry, child_task_ids)
    report.pool_windows = len(windows)
    for child_task_id in waiting:
        if child_task_id not in report.waiting_for_binding:
            report.waiting_for_binding.append(child_task_id)
    report.waiting_for_binding.sort(key=child_task_ids.index)
    report.dispatched_children = sum(
        1
        for child_task_id in child_task_ids
        if (
            registry.get_task(child_task_id).state == SupervisorState.COMPLETED
            or registry.load_worker_protocol(child_task_id).generation > 0
        )
    )
    report.bound_session_children = sum(
        1 for child_task_id in child_task_ids if registry.get_task(child_task_id).lsm_session_id
    )
    report.pending_children = sum(
        1
        for child_task_id in child_task_ids
        if registry.get_task(child_task_id).state != SupervisorState.COMPLETED
        and registry.get_task(child_task_id).current_worker_id is None
    )


def advance_child_dispatch_batch(
    registry: Registry,
    *,
    parent_task_id: str,
    max_windows: int,
    chrome_executable: str,
    workspace_loader: WorkspaceLoader,
    transport_factory: TransportFactory,
    lease_seconds: float = 7200.0,
    max_evidence_age_s: float = 60.0,
    close_terminal_pages: bool = True,
) -> ChildBatchReport:
    """Advance a persisted child batch using the existing child-spawn state machine.

    This is deliberately a one-shot advancement helper, not a resident loop. Invoke it
    again after children bind durable LSM sessions. It preflights ambiguous states before
    any new browser mutation, opens at most ``max_windows`` exact pages, and reuses a page
    only after durable LSM binding or terminal task completion.
    """

    if max_windows <= 0 or max_windows > 16:
        raise ValueError("max_windows must be between 1 and 16")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    dispatches = registry.child_dispatches_for_parent(parent_task_id)
    child_task_ids = [dispatch.child_task_id for dispatch in dispatches]
    report = ChildBatchReport(
        parent_task_id=parent_task_id,
        max_windows=int(max_windows),
        total_children=len(dispatches),
    )

    # Fail closed for the whole batch before new side effects if any child already has an
    # unresolved external result. ARMED and WINDOW_BOUND child-spawn states are safe durable
    # handoff points; unresolved action/replacement/probe mutations are not.
    probe_operation = registry.unresolved_probe_mutation_operation()
    if probe_operation is not None:
        report.stopped = True
        report.stop_reason = (
            f"global probe mutation {probe_operation.operation_id} is unresolved; "
            "reconcile before batch dispatch"
        )
        report.events.append(
            ChildBatchEvent(parent_task_id, "ambiguous_stop", report.stop_reason)
        )
        return report

    for child_task_id in child_task_ids:
        unresolved_action = registry.unresolved_action_attempt(child_task_id)
        if unresolved_action is not None:
            return _stop(
                report,
                child_task_id=child_task_id,
                reason=(
                    f"child has unresolved external action {unresolved_action.attempt_id} in "
                    f"{unresolved_action.state.value}; reconcile before batch dispatch"
                ),
                action="ambiguous_stop",
            )
        unresolved_replacement = registry.unresolved_replacement_attempt(child_task_id)
        if unresolved_replacement is not None:
            return _stop(
                report,
                child_task_id=child_task_id,
                reason=(
                    f"child has unresolved replacement {unresolved_replacement.attempt_id} in "
                    f"{unresolved_replacement.state.value}; reconcile before batch dispatch"
                ),
                action="ambiguous_stop",
            )
        unresolved = registry.unresolved_child_spawn_attempt(child_task_id)
        if unresolved is not None and unresolved.state in _AMBIGUOUS_STATES:
            return _stop(
                report,
                child_task_id=child_task_id,
                reason=(
                    f"child has unresolved spawn {unresolved.attempt_id} in "
                    f"{unresolved.state.value}; reconcile before batch dispatch"
                ),
                action="ambiguous_stop",
                attempt=unresolved,
            )

    for dispatch in dispatches:
        child_task_id = dispatch.child_task_id
        task = registry.get_task(child_task_id)

        if task.state == SupervisorState.COMPLETED:
            report.events.append(
                ChildBatchEvent(child_task_id, "terminal", "child is already durably completed")
            )
            continue

        if task.current_worker_id:
            if task.lsm_session_id:
                report.events.append(
                    ChildBatchEvent(
                        child_task_id,
                        "session_bound",
                        "child worker is durable; its exact page may be recycled",
                    )
                )
            else:
                if child_task_id not in report.waiting_for_binding:
                    report.waiting_for_binding.append(child_task_id)
                report.events.append(
                    ChildBatchEvent(
                        child_task_id,
                        "awaiting_lsm_binding",
                        "child keeps its page until a durable LSM session is bound",
                    )
                )
            continue

        attempt = registry.unresolved_child_spawn_attempt(child_task_id)
        if attempt is None:
            try:
                attempt = arm_child_spawn(
                    registry,
                    child_task_id=child_task_id,
                    chrome_executable=chrome_executable,
                    workspace=workspace_loader(child_task_id),
                    max_evidence_age_s=max_evidence_age_s,
                )
            except ChildSpawnBlocked as exc:
                return _stop(
                    report,
                    child_task_id=child_task_id,
                    reason="; ".join(exc.blockers),
                    action="arm_blocked",
                )
            report.events.append(
                ChildBatchEvent(
                    child_task_id,
                    "armed",
                    "spawn authority persisted; no browser mutation yet",
                    attempt_id=attempt.attempt_id,
                )
            )

        if attempt.state == ChildSpawnAttemptState.ARMED:
            windows, reusable, waiting = _pool_snapshot(registry, child_task_ids)
            report.pool_windows = len(windows)
            for item in waiting:
                if item not in report.waiting_for_binding:
                    report.waiting_for_binding.append(item)

            source = next((item for item in reusable if item.child_task_id != child_task_id), None)
            transport = transport_factory(attempt.chrome_executable)
            try:
                if source is not None and source.worker is not None and source.binding is not None:
                    opened = execute_child_spawn_open(
                        registry,
                        attempt_id=attempt.attempt_id,
                        transport=transport,
                        source_worker=source.worker,
                        source_binding=source.binding,
                    )
                    source_kind = "terminal completion" if source.terminal else "durable LSM binding"
                elif source is not None and source.source_spawn is not None:
                    opened = execute_child_spawn_open(
                        registry,
                        attempt_id=attempt.attempt_id,
                        transport=transport,
                        source_spawn=source.source_spawn,
                    )
                    source_kind = "terminal completed-spawn record"
                elif len(windows) < max_windows:
                    opened = execute_child_spawn_open(
                        registry,
                        attempt_id=attempt.attempt_id,
                        transport=transport,
                    )
                    source_kind = None
                else:
                    report.events.append(
                        ChildBatchEvent(
                            child_task_id,
                            "pool_wait",
                            (
                                f"dispatcher pool is full ({len(windows)}/{max_windows}); "
                                "waiting for an existing child to bind a durable LSM session"
                            ),
                            attempt_id=attempt.attempt_id,
                        )
                    )
                    _refresh_counts(registry, child_task_ids, report)
                    return report
            except ChildSpawnBlocked as exc:
                return _stop(
                    report,
                    child_task_id=child_task_id,
                    reason="; ".join(exc.blockers),
                    action="open_blocked",
                    attempt=attempt,
                )

            if source_kind is None:
                action = "opened_window"
                detail = "opened one new dispatcher window within the configured pool bound"
                source_child_task_id = None
            else:
                action = "reused_window"
                detail = f"reused exact HWND from {source.child_task_id} after {source_kind}"
                source_child_task_id = source.child_task_id
            report.events.append(
                ChildBatchEvent(
                    child_task_id,
                    action,
                    detail,
                    attempt_id=opened.attempt_id,
                    source_child_task_id=source_child_task_id,
                    window_handle=opened.window_handle,
                )
            )
            attempt = opened
            if attempt.state in _AMBIGUOUS_STATES:
                return _stop(
                    report,
                    child_task_id=child_task_id,
                    reason=attempt.last_error or f"open ended in {attempt.state.value}",
                    action="ambiguous_stop",
                    attempt=attempt,
                )
            if attempt.state != ChildSpawnAttemptState.WINDOW_BOUND:
                return _stop(
                    report,
                    child_task_id=child_task_id,
                    reason=attempt.last_error or f"open ended in {attempt.state.value}",
                    action="open_failed",
                    attempt=attempt,
                )

        if attempt.state == ChildSpawnAttemptState.WINDOW_BOUND:
            transport = transport_factory(attempt.chrome_executable)
            try:
                sent = execute_child_spawn_prompt(
                    registry,
                    attempt_id=attempt.attempt_id,
                    transport=transport,
                    lease_seconds=lease_seconds,
                )
            except ChildSpawnBlocked as exc:
                return _stop(
                    report,
                    child_task_id=child_task_id,
                    reason="; ".join(exc.blockers),
                    action="send_blocked",
                    attempt=attempt,
                )
            report.events.append(
                ChildBatchEvent(
                    child_task_id,
                    "sent" if sent.state == ChildSpawnAttemptState.COMPLETED else "send_incomplete",
                    sent.last_error or "persisted child prompt delivered to the exact bound conversation",
                    attempt_id=sent.attempt_id,
                    window_handle=sent.window_handle,
                )
            )
            attempt = sent
            if attempt.state in _AMBIGUOUS_STATES:
                return _stop(
                    report,
                    child_task_id=child_task_id,
                    reason=attempt.last_error or f"send ended in {attempt.state.value}",
                    action="ambiguous_stop",
                    attempt=attempt,
                )
            if attempt.state != ChildSpawnAttemptState.COMPLETED:
                return _stop(
                    report,
                    child_task_id=child_task_id,
                    reason=attempt.last_error or f"send ended in {attempt.state.value}",
                    action="send_paused",
                    attempt=attempt,
                )
            if not registry.get_task(child_task_id).lsm_session_id:
                report.waiting_for_binding.append(child_task_id)

    _refresh_counts(registry, child_task_ids, report)

    # With no undispatched child left, terminal pages are no longer useful dispatcher
    # slots. Close only exact terminal identities and durably mark completed spawn records
    # consumed so a later batch invocation cannot mistake history for a live page.
    if close_terminal_pages and report.pending_children == 0:
        _windows, reusable, _waiting = _pool_snapshot(registry, child_task_ids)
        for source in [item for item in reusable if item.terminal]:
            if source.worker is not None and source.binding is not None:
                transport = transport_factory(source.binding.chrome_executable)
                result = transport.close_worker_binding_authorized(
                    worker=source.worker,
                    binding=source.binding,
                )
                if result.changed or (
                    not result.side_effect_possible and "already absent" in (result.detail or "")
                ):
                    registry.clear_worker_window_binding(source.worker.worker_id)
                else:
                    return _stop(
                        report,
                        child_task_id=source.child_task_id,
                        reason=result.detail or "terminal page close could not be proven safe",
                        action="terminal_close_stop",
                    )
                window_handle = source.binding.window_handle
            elif source.source_spawn is not None:
                transport = transport_factory(source.source_spawn.chrome_executable)
                result = transport.close_completed_spawn_authorized(
                    source_attempt=source.source_spawn,
                )
                if result.changed or (
                    not result.side_effect_possible and "already absent" in (result.detail or "")
                ):
                    registry.annotate_child_spawn_attempt(
                        source.source_spawn.attempt_id,
                        metadata_updates={"window_closed_at": time.time()},
                    )
                else:
                    return _stop(
                        report,
                        child_task_id=source.child_task_id,
                        reason=result.detail or "terminal page close could not be proven safe",
                        action="terminal_close_stop",
                    )
                window_handle = source.source_spawn.window_handle
            else:
                continue
            report.events.append(
                ChildBatchEvent(
                    source.child_task_id,
                    "closed_terminal_window",
                    result.detail or "closed exact terminal child window",
                    window_handle=window_handle,
                )
            )
        _refresh_counts(registry, child_task_ids, report)

    return report


def default_transport_factory(chrome_executable: str) -> ChromeUiaChildSpawnTransport:
    return ChromeUiaChildSpawnTransport(chrome_executable=chrome_executable, enabled=True)
