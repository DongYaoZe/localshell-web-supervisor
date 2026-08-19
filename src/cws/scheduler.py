from __future__ import annotations

from dataclasses import dataclass

from .models import Assessment, SupervisorState, TaskRecord


_PRIORITY = {
    SupervisorState.NEEDS_HUMAN: 0,
    SupervisorState.BLOCKED: 1,
    SupervisorState.RECONCILING: 2,
    SupervisorState.SUSPECT: 3,
    SupervisorState.RECOVERING: 4,
    SupervisorState.STARTING: 5,
    SupervisorState.QUEUED: 6,
    SupervisorState.RUNNING: 7,
    SupervisorState.COMPLETED: 8,
    SupervisorState.ABANDONED: 9,
}


@dataclass(slots=True)
class AttentionItem:
    task_id: str
    state: SupervisorState
    priority: int
    reason: str


def attention_queue(items: list[tuple[TaskRecord, Assessment]]) -> list[AttentionItem]:
    eligible = {
        SupervisorState.NEEDS_HUMAN,
        SupervisorState.BLOCKED,
        SupervisorState.RECONCILING,
        SupervisorState.SUSPECT,
    }
    best_by_task: dict[str, AttentionItem] = {}
    for task, assessment in items:
        if assessment.state not in eligible:
            continue
        candidate = AttentionItem(
            task.task_id,
            assessment.state,
            _PRIORITY[assessment.state],
            assessment.reason,
        )
        current = best_by_task.get(task.task_id)
        if current is None or (
            candidate.priority,
            candidate.state.value,
            candidate.reason,
        ) < (
            current.priority,
            current.state.value,
            current.reason,
        ):
            best_by_task[task.task_id] = candidate
    queue = list(best_by_task.values())
    queue.sort(key=lambda item: (item.priority, item.task_id))
    return queue
