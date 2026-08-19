from __future__ import annotations

import sqlite3
from collections.abc import Callable

from .models import SupervisorState, WorkerStatus
from .worker_protocol import (
    DurableTaskStatus,
    EventKind,
    ProtocolDecision,
    ProtocolEvent,
    ProtocolInvariantError,
    TaskLineage,
    WorkerLease,
    WorkerLeaseStatus,
    WorkerTaskState,
    validate_state,
)


class WorkerProtocolPersistenceError(RuntimeError):
    """Durable worker-protocol state is missing, ambiguous, or inconsistent."""


ProtocolTransition = Callable[[WorkerTaskState], ProtocolDecision]


def protocol_state_exists(conn: sqlite3.Connection, task_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM worker_protocol_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        is not None
    )


def bootstrap_worker_protocol(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    parent_task_id: str | None = None,
    root_task_id: str | None = None,
    child_key: str | None = None,
) -> WorkerTaskState:
    """Create deterministic protocol metadata for one existing durable LWS task.

    Legacy ACTIVE authority is not silently refreshed. A clean current ACTIVE worker is
    represented at generation 1 with a zero-length lease ending at its last durable legacy
    timestamp. A later caller must reconcile and explicitly hand off/take over before obtaining
    fresh authority.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT 1 FROM worker_protocol_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if existing is not None:
            state = _load_worker_protocol_locked(conn, task_id)
            _require_requested_lineage(
                state,
                parent_task_id=parent_task_id,
                root_task_id=root_task_id,
                child_key=child_key,
            )
            conn.rollback()
            return state

        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        lineage = _resolve_lineage(
            conn,
            task_id,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            child_key=child_key,
        )
        workers = conn.execute(
            "SELECT * FROM workers WHERE task_id = ? ORDER BY worker_id", (task_id,)
        ).fetchall()
        state = _state_from_legacy(task, workers, lineage)
        _insert_protocol_snapshot(conn, state)
        conn.commit()
        return state
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def load_worker_protocol(conn: sqlite3.Connection, task_id: str) -> WorkerTaskState:
    return _load_worker_protocol_locked(conn, task_id)


def apply_worker_protocol_transition(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_revision: int,
    transition: ProtocolTransition,
) -> ProtocolDecision:
    """Apply one pure transition and persist its accepted mutation atomically.

    BEGIN IMMEDIATE serializes writers across separate Registry connections. The persisted
    protocol revision is also compared with ``expected_revision`` by the pure transition and
    by a SQL compare-and-swap update before commit.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        state = _load_worker_protocol_locked(conn, task_id)
        decision = transition(state)
        if decision.expected_revision != int(expected_revision):
            raise WorkerProtocolPersistenceError(
                "transition decision expected_revision does not match registry request"
            )
        if decision.state.task_id != task_id:
            raise WorkerProtocolPersistenceError("transition changed durable task identity")
        if not decision.mutated:
            conn.rollback()
            return decision
        if not decision.accepted:
            raise WorkerProtocolPersistenceError("rejected transition cannot be a mutation")
        if decision.state.revision != int(expected_revision) + 1:
            raise WorkerProtocolPersistenceError("accepted mutation must advance one revision")
        if not decision.events:
            raise WorkerProtocolPersistenceError("accepted protocol mutation must append an event")
        if any(
            event.task_id != task_id or event.revision != decision.state.revision
            for event in decision.events
        ):
            raise WorkerProtocolPersistenceError("protocol event task/revision mismatch")
        validate_state(decision.state)
        _persist_decision_locked(
            conn,
            previous=state,
            decision=decision,
            expected_revision=int(expected_revision),
        )
        reloaded = _load_worker_protocol_locked(conn, task_id)
        if reloaded != decision.state:
            raise WorkerProtocolPersistenceError(
                "persisted worker protocol does not round-trip to the accepted pure state"
            )
        conn.commit()
        return decision
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def worker_protocol_events(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    limit: int = 200,
) -> tuple[ProtocolEvent, ...]:
    rows = conn.execute(
        """SELECT * FROM worker_protocol_events
           WHERE task_id = ?
           ORDER BY revision ASC, event_index ASC
           LIMIT ?""",
        (task_id, max(1, min(int(limit), 2000))),
    ).fetchall()
    return tuple(
        ProtocolEvent(
            kind=EventKind(row["kind"]),
            task_id=row["task_id"],
            revision=int(row["revision"]),
            at=float(row["at"]),
            worker_id=row["worker_id"],
            generation=row["generation"],
            related_worker_id=row["related_worker_id"],
            ref=row["ref"],
        )
        for row in rows
    )


