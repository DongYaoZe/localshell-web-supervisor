from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .actions import ActionAcknowledgement, ActionAttempt, ActionAttemptState, UNRESOLVED_ACTION_STATES
from . import worker_persistence, worker_protocol as worker_protocol_model
from .db import connect
from .models import (
    BrowserObservation,
    LsmObservation,
    NetworkObservation,
    PageCapabilityKind,
    PageCapabilityRecord,
    ProbeMutationKind,
    ProbeMutationOperation,
    ProbeMutationState,
    ProbeWindowSlotBinding,
    ReconciliationRecord,
    SupervisorState,
    TaskRecord,
    WorkspaceObservation,
    WorkerRecord,
    WorkerStatus,
    WorkerWindowBinding,
)
from .probe_ops import (
    UNRESOLVED_PROBE_MUTATION_STATES,
    ProbeMutationObservation,
    decide_probe_reconciliation,
    slot_from_snapshot,
)

OBSERVATION_RETENTION_PER_ENTITY = 2000


class Registry:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)

    def close(self) -> None:
        self._conn.close()

    def bootstrap_worker_protocol(
        self,
        task_id: str,
        *,
        parent_task_id: str | None = None,
        root_task_id: str | None = None,
        child_key: str | None = None,
    ) -> worker_protocol_model.WorkerTaskState:
        return worker_persistence.bootstrap_worker_protocol(
            self._conn,
            task_id,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            child_key=child_key,
        )

    def load_worker_protocol(self, task_id: str) -> worker_protocol_model.WorkerTaskState:
        return worker_persistence.load_worker_protocol(self._conn, task_id)

    def worker_protocol_events(
        self, task_id: str, *, limit: int = 200
    ) -> tuple[worker_protocol_model.ProtocolEvent, ...]:
        return worker_persistence.worker_protocol_events(self._conn, task_id, limit=limit)

    def protocol_register_worker(
        self,
        task_id: str,
        conversation_url: str,
        *,
        expected_revision: int,
        worker_id: str | None = None,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        conversation_url = str(conversation_url).strip()
        if not conversation_url:
            raise ValueError("conversation_url must be non-empty")
        worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.register_worker(
                state,
                worker_id,
                conversation_ref=conversation_url,
                now=now,
                expected_revision=expected_revision,
            ),
        )

    def protocol_claim_worker(
        self,
        task_id: str,
        worker_id: str,
        *,
        expected_revision: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.claim_worker(
                state,
                worker_id,
                now=now,
                lease_seconds=lease_seconds,
                expected_revision=expected_revision,
            ),
        )

    def protocol_heartbeat_worker(
        self,
        task_id: str,
        worker_id: str,
        *,
        generation: int,
        expected_revision: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.heartbeat_worker(
                state,
                worker_id,
                generation=generation,
                now=now,
                lease_seconds=lease_seconds,
                expected_revision=expected_revision,
            ),
        )

    def protocol_request_handoff(
        self,
        task_id: str,
        worker_id: str,
        target_worker_id: str,
        *,
        generation: int,
        expected_revision: int,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.request_handoff(
                state,
                worker_id,
                target_worker_id,
                generation=generation,
                now=now,
                expected_revision=expected_revision,
            ),
        )

    def protocol_takeover_worker(
        self,
        task_id: str,
        candidate_worker_id: str,
        *,
        expected_revision: int,
        lease_seconds: float,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.takeover_worker(
                state,
                candidate_worker_id,
                now=now,
                lease_seconds=lease_seconds,
                expected_revision=expected_revision,
            ),
        )

    def protocol_complete_worker(
        self,
        task_id: str,
        worker_id: str,
        *,
        generation: int,
        expected_revision: int,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.complete_worker(
                state,
                worker_id,
                generation=generation,
                now=now,
                expected_revision=expected_revision,
            ),
        )

    def protocol_abandon_worker(
        self,
        task_id: str,
        worker_id: str,
        *,
        generation: int,
        expected_revision: int,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.abandon_worker(
                state,
                worker_id,
                generation=generation,
                now=now,
                expected_revision=expected_revision,
            ),
        )

    def protocol_complete_task(
        self,
        task_id: str,
        *,
        completion_ref: str,
        expected_revision: int,
        now: float | None = None,
    ) -> worker_protocol_model.ProtocolDecision:
        now = time.time() if now is None else float(now)
        return worker_persistence.apply_worker_protocol_transition(
            self._conn,
            task_id,
            expected_revision=expected_revision,
            transition=lambda state: worker_protocol_model.complete_task(
                state,
                completion_ref=completion_ref,
                now=now,
                expected_revision=expected_revision,
            ),
        )

    def register_task(
        self,
        *,
        task_id: str | None,
        project: str,
        objective: str,
        cwd: str,
        lsm_session_id: str | None = None,
        conversation_url: str | None = None,
        conversation_id: str | None = None,
    ) -> TaskRecord:
        now = time.time()
        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            """INSERT INTO tasks
               (task_id, project, objective, cwd, state, lsm_session_id,
                checkpoint_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
            (
                task_id,
                project,
                objective,
                cwd,
                SupervisorState.QUEUED.value,
                lsm_session_id,
                now,
                now,
            ),
        )
        self._conn.commit()
        task = self.get_task(task_id)
        if conversation_url:
            self.add_worker(
                task_id,
                conversation_url,
                conversation_id=conversation_id,
                make_current=True,
            )
            task = self.get_task(task_id)
        return task

    def get_task(self, task_id: str) -> TaskRecord:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        return TaskRecord(
            task_id=row["task_id"],
            project=row["project"],
            objective=row["objective"],
            cwd=row["cwd"],
            state=SupervisorState(row["state"]),
            lsm_session_id=row["lsm_session_id"],
            checkpoint=json.loads(row["checkpoint_json"] or "{}"),
            current_worker_id=row["current_worker_id"],
            recovery_attempts=row["recovery_attempts"],
            max_recovery_attempts=row["max_recovery_attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_tasks(self) -> list[TaskRecord]:
        rows = self._conn.execute("SELECT task_id FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self.get_task(row["task_id"]) for row in rows]

    def update_state(self, task_id: str, state: SupervisorState) -> None:
        if worker_persistence.protocol_state_exists(self._conn, task_id):
            protocol = self.load_worker_protocol(task_id)
            if protocol.task_status == worker_protocol_model.DurableTaskStatus.COMPLETED:
                if state != SupervisorState.COMPLETED:
                    raise RuntimeError(
                        "completed worker protocol task cannot be reopened by legacy update_state"
                    )
            elif state in {SupervisorState.COMPLETED, SupervisorState.ABANDONED}:
                raise RuntimeError(
                    "use worker-protocol task semantics for a protocol-initialized durable task"
                )
        self._conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
            (state.value, time.time(), task_id),
        )
        self._conn.commit()

    def set_checkpoint(self, task_id: str, checkpoint: dict) -> None:
        self._conn.execute(
            "UPDATE tasks SET checkpoint_json = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(checkpoint, ensure_ascii=False), time.time(), task_id),
        )
        self._conn.commit()

    def record_reconciliation(self, record: ReconciliationRecord) -> None:
        self.get_task(record.task_id)
        payload = asdict(record)
        self._conn.execute(
            """INSERT INTO reconciliation_records
               (reconcile_id, task_id, created_at, state, confidence, fence_token, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                record.reconcile_id,
                record.task_id,
                record.created_at,
                record.state,
                record.confidence,
                record.fence_token,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def latest_reconciliation(self, task_id: str) -> ReconciliationRecord | None:
        self.get_task(task_id)
        row = self._conn.execute(
            """SELECT payload_json FROM reconciliation_records
               WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return ReconciliationRecord(**json.loads(row["payload_json"]))

    def reconciliation_history(self, task_id: str, limit: int = 20) -> list[ReconciliationRecord]:
        self.get_task(task_id)
        rows = self._conn.execute(
            """SELECT payload_json FROM reconciliation_records
               WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT ?""",
            (task_id, max(1, min(limit, 200))),
        ).fetchall()
        return [ReconciliationRecord(**json.loads(row["payload_json"])) for row in rows]

    def record_action_attempt(self, attempt: ActionAttempt) -> None:
        self.get_task(attempt.task_id)
        worker = self.get_worker(attempt.worker_id)
        if worker.task_id != attempt.task_id:
            raise ValueError("action attempt worker belongs to a different task")
        unresolved = self.unresolved_action_attempt(attempt.task_id)
        if unresolved is not None and unresolved.attempt_id != attempt.attempt_id:
            raise RuntimeError(
                f"task {attempt.task_id} already has unresolved action {unresolved.attempt_id} "
                f"in state {unresolved.state.value}"
            )
        payload = asdict(attempt)
        payload["state"] = attempt.state.value
        self._conn.execute(
            """INSERT INTO action_attempts
               (attempt_id, task_id, worker_id, state, created_at, updated_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt.attempt_id,
                attempt.task_id,
                attempt.worker_id,
                attempt.state.value,
                attempt.created_at,
                attempt.updated_at,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def record_recovery_action_attempt(self, attempt: ActionAttempt) -> None:
        """Atomically persist an ARMED recovery action and consume one recovery budget slot."""
        if attempt.state != ActionAttemptState.ARMED:
            raise ValueError("recovery action must be ARMED before durable recording")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            task_row = self._conn.execute(
                "SELECT current_worker_id, recovery_attempts, max_recovery_attempts FROM tasks WHERE task_id = ?",
                (attempt.task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {attempt.task_id}")
            if int(task_row["recovery_attempts"]) >= int(task_row["max_recovery_attempts"]):
                raise RuntimeError("recovery attempt budget is exhausted")
            worker_row = self._conn.execute(
                "SELECT task_id, status FROM workers WHERE worker_id = ?", (attempt.worker_id,)
            ).fetchone()
            if worker_row is None:
                raise KeyError(f"unknown worker: {attempt.worker_id}")
            if worker_row["task_id"] != attempt.task_id:
                raise ValueError("action attempt worker belongs to a different task")
            if task_row["current_worker_id"] != attempt.worker_id or worker_row["status"] != WorkerStatus.ACTIVE.value:
                raise RuntimeError("recovery action worker is no longer the active current worker")
            states = tuple(state.value for state in UNRESOLVED_ACTION_STATES)
            placeholders = ",".join("?" for _ in states)
            unresolved = self._conn.execute(
                f"SELECT attempt_id FROM action_attempts WHERE task_id = ? AND state IN ({placeholders}) LIMIT 1",
                (attempt.task_id, *states),
            ).fetchone()
            if unresolved is not None:
                raise RuntimeError(
                    f"task {attempt.task_id} already has unresolved action {unresolved['attempt_id']}"
                )
            payload = asdict(attempt)
            payload["state"] = attempt.state.value
            self._conn.execute(
                """INSERT INTO action_attempts
                   (attempt_id, task_id, worker_id, state, created_at, updated_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.worker_id,
                    attempt.state.value,
                    attempt.created_at,
                    attempt.updated_at,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.execute(
                """UPDATE tasks
                   SET recovery_attempts = recovery_attempts + 1, updated_at = ?
                   WHERE task_id = ?""",
                (attempt.created_at, attempt.task_id),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def get_action_attempt(self, attempt_id: str) -> ActionAttempt:
        row = self._conn.execute(
            "SELECT payload_json FROM action_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown action attempt: {attempt_id}")
        payload = json.loads(row["payload_json"])
        payload["state"] = ActionAttemptState(payload["state"])
        return ActionAttempt(**payload)

    def action_attempts(self, task_id: str, limit: int = 20) -> list[ActionAttempt]:
        self.get_task(task_id)
        rows = self._conn.execute(
            """SELECT attempt_id FROM action_attempts WHERE task_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (task_id, max(1, min(limit, 200))),
        ).fetchall()
        return [self.get_action_attempt(row["attempt_id"]) for row in rows]

    def unresolved_action_attempt(self, task_id: str) -> ActionAttempt | None:
        self.get_task(task_id)
        states = tuple(state.value for state in UNRESOLVED_ACTION_STATES)
        placeholders = ",".join("?" for _ in states)
        row = self._conn.execute(
            f"""SELECT attempt_id FROM action_attempts
                WHERE task_id = ? AND state IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            (task_id, *states),
        ).fetchone()
        return self.get_action_attempt(row["attempt_id"]) if row is not None else None

    def _replace_action_attempt(self, attempt: ActionAttempt) -> ActionAttempt:
        payload = asdict(attempt)
        payload["state"] = attempt.state.value
        cursor = self._conn.execute(
            """UPDATE action_attempts SET state = ?, updated_at = ?, payload_json = ?
               WHERE attempt_id = ?""",
            (
                attempt.state.value,
                attempt.updated_at,
                json.dumps(payload, ensure_ascii=False),
                attempt.attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise KeyError(f"unknown action attempt: {attempt.attempt_id}")
        self._conn.commit()
        return self.get_action_attempt(attempt.attempt_id)

    def mark_action_submitted(
        self,
        attempt_id: str,
        *,
        transport_name: str,
        submitted_at: float | None = None,
    ) -> ActionAttempt:
        attempt = self.get_action_attempt(attempt_id)
        if attempt.state != ActionAttemptState.ARMED:
            raise RuntimeError(f"action {attempt_id} is not ARMED")
        now = time.time() if submitted_at is None else float(submitted_at)
        attempt.state = ActionAttemptState.SUBMITTED
        attempt.transport_name = transport_name
        attempt.submitted_at = now
        attempt.updated_at = now
        return self._replace_action_attempt(attempt)

    def mark_action_reconcile_required(
        self,
        attempt_id: str,
        *,
        error: str,
        transport_name: str | None = None,
        now: float | None = None,
    ) -> ActionAttempt:
        attempt = self.get_action_attempt(attempt_id)
        if attempt.state not in UNRESOLVED_ACTION_STATES:
            raise RuntimeError(f"action {attempt_id} is already terminal")
        ts = time.time() if now is None else float(now)
        attempt.state = ActionAttemptState.RECONCILE_REQUIRED
        attempt.transport_name = transport_name or attempt.transport_name
        attempt.last_error = str(error)[:1000]
        attempt.updated_at = ts
        return self._replace_action_attempt(attempt)

    def fail_action_attempt(
        self,
        attempt_id: str,
        *,
        error: str,
        transport_name: str | None = None,
        now: float | None = None,
    ) -> ActionAttempt:
        """Mark terminal failure only when the caller can prove no side effect occurred."""
        attempt = self.get_action_attempt(attempt_id)
        if attempt.state not in UNRESOLVED_ACTION_STATES:
            raise RuntimeError(f"action {attempt_id} is already terminal")
        ts = time.time() if now is None else float(now)
        attempt.state = ActionAttemptState.FAILED
        attempt.transport_name = transport_name or attempt.transport_name
        attempt.last_error = str(error)[:1000]
        attempt.updated_at = ts
        return self._replace_action_attempt(attempt)

    def acknowledge_action(
        self,
        acknowledgement: ActionAcknowledgement,
    ) -> ActionAttempt:
        from .actions import validate_acknowledgement

        attempt = self.get_action_attempt(acknowledgement.attempt_id)
        validate_acknowledgement(attempt, acknowledgement)
        attempt.state = ActionAttemptState.ACKNOWLEDGED
        attempt.acknowledged_at = acknowledgement.observed_at
        attempt.acknowledgement_kind = acknowledgement.kind
        attempt.acknowledgement_hash = acknowledgement.evidence_hash
        if acknowledgement.text_signature:
            attempt.metadata["ack_uia_text_signature"] = acknowledgement.text_signature
        attempt.last_error = None
        attempt.updated_at = acknowledgement.observed_at
        return self._replace_action_attempt(attempt)

    def record_action_ack_browser_signature(
        self,
        attempt_id: str,
        *,
        message_signature: str,
    ) -> ActionAttempt:
        """Attach the comparable normal-browser signature after a positive ACK.

        The nonce ACK observer and normal UIA browser observer intentionally hash different
        bounded surfaces. Replay suppression therefore stores the normal browser signature
        separately after the action is already ACKNOWLEDGED.
        """
        signature = str(message_signature).strip()
        if not signature:
            raise ValueError("message_signature must be non-empty")
        attempt = self.get_action_attempt(attempt_id)
        if attempt.state != ActionAttemptState.ACKNOWLEDGED:
            raise RuntimeError("browser acknowledgement signature requires ACKNOWLEDGED action")
        attempt.metadata["ack_browser_signature"] = signature
        return self._replace_action_attempt(attempt)

    def cancel_action_attempt(
        self,
        attempt_id: str,
        *,
        reason: str,
        now: float | None = None,
    ) -> ActionAttempt:
        attempt = self.get_action_attempt(attempt_id)
        if attempt.state not in UNRESOLVED_ACTION_STATES:
            raise RuntimeError(f"action {attempt_id} is already terminal")
        ts = time.time() if now is None else float(now)
        attempt.state = ActionAttemptState.CANCELLED
        attempt.last_error = str(reason)[:1000]
        attempt.updated_at = ts
        return self._replace_action_attempt(attempt)

    def record_recovery_event(
        self,
        task_id: str,
        *,
        action: str,
        safe_to_dispatch: bool,
        reason: str,
        payload: dict,
    ) -> None:
        self.get_task(task_id)
        self._conn.execute(
            """INSERT INTO recovery_events
               (task_id, created_at, action, safe_to_dispatch, reason, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                time.time(),
                action,
                int(safe_to_dispatch),
                reason,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def recovery_history(self, task_id: str, limit: int = 20) -> list[dict]:
        self.get_task(task_id)
        rows = self._conn.execute(
            """SELECT created_at, action, safe_to_dispatch, reason, payload_json
               FROM recovery_events WHERE task_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (task_id, max(1, min(limit, 200))),
        ).fetchall()
        return [
            {
                "created_at": row["created_at"],
                "action": row["action"],
                "safe_to_dispatch": bool(row["safe_to_dispatch"]),
                "reason": row["reason"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def acquire_watchdog_lease(
        self,
        *,
        name: str,
        owner_id: str,
        pid: int,
        host: str,
        ttl_s: float,
        now: float | None = None,
    ) -> tuple[bool, dict]:
        now = time.time() if now is None else float(now)
        expires_at = now + max(1.0, float(ttl_s))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM watchdog_leases WHERE name = ?", (name,)
            ).fetchone()
            if (
                row is not None
                and row["owner_id"] != owner_id
                and float(row["expires_at"]) > now
            ):
                current = dict(row)
                self._conn.rollback()
                return False, current
            started_at = (
                float(row["started_at"])
                if row is not None and row["owner_id"] == owner_id
                else now
            )
            self._conn.execute(
                """INSERT INTO watchdog_leases
                   (name, owner_id, pid, host, started_at, heartbeat_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     owner_id = excluded.owner_id,
                     pid = excluded.pid,
                     host = excluded.host,
                     started_at = excluded.started_at,
                     heartbeat_at = excluded.heartbeat_at,
                     expires_at = excluded.expires_at""",
                (name, owner_id, int(pid), host, started_at, now, expires_at),
            )
            self._conn.commit()
            return True, {
                "name": name,
                "owner_id": owner_id,
                "pid": int(pid),
                "host": host,
                "started_at": started_at,
                "heartbeat_at": now,
                "expires_at": expires_at,
            }
        except BaseException:
            self._conn.rollback()
            raise

    def heartbeat_watchdog_lease(
        self,
        *,
        name: str,
        owner_id: str,
        ttl_s: float,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else float(now)
        cursor = self._conn.execute(
            """UPDATE watchdog_leases
               SET heartbeat_at = ?, expires_at = ?
               WHERE name = ? AND owner_id = ?""",
            (now, now + max(1.0, float(ttl_s)), name, owner_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def release_watchdog_lease(self, *, name: str, owner_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM watchdog_leases WHERE name = ? AND owner_id = ?",
            (name, owner_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def watchdog_lease(self, name: str = "default") -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM watchdog_leases WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row is not None else None

    def request_watchdog_stop(
        self,
        *,
        name: str = "default",
        grace_s: float = 60.0,
        now: float | None = None,
    ) -> dict | None:
        """Fence a running watchdog so it exits by losing its next heartbeat lease.

        The stop owner remains fresh during the grace period, preventing a replacement
        watchdog from starting before the old PID has exited.
        """
        now = time.time() if now is None else float(now)
        token = f"stop:{uuid.uuid4().hex}"
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM watchdog_leases WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                self._conn.rollback()
                return None
            previous = dict(row)
            if str(previous["owner_id"]).startswith("stop:") and float(previous["expires_at"]) > now:
                self._conn.rollback()
                return previous
            expires_at = now + max(5.0, float(grace_s))
            self._conn.execute(
                """UPDATE watchdog_leases
                   SET owner_id = ?, heartbeat_at = ?, expires_at = ?
                   WHERE name = ?""",
                (token, now, expires_at, name),
            )
            self._conn.commit()
            return {
                **previous,
                "previous_owner_id": previous["owner_id"],
                "owner_id": token,
                "heartbeat_at": now,
                "expires_at": expires_at,
            }
        except BaseException:
            self._conn.rollback()
            raise

    def clear_watchdog_stop(self, *, name: str, stop_owner_id: str) -> bool:
        if not stop_owner_id.startswith("stop:"):
            raise ValueError("stop_owner_id must be a stop: lease token")
        cursor = self._conn.execute(
            "DELETE FROM watchdog_leases WHERE name = ? AND owner_id = ?",
            (name, stop_owner_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def add_worker(
        self,
        task_id: str,
        conversation_url: str,
        *,
        conversation_id: str | None = None,
        make_current: bool = True,
    ) -> WorkerRecord:
        self.get_task(task_id)
        if worker_persistence.protocol_state_exists(self._conn, task_id):
            raise RuntimeError(
                "use protocol_register_worker for a protocol-initialized durable task"
            )
        now = time.time()
        worker_id = f"worker_{uuid.uuid4().hex[:12]}"
        self._conn.execute(
            """INSERT INTO workers
               (worker_id, task_id, conversation_url, conversation_id, status, started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                worker_id,
                task_id,
                conversation_url,
                conversation_id,
                WorkerStatus.ACTIVE.value,
                now,
            ),
        )
        if make_current:
            old = self._conn.execute(
                "SELECT current_worker_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            if old:
                self._conn.execute(
                    "UPDATE workers SET status = ?, ended_at = ? WHERE worker_id = ?",
                    (WorkerStatus.SUPERSEDED.value, now, old),
                )
                self._conn.execute(
                    "DELETE FROM worker_window_bindings WHERE worker_id = ?",
                    (old,),
                )
            self._conn.execute(
                "UPDATE tasks SET current_worker_id = ?, updated_at = ? WHERE task_id = ?",
                (worker_id, now, task_id),
            )
        self._conn.commit()
        return self.get_worker(worker_id)

    def get_worker(self, worker_id: str) -> WorkerRecord:
        row = self._conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown worker: {worker_id}")
        return WorkerRecord(
            worker_id=row["worker_id"],
            task_id=row["task_id"],
            conversation_url=row["conversation_url"],
            conversation_id=row["conversation_id"],
            status=WorkerStatus(row["status"]),
            started_at=row["started_at"],
            last_seen_at=row["last_seen_at"],
            ended_at=row["ended_at"],
        )

    def workers_for_task(self, task_id: str) -> list[WorkerRecord]:
        rows = self._conn.execute(
            "SELECT worker_id FROM workers WHERE task_id = ? ORDER BY started_at DESC", (task_id,)
        ).fetchall()
        return [self.get_worker(row["worker_id"]) for row in rows]

    def set_worker_status(self, worker_id: str, status: WorkerStatus) -> WorkerRecord:
        """Update durable worker bookkeeping only; does not open/close a browser page."""
        worker = self.get_worker(worker_id)
        if worker_persistence.protocol_state_exists(self._conn, worker.task_id):
            raise RuntimeError(
                "use worker-protocol transitions for a protocol-initialized durable task"
            )
        ended_at = time.time() if status in {WorkerStatus.SUPERSEDED, WorkerStatus.DEAD} else None
        self._conn.execute(
            "UPDATE workers SET status = ?, ended_at = ? WHERE worker_id = ?",
            (status.value, ended_at, worker_id),
        )
        if status != WorkerStatus.ACTIVE:
            self._conn.execute(
                "DELETE FROM worker_window_bindings WHERE worker_id = ?",
                (worker_id,),
            )
        self._conn.commit()
        return self.get_worker(worker_id)

    def bind_worker_window(
        self,
        worker_id: str,
        *,
        window_handle: int,
        browser_pid: int,
        chrome_executable: str,
        conversation_url: str,
        source: str = "windows_uia_chrome",
        observed_at: float | None = None,
        ttl_s: float = 30.0,
    ) -> WorkerWindowBinding:
        """Persist a short-lived exact-window lease for one active worker.

        This is local bookkeeping only. The binding is intentionally ephemeral because HWNDs
        can be reused after a window closes. Callers must still revalidate URL/process/window
        identity immediately before any mutation.
        """
        worker = self.get_worker(worker_id)
        if worker.status != WorkerStatus.ACTIVE:
            raise ValueError("window binding requires an active worker")
        if int(window_handle) <= 0 or int(browser_pid) <= 0:
            raise ValueError("window binding requires positive HWND and browser PID")
        if not str(chrome_executable).strip():
            raise ValueError("window binding requires a Chrome executable path")
        if not str(conversation_url).strip():
            raise ValueError("window binding requires a conversation URL")
        if str(conversation_url).rstrip("/") != str(worker.conversation_url).rstrip("/"):
            raise ValueError("window binding URL must exactly match the registered worker URL")
        observed_at = time.time() if observed_at is None else float(observed_at)
        ttl_s = max(1.0, float(ttl_s))
        existing = self.get_worker_window_binding(worker_id)
        bound_at = existing.bound_at if existing else observed_at
        expires_at = observed_at + ttl_s
        self._conn.execute(
            """INSERT INTO worker_window_bindings
               (worker_id, window_handle, browser_pid, chrome_executable, conversation_url,
                source, bound_at, observed_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(worker_id) DO UPDATE SET
                   window_handle = excluded.window_handle,
                   browser_pid = excluded.browser_pid,
                   chrome_executable = excluded.chrome_executable,
                   conversation_url = excluded.conversation_url,
                   source = excluded.source,
                   observed_at = excluded.observed_at,
                   expires_at = excluded.expires_at""",
            (
                worker_id,
                int(window_handle),
                int(browser_pid),
                str(chrome_executable),
                str(conversation_url),
                str(source),
                bound_at,
                observed_at,
                expires_at,
            ),
        )
        self._conn.commit()
        binding = self.get_worker_window_binding(worker_id)
        assert binding is not None
        return binding

    def get_worker_window_binding(
        self,
        worker_id: str,
        *,
        now: float | None = None,
        require_fresh: bool = False,
    ) -> WorkerWindowBinding | None:
        row = self._conn.execute(
            "SELECT * FROM worker_window_bindings WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        if row is None:
            return None
        binding = WorkerWindowBinding(
            worker_id=row["worker_id"],
            window_handle=int(row["window_handle"]),
            browser_pid=int(row["browser_pid"]),
            chrome_executable=row["chrome_executable"],
            conversation_url=row["conversation_url"],
            source=row["source"],
            bound_at=float(row["bound_at"]),
            observed_at=float(row["observed_at"]),
            expires_at=float(row["expires_at"]),
        )
        if require_fresh and not binding.is_fresh(
            now=time.time() if now is None else float(now)
        ):
            return None
        return binding

    def clear_worker_window_binding(self, worker_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM worker_window_bindings WHERE worker_id = ?",
            (worker_id,),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def record_page_capability(self, capability: PageCapabilityRecord) -> PageCapabilityRecord:
        if not capability.capability_id.strip() or not capability.evidence_digest.strip():
            raise ValueError("page capability requires id and evidence digest")
        self._conn.execute(
            """INSERT INTO page_capabilities
               (capability_id, kind, scope_host, browser_family, browser_major, platform,
                surface, isolation_mode, evaluator_version, evidence_digest,
                source_experiment_id, observed_at, recorded_at, expires_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(capability_id) DO UPDATE SET
                   recorded_at=excluded.recorded_at,
                   expires_at=excluded.expires_at,
                   payload_json=excluded.payload_json""",
            (
                capability.capability_id,
                capability.kind.value,
                capability.scope_host,
                capability.browser_family,
                int(capability.browser_major),
                capability.platform,
                capability.surface,
                capability.isolation_mode,
                capability.evaluator_version,
                capability.evidence_digest,
                capability.source_experiment_id,
                float(capability.observed_at),
                float(capability.recorded_at),
                float(capability.expires_at),
                json.dumps(capability.metadata, ensure_ascii=False, sort_keys=True),
            ),
        )
        self._conn.commit()
        return self.get_page_capability(capability.capability_id)

    def get_page_capability(self, capability_id: str) -> PageCapabilityRecord:
        row = self._conn.execute(
            "SELECT * FROM page_capabilities WHERE capability_id = ?", (capability_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown page capability: {capability_id}")
        return PageCapabilityRecord(
            capability_id=row["capability_id"],
            kind=PageCapabilityKind(row["kind"]),
            scope_host=row["scope_host"],
            browser_family=row["browser_family"],
            browser_major=int(row["browser_major"]),
            platform=row["platform"],
            surface=row["surface"],
            isolation_mode=row["isolation_mode"],
            evaluator_version=row["evaluator_version"],
            evidence_digest=row["evidence_digest"],
            source_experiment_id=row["source_experiment_id"],
            observed_at=float(row["observed_at"]),
            recorded_at=float(row["recorded_at"]),
            expires_at=float(row["expires_at"]),
            metadata=json.loads(row["payload_json"]),
        )

    def page_capabilities(
        self,
        *,
        kind: PageCapabilityKind | None = None,
        limit: int = 100,
    ) -> list[PageCapabilityRecord]:
        if kind is None:
            rows = self._conn.execute(
                "SELECT capability_id FROM page_capabilities "
                "ORDER BY observed_at DESC, capability_id LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT capability_id FROM page_capabilities WHERE kind = ? "
                "ORDER BY observed_at DESC, capability_id LIMIT ?",
                (kind.value, max(1, int(limit))),
            ).fetchall()
        return [self.get_page_capability(row["capability_id"]) for row in rows]

    @staticmethod
    def _probe_operation_payload(operation: ProbeMutationOperation) -> str:
        payload = asdict(operation)
        payload["kind"] = operation.kind.value
        payload["state"] = operation.state.value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def get_probe_mutation_operation(self, operation_id: str) -> ProbeMutationOperation:
        row = self._conn.execute(
            "SELECT payload_json FROM probe_mutation_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown probe mutation operation: {operation_id}")
        payload = json.loads(row["payload_json"])
        payload["kind"] = ProbeMutationKind(payload["kind"])
        payload["state"] = ProbeMutationState(payload["state"])
        return ProbeMutationOperation(**payload)

    def unresolved_probe_mutation_operation(self) -> ProbeMutationOperation | None:
        states = tuple(state.value for state in UNRESOLVED_PROBE_MUTATION_STATES)
        placeholders = ",".join("?" for _ in states)
        row = self._conn.execute(
            f"""SELECT operation_id FROM probe_mutation_operations
                WHERE state IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            states,
        ).fetchone()
        return self.get_probe_mutation_operation(row["operation_id"]) if row is not None else None

    def probe_mutation_operations(self, *, limit: int = 100) -> list[ProbeMutationOperation]:
        rows = self._conn.execute(
            """SELECT operation_id FROM probe_mutation_operations
               ORDER BY created_at DESC, operation_id LIMIT ?""",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [self.get_probe_mutation_operation(row["operation_id"]) for row in rows]

    @staticmethod
    def _probe_slot_snapshot_matches(
        slot: ProbeWindowSlotBinding | None,
        snapshot: dict[str, object] | None,
    ) -> bool:
        prior = slot_from_snapshot(snapshot)
        return slot == prior

    def arm_probe_mutation_operation(
        self,
        operation: ProbeMutationOperation,
    ) -> ProbeMutationOperation:
        """Atomically persist the global probe-mutation fence before mutation authority exists."""
        if operation.state != ProbeMutationState.ARMED:
            raise ValueError("probe mutation must be ARMED before durable recording")
        if not operation.operation_id.strip() or not operation.nonce.strip():
            raise ValueError("probe mutation requires operation id and nonce")
        if not operation.slot_id.strip() or not operation.owner_token.strip():
            raise ValueError("probe mutation requires slot id and owner token")
        if not operation.expected_chrome_executable.strip():
            raise ValueError("probe mutation requires an expected Chrome executable")
        if operation.kind != ProbeMutationKind.CLOSE and not operation.expected_actual_url.strip():
            raise ValueError("OPEN/ROTATE mutation requires an expected tagged target")

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            task_row = self._conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?", (operation.target_task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(f"unknown task: {operation.target_task_id}")
            worker_row = self._conn.execute(
                "SELECT task_id, status, conversation_url FROM workers WHERE worker_id = ?",
                (operation.target_worker_id,),
            ).fetchone()
            if worker_row is None:
                raise KeyError(f"unknown worker: {operation.target_worker_id}")
            if worker_row["task_id"] != operation.target_task_id:
                raise ValueError("probe mutation worker belongs to a different task")
            if worker_row["status"] != WorkerStatus.PARKED.value:
                raise ValueError("probe mutation requires a parked target worker")
            if str(worker_row["conversation_url"]).rstrip("/") != operation.target_conversation_url.rstrip("/"):
                raise ValueError("probe mutation target URL no longer matches the worker")

            states = tuple(state.value for state in UNRESOLVED_PROBE_MUTATION_STATES)
            placeholders = ",".join("?" for _ in states)
            unresolved = self._conn.execute(
                f"""SELECT operation_id FROM probe_mutation_operations
                    WHERE state IN ({placeholders}) LIMIT 1""",
                states,
            ).fetchone()
            if unresolved is not None:
                raise RuntimeError(
                    f"unresolved probe mutation already exists: {unresolved['operation_id']}"
                )

            slot_rows = self._conn.execute(
                "SELECT slot_id FROM probe_window_slots ORDER BY slot_id"
            ).fetchall()
            if len(slot_rows) > 1:
                raise RuntimeError("multiple durable probe slots require reconciliation")
            current = None
            if slot_rows:
                current = self.get_probe_window_slot(str(slot_rows[0]["slot_id"]))

            if operation.kind == ProbeMutationKind.OPEN:
                if operation.prior_slot is not None or current is not None:
                    raise RuntimeError("OPEN mutation requires no existing durable probe slot")
            else:
                if operation.prior_slot is None:
                    raise ValueError("ROTATE/CLOSE mutation requires a prior slot snapshot")
                if not self._probe_slot_snapshot_matches(current, operation.prior_slot):
                    raise RuntimeError("durable probe slot changed before mutation could be armed")

            self._conn.execute(
                """INSERT INTO probe_mutation_operations
                   (operation_id, nonce, kind, state, slot_id, target_task_id,
                    target_worker_id, created_at, updated_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    operation.operation_id,
                    operation.nonce,
                    operation.kind.value,
                    operation.state.value,
                    operation.slot_id,
                    operation.target_task_id,
                    operation.target_worker_id,
                    operation.created_at,
                    operation.updated_at,
                    self._probe_operation_payload(operation),
                ),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.get_probe_mutation_operation(operation.operation_id)

    def _save_probe_mutation_operation(
        self,
        operation: ProbeMutationOperation,
        *,
        commit: bool = True,
    ) -> ProbeMutationOperation:
        cursor = self._conn.execute(
            """UPDATE probe_mutation_operations
               SET state = ?, updated_at = ?, payload_json = ?
               WHERE operation_id = ?""",
            (
                operation.state.value,
                operation.updated_at,
                self._probe_operation_payload(operation),
                operation.operation_id,
            ),
        )
        if cursor.rowcount != 1:
            if commit:
                self._conn.rollback()
            raise KeyError(f"unknown probe mutation operation: {operation.operation_id}")
        if commit:
            self._conn.commit()
            return self.get_probe_mutation_operation(operation.operation_id)
        return operation

    def authorize_probe_close(
        self,
        operation_id: str,
        *,
        now: float | None = None,
    ) -> ProbeMutationOperation:
        """Persist CLOSE intent; only the returned record grants one exact-close attempt."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            operation = self.get_probe_mutation_operation(operation_id)
            if operation.kind not in {ProbeMutationKind.ROTATE, ProbeMutationKind.CLOSE}:
                raise RuntimeError("probe operation does not contain a CLOSE phase")
            if operation.state != ProbeMutationState.ARMED:
                raise RuntimeError(f"probe operation {operation_id} is not ARMED")
            operation.state = ProbeMutationState.CLOSE_SUBMITTED
            operation.updated_at = time.time() if now is None else float(now)
            self._save_probe_mutation_operation(operation, commit=False)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.get_probe_mutation_operation(operation_id)

    def authorize_probe_open(
        self,
        operation_id: str,
        *,
        now: float | None = None,
    ) -> ProbeMutationOperation:
        """Persist OPEN intent; only the returned record grants one launch attempt."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            operation = self.get_probe_mutation_operation(operation_id)
            allowed = (
                operation.kind == ProbeMutationKind.OPEN
                and operation.state == ProbeMutationState.ARMED
            ) or (
                operation.kind == ProbeMutationKind.ROTATE
                and operation.state == ProbeMutationState.READY_TO_OPEN
            )
            if not allowed:
                raise RuntimeError(f"probe operation {operation_id} is not ready for OPEN")
            operation.state = ProbeMutationState.OPEN_SUBMITTED
            operation.resume_state = None
            operation.updated_at = time.time() if now is None else float(now)
            self._save_probe_mutation_operation(operation, commit=False)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.get_probe_mutation_operation(operation_id)

    def _validate_probe_completion_binding(
        self,
        operation: ProbeMutationOperation,
        binding: ProbeWindowSlotBinding,
    ) -> None:
        if binding.slot_id != operation.slot_id:
            raise ValueError("completion binding uses the wrong probe slot")
        if binding.owner_token != operation.owner_token:
            raise ValueError("completion binding uses the wrong ownership token")
        if binding.target_worker_id != operation.target_worker_id:
            raise ValueError("completion binding uses the wrong target worker")
        if binding.target_conversation_url.rstrip("/") != operation.target_conversation_url.rstrip("/"):
            raise ValueError("completion binding uses the wrong target conversation")
        if binding.actual_url.rstrip("/") != operation.expected_actual_url.rstrip("/"):
            raise ValueError("completion binding does not match the expected tagged target")
        if binding.chrome_executable.casefold() != operation.expected_chrome_executable.casefold():
            raise ValueError("completion binding executable identity changed")
        if binding.window_handle <= 0 or binding.browser_pid <= 0:
            raise ValueError("completion binding requires positive HWND and browser PID")

    def _complete_probe_mutation_locked(
        self,
        operation: ProbeMutationOperation,
        *,
        binding: ProbeWindowSlotBinding | None = None,
        ts: float,
        outcome: str | None = None,
        reconciled: bool = False,
    ) -> None:
        """Publish slot/close plus COMPLETED while caller holds BEGIN IMMEDIATE."""
        if operation.kind in {ProbeMutationKind.OPEN, ProbeMutationKind.ROTATE}:
            if binding is None:
                raise ValueError("OPEN/ROTATE completion requires an exact observed binding")
            self._validate_probe_completion_binding(operation, binding)
        elif binding is not None:
            raise ValueError("CLOSE completion must not provide a replacement binding")

        slot_rows = self._conn.execute(
            "SELECT slot_id FROM probe_window_slots ORDER BY slot_id"
        ).fetchall()
        if len(slot_rows) > 1:
            raise RuntimeError("multiple durable probe slots require reconciliation")
        current = None
        if slot_rows:
            current = self.get_probe_window_slot(str(slot_rows[0]["slot_id"]))

        if operation.kind == ProbeMutationKind.OPEN:
            if current is not None:
                raise RuntimeError("durable probe slot appeared while OPEN was unresolved")
        else:
            if not self._probe_slot_snapshot_matches(current, operation.prior_slot):
                raise RuntimeError("durable probe slot changed while mutation was unresolved")

        if operation.kind == ProbeMutationKind.CLOSE:
            self._conn.execute(
                "DELETE FROM probe_window_slots WHERE slot_id = ?", (operation.slot_id,)
            )
        else:
            assert binding is not None
            self._conn.execute(
                """INSERT INTO probe_window_slots
                   (slot_id, owner_token, target_worker_id, target_conversation_url,
                    actual_url, window_handle, browser_pid, chrome_executable, source,
                    bound_at, observed_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slot_id) DO UPDATE SET
                       owner_token=excluded.owner_token,
                       target_worker_id=excluded.target_worker_id,
                       target_conversation_url=excluded.target_conversation_url,
                       actual_url=excluded.actual_url,
                       window_handle=excluded.window_handle,
                       browser_pid=excluded.browser_pid,
                       chrome_executable=excluded.chrome_executable,
                       source=excluded.source,
                       bound_at=excluded.bound_at,
                       observed_at=excluded.observed_at,
                       expires_at=excluded.expires_at""",
                (
                    binding.slot_id,
                    binding.owner_token,
                    binding.target_worker_id,
                    binding.target_conversation_url,
                    binding.actual_url,
                    binding.window_handle,
                    binding.browser_pid,
                    binding.chrome_executable,
                    binding.source,
                    binding.bound_at,
                    binding.observed_at,
                    binding.expires_at,
                ),
            )

        operation.state = ProbeMutationState.COMPLETED
        operation.resume_state = None
        operation.updated_at = ts
        if outcome is not None:
            operation.last_outcome = str(outcome)[:200]
        if reconciled:
            operation.reconcile_attempts += 1
            operation.last_reconcile_at = ts
        operation.last_error = None
        self._save_probe_mutation_operation(operation, commit=False)

    def complete_probe_mutation_operation(
        self,
        operation_id: str,
        *,
        binding: ProbeWindowSlotBinding | None = None,
        now: float | None = None,
        outcome: str | None = None,
    ) -> ProbeMutationOperation:
        """Publish a direct transport result only after the matching durable authority."""
        ts = time.time() if now is None else float(now)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            operation = self.get_probe_mutation_operation(operation_id)
            if operation.state == ProbeMutationState.COMPLETED:
                self._conn.commit()
                return operation
            if operation.state not in UNRESOLVED_PROBE_MUTATION_STATES:
                raise RuntimeError(f"probe operation {operation_id} is already terminal")
            if operation.kind in {ProbeMutationKind.OPEN, ProbeMutationKind.ROTATE}:
                if operation.state != ProbeMutationState.OPEN_SUBMITTED:
                    raise RuntimeError(
                        "replacement binding may complete only after durable OPEN authority"
                    )
            elif operation.state != ProbeMutationState.CLOSE_SUBMITTED:
                raise RuntimeError("CLOSE may complete only after durable CLOSE authority")
            self._complete_probe_mutation_locked(
                operation,
                binding=binding,
                ts=ts,
                outcome=outcome,
                reconciled=False,
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.get_probe_mutation_operation(operation_id)

    def fail_probe_mutation_operation(
        self,
        operation_id: str,
        *,
        reason: str,
        now: float | None = None,
    ) -> ProbeMutationOperation:
        operation = self.get_probe_mutation_operation(operation_id)
        if operation.state != ProbeMutationState.ARMED:
            raise RuntimeError("probe mutation may fail terminally only before mutation authority")
        operation.state = ProbeMutationState.FAILED
        operation.last_error = str(reason)[:1000]
        operation.updated_at = time.time() if now is None else float(now)
        return self._save_probe_mutation_operation(operation)

    def reconcile_probe_mutation_operation(
        self,
        operation_id: str,
        observation: ProbeMutationObservation,
    ) -> ProbeMutationOperation:
        """Apply one read-only observation idempotently to an unresolved operation."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the write lock. Otherwise an observation made against ARMED could
            # overwrite a concurrently persisted OPEN_SUBMITTED/CLOSE_SUBMITTED authority.
            operation = self.get_probe_mutation_operation(operation_id)
            if operation.state == ProbeMutationState.COMPLETED:
                self._conn.commit()
                return operation
            if operation.state not in UNRESOLVED_PROBE_MUTATION_STATES:
                self._conn.commit()
                return operation
            decision = decide_probe_reconciliation(operation, observation)
            if decision.next_state == ProbeMutationState.COMPLETED:
                self._complete_probe_mutation_locked(
                    operation,
                    binding=decision.adopt_binding,
                    ts=max(operation.updated_at, float(observation.observed_at)),
                    outcome=decision.outcome.value,
                    reconciled=True,
                )
            else:
                if decision.next_state == ProbeMutationState.RECONCILE_REQUIRED:
                    if operation.state != ProbeMutationState.RECONCILE_REQUIRED:
                        operation.resume_state = operation.state.value
                    operation.last_error = decision.reason[:1000]
                else:
                    operation.resume_state = None
                    operation.last_error = None
                operation.state = decision.next_state
                operation.updated_at = max(
                    operation.updated_at, float(observation.observed_at)
                )
                operation.reconcile_attempts += 1
                operation.last_reconcile_at = float(observation.observed_at)
                operation.last_outcome = decision.outcome.value
                self._save_probe_mutation_operation(operation, commit=False)
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return self.get_probe_mutation_operation(operation_id)

    def bind_probe_window_slot(
        self,
        slot_id: str,
        *,
        owner_token: str,
        target_worker_id: str,
        target_conversation_url: str,
        actual_url: str,
        window_handle: int,
        browser_pid: int,
        chrome_executable: str,
        source: str = "windows_uia_cws_probe",
        observed_at: float | None = None,
        ttl_s: float = 120.0,
    ) -> ProbeWindowSlotBinding:
        observed_at = time.time() if observed_at is None else float(observed_at)
        ttl_s = max(1.0, float(ttl_s))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            unresolved = self.unresolved_probe_mutation_operation()
            if unresolved is not None:
                raise RuntimeError(
                    f"probe slot is fenced by unresolved mutation {unresolved.operation_id}"
                )
            worker = self.get_worker(target_worker_id)
            if worker.status != WorkerStatus.PARKED:
                raise ValueError("probe slot binding requires a parked worker")
            other_slot = self._conn.execute(
                "SELECT slot_id FROM probe_window_slots WHERE slot_id <> ? LIMIT 1", (slot_id,)
            ).fetchone()
            if other_slot is not None:
                raise ValueError(
                    f"CWS supports only one durable probe slot; existing slot is {other_slot['slot_id']}"
                )
            if not slot_id.strip() or not owner_token.strip():
                raise ValueError("probe slot requires non-empty slot id and owner token")
            if int(window_handle) <= 0 or int(browser_pid) <= 0:
                raise ValueError("probe slot requires positive HWND and browser PID")
            if not chrome_executable.strip():
                raise ValueError("probe slot requires a Chrome executable path")
            if worker.conversation_url.rstrip("/") != target_conversation_url.rstrip("/"):
                raise ValueError("probe slot target URL must match the registered worker URL")
            existing = self.get_probe_window_slot(slot_id)
            bound_at = existing.bound_at if existing else observed_at
            self._conn.execute(
                """INSERT INTO probe_window_slots
                   (slot_id, owner_token, target_worker_id, target_conversation_url, actual_url,
                    window_handle, browser_pid, chrome_executable, source, bound_at, observed_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slot_id) DO UPDATE SET
                       owner_token=excluded.owner_token,
                       target_worker_id=excluded.target_worker_id,
                       target_conversation_url=excluded.target_conversation_url,
                       actual_url=excluded.actual_url,
                       window_handle=excluded.window_handle,
                       browser_pid=excluded.browser_pid,
                       chrome_executable=excluded.chrome_executable,
                       source=excluded.source,
                       observed_at=excluded.observed_at,
                       expires_at=excluded.expires_at""",
                (slot_id, owner_token, target_worker_id, target_conversation_url, actual_url,
                 int(window_handle), int(browser_pid), chrome_executable, source, bound_at,
                 observed_at, observed_at + ttl_s),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        slot = self.get_probe_window_slot(slot_id)
        assert slot is not None
        return slot

    def get_probe_window_slot(
        self, slot_id: str, *, now: float | None = None, require_fresh: bool = False
    ) -> ProbeWindowSlotBinding | None:
        row = self._conn.execute(
            "SELECT * FROM probe_window_slots WHERE slot_id = ?", (slot_id,)
        ).fetchone()
        if row is None:
            return None
        slot = ProbeWindowSlotBinding(
            slot_id=row["slot_id"], owner_token=row["owner_token"],
            target_worker_id=row["target_worker_id"],
            target_conversation_url=row["target_conversation_url"], actual_url=row["actual_url"],
            window_handle=int(row["window_handle"]), browser_pid=int(row["browser_pid"]),
            chrome_executable=row["chrome_executable"], source=row["source"],
            bound_at=float(row["bound_at"]), observed_at=float(row["observed_at"]),
            expires_at=float(row["expires_at"]),
        )
        if require_fresh and not slot.is_fresh(now=time.time() if now is None else float(now)):
            return None
        return slot

    def clear_probe_window_slot(self, slot_id: str) -> bool:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            unresolved = self.unresolved_probe_mutation_operation()
            if unresolved is not None:
                raise RuntimeError(
                    f"probe slot is fenced by unresolved mutation {unresolved.operation_id}"
                )
            cur = self._conn.execute(
                "DELETE FROM probe_window_slots WHERE slot_id = ?", (slot_id,)
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return cur.rowcount == 1

    def probe_window_slots(self) -> list[ProbeWindowSlotBinding]:
        rows = self._conn.execute("SELECT slot_id FROM probe_window_slots ORDER BY slot_id").fetchall()
        return [slot for row in rows if (slot := self.get_probe_window_slot(row["slot_id"])) is not None]

    def track_job(self, task_id: str, job_id: str) -> None:
        self.get_task(task_id)
        self._conn.execute(
            "INSERT OR IGNORE INTO task_jobs(task_id, job_id) VALUES (?, ?)", (task_id, job_id)
        )
        self._conn.commit()

    def tracked_jobs(self, task_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT job_id FROM task_jobs WHERE task_id = ? ORDER BY job_id", (task_id,)
        ).fetchall()
        return [row["job_id"] for row in rows]

    def record_browser_observation(self, obs: BrowserObservation) -> None:
        payload = asdict(obs)
        self._conn.execute(
            "INSERT INTO browser_observations(worker_id, observed_at, payload_json) VALUES (?, ?, ?)",
            (obs.worker_id, obs.observed_at, json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.execute(
            "UPDATE workers SET last_seen_at = ? WHERE worker_id = ?",
            (obs.observed_at, obs.worker_id),
        )
        self._conn.execute(
            """DELETE FROM browser_observations
               WHERE worker_id = ? AND id NOT IN (
                   SELECT id FROM browser_observations
                   WHERE worker_id = ? ORDER BY observed_at DESC, id DESC LIMIT ?
               )""",
            (obs.worker_id, obs.worker_id, OBSERVATION_RETENTION_PER_ENTITY),
        )
        self._conn.commit()

    def latest_browser_observation(self, worker_id: str | None) -> BrowserObservation | None:
        if not worker_id:
            return None
        row = self._conn.execute(
            """SELECT payload_json FROM browser_observations
               WHERE worker_id = ? ORDER BY observed_at DESC LIMIT 1""",
            (worker_id,),
        ).fetchone()
        if row is None:
            return None
        return BrowserObservation(**json.loads(row["payload_json"]))

    def browser_observation_history(
        self,
        worker_id: str | None,
        *,
        limit: int = 20,
    ) -> list[BrowserObservation]:
        if not worker_id:
            return []
        rows = self._conn.execute(
            """SELECT payload_json FROM browser_observations
               WHERE worker_id = ? ORDER BY observed_at DESC, id DESC LIMIT ?""",
            (worker_id, max(1, min(int(limit), 200))),
        ).fetchall()
        return [BrowserObservation(**json.loads(row["payload_json"])) for row in rows]

    def record_network_observation(self, obs: NetworkObservation) -> None:
        payload = asdict(obs)
        self._conn.execute(
            "INSERT INTO network_observations(worker_id, observed_at, payload_json) VALUES (?, ?, ?)",
            (obs.worker_id, obs.observed_at, json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.execute(
            """DELETE FROM network_observations
               WHERE worker_id = ? AND id NOT IN (
                   SELECT id FROM network_observations
                   WHERE worker_id = ? ORDER BY observed_at DESC, id DESC LIMIT ?
               )""",
            (obs.worker_id, obs.worker_id, OBSERVATION_RETENTION_PER_ENTITY),
        )
        self._conn.commit()

    def latest_network_observation(self, worker_id: str | None) -> NetworkObservation | None:
        if not worker_id:
            return None
        row = self._conn.execute(
            """SELECT payload_json FROM network_observations
               WHERE worker_id = ? ORDER BY observed_at DESC LIMIT 1""",
            (worker_id,),
        ).fetchone()
        if row is None:
            return None
        return NetworkObservation(**json.loads(row["payload_json"]))

    def record_lsm_observation(self, obs: LsmObservation) -> None:
        payload = asdict(obs)
        self._conn.execute(
            "INSERT INTO lsm_observations(task_id, observed_at, payload_json) VALUES (?, ?, ?)",
            (obs.task_id, obs.observed_at, json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.execute(
            """DELETE FROM lsm_observations
               WHERE task_id = ? AND id NOT IN (
                   SELECT id FROM lsm_observations
                   WHERE task_id = ? ORDER BY observed_at DESC, id DESC LIMIT ?
               )""",
            (obs.task_id, obs.task_id, OBSERVATION_RETENTION_PER_ENTITY),
        )
        self._conn.commit()

    def latest_lsm_observation(self, task_id: str) -> LsmObservation | None:
        row = self._conn.execute(
            """SELECT payload_json FROM lsm_observations
               WHERE task_id = ? ORDER BY observed_at DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return LsmObservation(**json.loads(row["payload_json"]))

    def record_workspace_observation(self, obs: WorkspaceObservation) -> None:
        payload = asdict(obs)
        self._conn.execute(
            """INSERT INTO workspace_observations(task_id, observed_at, payload_json)
               VALUES (?, ?, ?)""",
            (obs.task_id, obs.observed_at, json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.execute(
            """DELETE FROM workspace_observations
               WHERE task_id = ? AND id NOT IN (
                   SELECT id FROM workspace_observations
                   WHERE task_id = ? ORDER BY observed_at DESC, id DESC LIMIT ?
               )""",
            (obs.task_id, obs.task_id, OBSERVATION_RETENTION_PER_ENTITY),
        )
        self._conn.commit()

    def latest_workspace_observation(self, task_id: str) -> WorkspaceObservation | None:
        row = self._conn.execute(
            """SELECT payload_json FROM workspace_observations
               WHERE task_id = ? ORDER BY observed_at DESC LIMIT 1""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return WorkspaceObservation(**json.loads(row["payload_json"]))
