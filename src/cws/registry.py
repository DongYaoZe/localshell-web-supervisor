from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .actions import ActionAcknowledgement, ActionAttempt, ActionAttemptState, UNRESOLVED_ACTION_STATES
from .db import connect
from .models import (
    BrowserObservation,
    LsmObservation,
    NetworkObservation,
    ReconciliationRecord,
    SupervisorState,
    TaskRecord,
    WorkspaceObservation,
    WorkerRecord,
    WorkerStatus,
)

OBSERVATION_RETENTION_PER_ENTITY = 2000


class Registry:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)

    def close(self) -> None:
        self._conn.close()

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
        attempt.last_error = None
        attempt.updated_at = acknowledgement.observed_at
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
        self.get_worker(worker_id)
        ended_at = time.time() if status in {WorkerStatus.SUPERSEDED, WorkerStatus.DEAD} else None
        self._conn.execute(
            "UPDATE workers SET status = ?, ended_at = ? WHERE worker_id = ?",
            (status.value, ended_at, worker_id),
        )
        self._conn.commit()
        return self.get_worker(worker_id)

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