def _state_from_legacy(
    task: sqlite3.Row,
    workers: list[sqlite3.Row],
    lineage: TaskLineage,
) -> WorkerTaskState:
    current_worker_id = task["current_worker_id"]
    try:
        legacy_state = SupervisorState(task["state"])
    except ValueError as exc:
        raise WorkerProtocolPersistenceError(
            f"unknown legacy task state: {task['state']}"
        ) from exc
    if legacy_state == SupervisorState.ABANDONED:
        raise WorkerProtocolPersistenceError(
            "legacy ABANDONED task meaning is ambiguous for protocol bootstrap"
        )

    by_id = {row["worker_id"]: row for row in workers}
    active_ids = [
        row["worker_id"] for row in workers if row["status"] == WorkerStatus.ACTIVE.value
    ]
    parked_ids = [
        row["worker_id"] for row in workers if row["status"] == WorkerStatus.PARKED.value
    ]
    if parked_ids:
        raise WorkerProtocolPersistenceError(
            "legacy PARKED workers have ambiguous protocol authority; reconcile before bootstrap"
        )
    if current_worker_id is None:
        if active_ids:
            raise WorkerProtocolPersistenceError(
                "legacy ACTIVE worker exists without tasks.current_worker_id"
            )
    else:
        current = by_id.get(current_worker_id)
        if current is None:
            raise WorkerProtocolPersistenceError(
                "tasks.current_worker_id does not identify a worker in the same task"
            )
        if current["status"] != WorkerStatus.ACTIVE.value or active_ids != [current_worker_id]:
            raise WorkerProtocolPersistenceError(
                "legacy current worker is not the sole ACTIVE worker"
            )
        if legacy_state == SupervisorState.COMPLETED:
            raise WorkerProtocolPersistenceError(
                "legacy COMPLETED task cannot retain an ACTIVE current worker"
            )

    generation = 1 if current_worker_id is not None else 0
    leases: list[WorkerLease] = []
    for row in workers:
        try:
            status = WorkerStatus(row["status"])
        except ValueError as exc:
            raise WorkerProtocolPersistenceError(
                f"unknown legacy worker status: {row['status']}"
            ) from exc
        if status == WorkerStatus.ACTIVE:
            if row["ended_at"] is not None:
                raise WorkerProtocolPersistenceError(
                    "legacy ACTIVE worker unexpectedly has terminal ended_at"
                )
            heartbeat = (
                float(row["last_seen_at"])
                if row["last_seen_at"] is not None
                else float(row["started_at"])
            )
            leases.append(
                WorkerLease(
                    worker_id=row["worker_id"],
                    task_id=task["task_id"],
                    conversation_ref=row["conversation_url"],
                    status=WorkerLeaseStatus.ACTIVE,
                    registered_at=float(row["started_at"]),
                    generation=1,
                    claimed_at=float(row["started_at"]),
                    last_heartbeat_at=heartbeat,
                    lease_expires_at=heartbeat,
                )
            )
        elif status == WorkerStatus.SUPERSEDED:
            if row["ended_at"] is None:
                raise WorkerProtocolPersistenceError(
                    "legacy SUPERSEDED worker lacks terminal ended_at evidence"
                )
            leases.append(
                WorkerLease(
                    worker_id=row["worker_id"],
                    task_id=task["task_id"],
                    conversation_ref=row["conversation_url"],
                    status=WorkerLeaseStatus.SUPERSEDED,
                    registered_at=float(row["started_at"]),
                    last_heartbeat_at=row["last_seen_at"],
                    ended_at=float(row["ended_at"]),
                )
            )
        elif status == WorkerStatus.DEAD:
            if row["ended_at"] is None:
                raise WorkerProtocolPersistenceError(
                    "legacy DEAD worker lacks terminal ended_at evidence"
                )
            leases.append(
                WorkerLease(
                    worker_id=row["worker_id"],
                    task_id=task["task_id"],
                    conversation_ref=row["conversation_url"],
                    status=WorkerLeaseStatus.ABANDONED,
                    registered_at=float(row["started_at"]),
                    last_heartbeat_at=row["last_seen_at"],
                    ended_at=float(row["ended_at"]),
                )
            )
        else:
            raise WorkerProtocolPersistenceError(
                f"unsupported legacy worker status for bootstrap: {status.value}"
            )

    task_status = DurableTaskStatus.OPEN
    completed_at = None
    completion_ref = None
    if legacy_state == SupervisorState.COMPLETED:
        task_status = DurableTaskStatus.COMPLETED
        completed_at = float(task["updated_at"])
        completion_ref = "legacy:supervisor-state:COMPLETED"

    state = WorkerTaskState(
        lineage=lineage,
        revision=0,
        generation=generation,
        task_status=task_status,
        current_worker_id=current_worker_id,
        workers=tuple(sorted(leases, key=lambda lease: lease.worker_id)),
        completed_at=completed_at,
        completion_ref=completion_ref,
    )
    try:
        validate_state(state)
    except ProtocolInvariantError as exc:
        raise WorkerProtocolPersistenceError(
            f"legacy worker bootstrap is not a valid protocol snapshot: {exc}"
        ) from exc
    return state


def _resolve_lineage(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    parent_task_id: str | None,
    root_task_id: str | None,
    child_key: str | None,
) -> TaskLineage:
    child_key = _optional_nonempty(child_key, "child_key")
    if parent_task_id is None:
        if root_task_id not in (None, task_id):
            raise WorkerProtocolPersistenceError(
                "root protocol task must name itself as root_task_id"
            )
        return TaskLineage(task_id, None, task_id, child_key)

    parent_task_id = parent_task_id.strip()
    if not parent_task_id or parent_task_id == task_id:
        raise WorkerProtocolPersistenceError("parent_task_id must name a different durable task")
    if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (parent_task_id,)).fetchone() is None:
        raise WorkerProtocolPersistenceError("parent_task_id does not exist")

    parent_protocol = conn.execute(
        "SELECT root_task_id FROM worker_protocol_tasks WHERE task_id = ?",
        (parent_task_id,),
    ).fetchone()
    if root_task_id is None:
        if parent_protocol is None:
            raise WorkerProtocolPersistenceError(
                "child bootstrap requires root_task_id until parent protocol state exists"
            )
        root_task_id = parent_protocol["root_task_id"]
    root_task_id = root_task_id.strip()
    if not root_task_id or root_task_id == task_id:
        raise WorkerProtocolPersistenceError("child root_task_id must name an ancestor task")
    if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (root_task_id,)).fetchone() is None:
        raise WorkerProtocolPersistenceError("root_task_id does not exist")
    if parent_protocol is not None and parent_protocol["root_task_id"] != root_task_id:
        raise WorkerProtocolPersistenceError("child root_task_id disagrees with parent lineage")
    return TaskLineage(task_id, parent_task_id, root_task_id, child_key)


def _require_requested_lineage(
    state: WorkerTaskState,
    *,
    parent_task_id: str | None,
    root_task_id: str | None,
    child_key: str | None,
) -> None:
    requested = (parent_task_id, root_task_id, child_key)
    if requested == (None, None, None):
        return
    actual = (
        state.lineage.parent_task_id,
        state.lineage.root_task_id,
        state.lineage.child_key,
    )
    normalized_parent = parent_task_id.strip() if parent_task_id is not None else None
    normalized_root = root_task_id.strip() if root_task_id is not None else None
    if normalized_root is None:
        normalized_root = state.lineage.root_task_id
    normalized_child = child_key.strip() if child_key is not None else None
    expected = (normalized_parent, normalized_root, normalized_child)
    if actual != expected:
        raise WorkerProtocolPersistenceError(
            f"protocol lineage already initialized as {actual!r}, not {expected!r}"
        )


def _optional_nonempty(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise WorkerProtocolPersistenceError(f"{name} must be non-empty when supplied")
    return value


def _insert_protocol_snapshot(conn: sqlite3.Connection, state: WorkerTaskState) -> None:
    conn.execute(
        """INSERT INTO worker_protocol_tasks
           (task_id, revision, generation, task_status, current_worker_id,
            handoff_target_worker_id, handoff_requested_at, parent_task_id, root_task_id,
            child_key, completed_at, completion_ref)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        _task_values(state),
    )
    for lease in state.workers:
        _upsert_protocol_lease(conn, lease)


def _load_worker_protocol_locked(
    conn: sqlite3.Connection,
    task_id: str,
) -> WorkerTaskState:
    row = conn.execute(
        "SELECT * FROM worker_protocol_tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        if conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone() is None:
            raise KeyError(f"unknown task: {task_id}")
        raise WorkerProtocolPersistenceError(
            f"worker protocol is not bootstrapped for task {task_id}"
        )
    lease_rows = conn.execute(
        "SELECT * FROM worker_protocol_leases WHERE task_id = ? ORDER BY worker_id",
        (task_id,),
    ).fetchall()
    try:
        state = WorkerTaskState(
            lineage=TaskLineage(
                task_id=row["task_id"],
                parent_task_id=row["parent_task_id"],
                root_task_id=row["root_task_id"],
                child_key=row["child_key"],
            ),
            revision=int(row["revision"]),
            generation=int(row["generation"]),
            task_status=DurableTaskStatus(row["task_status"]),
            current_worker_id=row["current_worker_id"],
            handoff_target_worker_id=row["handoff_target_worker_id"],
            handoff_requested_at=row["handoff_requested_at"],
            workers=tuple(_lease_from_row(item) for item in lease_rows),
            completed_at=row["completed_at"],
            completion_ref=row["completion_ref"],
        )
        validate_state(state)
    except (ValueError, ProtocolInvariantError) as exc:
        raise WorkerProtocolPersistenceError(
            f"persisted worker protocol is invalid for task {task_id}: {exc}"
        ) from exc
    _validate_lineage_references(conn, state)
    _validate_legacy_consistency(conn, state)
    return state


def _lease_from_row(row: sqlite3.Row) -> WorkerLease:
    return WorkerLease(
        worker_id=row["worker_id"],
        task_id=row["task_id"],
        conversation_ref=row["conversation_ref"],
        status=WorkerLeaseStatus(row["status"]),
        registered_at=float(row["registered_at"]),
        generation=row["generation"],
        claimed_at=row["claimed_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        lease_expires_at=row["lease_expires_at"],
        ended_at=row["ended_at"],
        superseded_by=row["superseded_by"],
    )


def _validate_lineage_references(conn: sqlite3.Connection, state: WorkerTaskState) -> None:
    lineage = state.lineage
    root = conn.execute(
        "SELECT 1 FROM tasks WHERE task_id = ?", (lineage.root_task_id,)
    ).fetchone()
    if root is None:
        raise WorkerProtocolPersistenceError("protocol root_task_id no longer exists")
    if lineage.parent_task_id is not None:
        if conn.execute(
            "SELECT 1 FROM tasks WHERE task_id = ?", (lineage.parent_task_id,)
        ).fetchone() is None:
            raise WorkerProtocolPersistenceError("protocol parent_task_id no longer exists")
        parent = conn.execute(
            "SELECT root_task_id FROM worker_protocol_tasks WHERE task_id = ?",
            (lineage.parent_task_id,),
        ).fetchone()
        if parent is not None and parent["root_task_id"] != lineage.root_task_id:
            raise WorkerProtocolPersistenceError("protocol child root disagrees with parent root")


def _validate_legacy_consistency(conn: sqlite3.Connection, state: WorkerTaskState) -> None:
    task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (state.task_id,)).fetchone()
    if task is None:
        raise WorkerProtocolPersistenceError("canonical task row is missing")
    if task["current_worker_id"] != state.current_worker_id:
        raise WorkerProtocolPersistenceError(
            "tasks.current_worker_id disagrees with persisted worker protocol"
        )
    if state.task_status == DurableTaskStatus.COMPLETED:
        if task["state"] != SupervisorState.COMPLETED.value:
            raise WorkerProtocolPersistenceError(
                "completed protocol task disagrees with canonical task state"
            )
    elif task["state"] in {
        SupervisorState.COMPLETED.value,
        SupervisorState.ABANDONED.value,
    }:
        raise WorkerProtocolPersistenceError(
            "canonical terminal task state disagrees with open worker protocol"
        )

    canonical_rows = conn.execute(
        "SELECT * FROM workers WHERE task_id = ? ORDER BY worker_id", (state.task_id,)
    ).fetchall()
    canonical = {row["worker_id"]: row for row in canonical_rows}
    protocol_ids = {lease.worker_id for lease in state.workers}
    if set(canonical) != protocol_ids:
        raise WorkerProtocolPersistenceError(
            "canonical workers and protocol workers do not have identical identities"
        )
    for lease in state.workers:
        row = canonical[lease.worker_id]
        if row["task_id"] != lease.task_id:
            raise WorkerProtocolPersistenceError("worker durable task identity mismatch")
        if row["conversation_url"] != lease.conversation_ref:
            raise WorkerProtocolPersistenceError("worker conversation reference mismatch")
        if float(row["started_at"]) != float(lease.registered_at):
            raise WorkerProtocolPersistenceError("worker registration timestamp mismatch")
        expected_status = _legacy_status(lease.status).value
        if row["status"] != expected_status:
            raise WorkerProtocolPersistenceError(
                f"legacy worker status {row['status']} disagrees with protocol {lease.status.value}"
            )
        if lease.status in {
            WorkerLeaseStatus.SUPERSEDED,
            WorkerLeaseStatus.COMPLETED,
            WorkerLeaseStatus.ABANDONED,
        } and row["ended_at"] is None:
            raise WorkerProtocolPersistenceError("terminal protocol worker lacks legacy ended_at")
        if lease.superseded_by is not None and lease.superseded_by not in protocol_ids:
            raise WorkerProtocolPersistenceError("superseded_by identifies a foreign worker")


def _persist_decision_locked(
    conn: sqlite3.Connection,
    *,
    previous: WorkerTaskState,
    decision: ProtocolDecision,
    expected_revision: int,
) -> None:
    state = decision.state
    _sync_legacy_workers(conn, state)

    task = conn.execute(
        "SELECT state, updated_at FROM tasks WHERE task_id = ?", (state.task_id,)
    ).fetchone()
    if task is None:
        raise WorkerProtocolPersistenceError("canonical task disappeared during transition")
    mapped_task_state = task["state"]
    if state.task_status == DurableTaskStatus.COMPLETED:
        mapped_task_state = SupervisorState.COMPLETED.value
    updated_at = max(
        float(task["updated_at"]),
        max(float(event.at) for event in decision.events),
    )
    conn.execute(
        """UPDATE tasks
           SET current_worker_id = ?, state = ?, updated_at = ?
           WHERE task_id = ?""",
        (state.current_worker_id, mapped_task_state, updated_at, state.task_id),
    )

    cursor = conn.execute(
        """UPDATE worker_protocol_tasks
           SET revision = ?, generation = ?, task_status = ?, current_worker_id = ?,
               handoff_target_worker_id = ?, handoff_requested_at = ?, parent_task_id = ?,
               root_task_id = ?, child_key = ?, completed_at = ?, completion_ref = ?
           WHERE task_id = ? AND revision = ?""",
        (
            state.revision,
            state.generation,
            state.task_status.value,
            state.current_worker_id,
            state.handoff_target_worker_id,
            state.handoff_requested_at,
            state.lineage.parent_task_id,
            state.lineage.root_task_id,
            state.lineage.child_key,
            state.completed_at,
            state.completion_ref,
            state.task_id,
            expected_revision,
        ),
    )
    if cursor.rowcount != 1:
        raise WorkerProtocolPersistenceError(
            "worker protocol revision compare-and-swap failed during commit"
        )

    for lease in state.workers:
        _upsert_protocol_lease(conn, lease)
    _append_events(conn, decision.events)

    if previous.lineage != state.lineage:
        raise WorkerProtocolPersistenceError("pure transition may not mutate task lineage")


def _sync_legacy_workers(conn: sqlite3.Connection, state: WorkerTaskState) -> None:
    existing_rows = conn.execute(
        "SELECT * FROM workers WHERE task_id = ?", (state.task_id,)
    ).fetchall()
    existing = {row["worker_id"]: row for row in existing_rows}
    for lease in state.workers:
        row = existing.get(lease.worker_id)
        if row is None:
            if lease.status != WorkerLeaseStatus.CANDIDATE or not lease.conversation_ref:
                raise WorkerProtocolPersistenceError(
                    "only a registered protocol candidate may create a canonical worker row"
                )
            conn.execute(
                """INSERT INTO workers
                   (worker_id, task_id, conversation_url, conversation_id, status,
                    started_at, last_seen_at, ended_at)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?)""",
                (
                    lease.worker_id,
                    lease.task_id,
                    lease.conversation_ref,
                    _legacy_status(lease.status).value,
                    lease.registered_at,
                    lease.last_heartbeat_at,
                    lease.ended_at,
                ),
            )
        else:
            if row["task_id"] != lease.task_id:
                raise WorkerProtocolPersistenceError("worker id already belongs to another task")
            if row["conversation_url"] != lease.conversation_ref:
                raise WorkerProtocolPersistenceError(
                    "protocol transition may not change canonical conversation URL"
                )
            conn.execute(
                """UPDATE workers
                   SET status = ?, last_seen_at = ?, ended_at = ?
                   WHERE worker_id = ? AND task_id = ?""",
                (
                    _legacy_status(lease.status).value,
                    lease.last_heartbeat_at,
                    lease.ended_at,
                    lease.worker_id,
                    state.task_id,
                ),
            )
        if lease.status != WorkerLeaseStatus.ACTIVE:
            conn.execute(
                "DELETE FROM worker_window_bindings WHERE worker_id = ?", (lease.worker_id,)
            )

    canonical_ids = {
        row["worker_id"]
        for row in conn.execute(
            "SELECT worker_id FROM workers WHERE task_id = ?", (state.task_id,)
        ).fetchall()
    }
    protocol_ids = {lease.worker_id for lease in state.workers}
    if canonical_ids != protocol_ids:
        raise WorkerProtocolPersistenceError(
            "protocol transition cannot ignore or delete canonical worker identities"
        )


def _legacy_status(status: WorkerLeaseStatus) -> WorkerStatus:
    if status == WorkerLeaseStatus.CANDIDATE:
        return WorkerStatus.PARKED
    if status == WorkerLeaseStatus.ACTIVE:
        return WorkerStatus.ACTIVE
    if status == WorkerLeaseStatus.SUPERSEDED:
        return WorkerStatus.SUPERSEDED
    if status in {WorkerLeaseStatus.COMPLETED, WorkerLeaseStatus.ABANDONED}:
        return WorkerStatus.DEAD
    raise WorkerProtocolPersistenceError(f"unmapped protocol worker status: {status}")


def _upsert_protocol_lease(conn: sqlite3.Connection, lease: WorkerLease) -> None:
    if not lease.conversation_ref:
        raise WorkerProtocolPersistenceError(
            "persisted worker protocol requires a canonical conversation reference"
        )
    conn.execute(
        """INSERT INTO worker_protocol_leases
           (worker_id, task_id, conversation_ref, status, registered_at, generation,
            claimed_at, last_heartbeat_at, lease_expires_at, ended_at, superseded_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(worker_id) DO UPDATE SET
               task_id = excluded.task_id,
               conversation_ref = excluded.conversation_ref,
               status = excluded.status,
               registered_at = excluded.registered_at,
               generation = excluded.generation,
               claimed_at = excluded.claimed_at,
               last_heartbeat_at = excluded.last_heartbeat_at,
               lease_expires_at = excluded.lease_expires_at,
               ended_at = excluded.ended_at,
               superseded_by = excluded.superseded_by""",
        (
            lease.worker_id,
            lease.task_id,
            lease.conversation_ref,
            lease.status.value,
            lease.registered_at,
            lease.generation,
            lease.claimed_at,
            lease.last_heartbeat_at,
            lease.lease_expires_at,
            lease.ended_at,
            lease.superseded_by,
        ),
    )


def _append_events(conn: sqlite3.Connection, events: tuple[ProtocolEvent, ...]) -> None:
    by_revision: dict[int, int] = {}
    for event in events:
        index = by_revision.get(event.revision, 0)
        by_revision[event.revision] = index + 1
        conn.execute(
            """INSERT INTO worker_protocol_events
               (task_id, revision, event_index, kind, at, worker_id, generation,
                related_worker_id, ref)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.task_id,
                event.revision,
                index,
                event.kind.value,
                event.at,
                event.worker_id,
                event.generation,
                event.related_worker_id,
                event.ref,
            ),
        )


def _task_values(state: WorkerTaskState) -> tuple[object, ...]:
    return (
        state.task_id,
        state.revision,
        state.generation,
        state.task_status.value,
        state.current_worker_id,
        state.handoff_target_worker_id,
        state.handoff_requested_at,
        state.lineage.parent_task_id,
        state.lineage.root_task_id,
        state.lineage.child_key,
        state.completed_at,
        state.completion_ref,
    )
