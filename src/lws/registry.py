from __future__ import annotations

import json
import hashlib
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .actions import ActionAcknowledgement, ActionAttempt, ActionAttemptState, UNRESOLVED_ACTION_STATES
from . import worker_persistence, worker_protocol as worker_protocol_model
from .db import connect
from .models import (
    BrowserObservation,
    ChildDispatchRecord,
    ChildSpawnAttempt,
    ChildSpawnAttemptState,
    LsmObservation,
    NetworkObservation,
    PageCapabilityKind,
    PageCapabilityRecord,
    ProbeMutationKind,
    ProbeMutationOperation,
    ProbeMutationState,
    ProbeWindowSlotBinding,
    ReconciliationRecord,
    ReplacementAttempt,
    ReplacementAttemptState,
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

    def create_child_dispatch(
        self,
        parent_task_id: str,
        *,
        child_key: str,
        project: str,
        objective: str,
        cwd: str,
        prompt_text: str,
        child_task_id: str | None = None,
        expected_branch: str | None = None,
        base_ref: str | None = None,
        web_project_url: str | None = None,
        metadata: dict | None = None,
        now: float | None = None,
    ) -> ChildDispatchRecord:
        """Atomically persist one parent-to-child work contract and protocol lineage."""

        parent_task_id = str(parent_task_id).strip()
        child_key = str(child_key).strip()
        project = str(project).strip()
        objective = str(objective).strip()
        cwd = str(cwd).strip()
        prompt_text = str(prompt_text)
        expected_branch = _optional_text(expected_branch)
        base_ref = _optional_text(base_ref)
        web_project_url = _optional_text(web_project_url)
        metadata = dict(metadata or {})
        if not parent_task_id or not child_key or not project or not objective or not cwd:
            raise ValueError("parent task, child key, project, objective, and cwd are required")
        if not prompt_text.strip():
            raise ValueError("child dispatch prompt must be non-empty")
        if len(prompt_text.encode("utf-8")) > 256 * 1024:
            raise ValueError("child dispatch prompt exceeds 256 KiB")
        child_task_id = str(child_task_id or f"task_{uuid.uuid4().hex[:12]}").strip()
        if not child_task_id or child_task_id == parent_task_id:
            raise ValueError("child_task_id must identify a different non-empty task")
        timestamp = time.time() if now is None else float(now)
        prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

        if not worker_persistence.protocol_state_exists(self._conn, parent_task_id):
            # A legacy parent with no lineage is a root task. Bootstrap is conservative:
            # ambiguous legacy worker state still fails closed in worker_persistence.
            self.bootstrap_worker_protocol(parent_task_id)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            parent = self._conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?", (parent_task_id,)
            ).fetchone()
            if parent is None:
                raise KeyError(f"unknown parent task: {parent_task_id}")
            parent_protocol = self._conn.execute(
                "SELECT task_status, root_task_id FROM worker_protocol_tasks WHERE task_id = ?",
                (parent_task_id,),
            ).fetchone()
            if parent_protocol is None:
                raise RuntimeError("parent task must have worker protocol state before dispatch")
            if parent_protocol["task_status"] != worker_protocol_model.DurableTaskStatus.OPEN.value:
                raise RuntimeError("completed parent task cannot create new child dispatches")

            existing = self._conn.execute(
                "SELECT * FROM child_dispatches WHERE parent_task_id = ? AND child_key = ?",
                (parent_task_id, child_key),
            ).fetchone()
            if existing is not None:
                record = _child_dispatch_from_row(existing)
                expected = (
                    project,
                    objective,
                    cwd,
                    prompt_sha256,
                    expected_branch,
                    base_ref,
                    web_project_url,
                    metadata,
                )
                child = self._conn.execute(
                    "SELECT project, objective, cwd FROM tasks WHERE task_id = ?",
                    (record.child_task_id,),
                ).fetchone()
                actual = (
                    child["project"] if child else None,
                    child["objective"] if child else None,
                    child["cwd"] if child else None,
                    record.prompt_sha256,
                    record.expected_branch,
                    record.base_ref,
                    record.web_project_url,
                    record.metadata,
                )
                if actual != expected or (child_task_id and child_task_id != record.child_task_id):
                    raise RuntimeError("child key already exists with a different dispatch contract")
                self._conn.rollback()
                return record

            if self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (child_task_id,)
            ).fetchone() is not None:
                raise RuntimeError("child_task_id already exists without this dispatch contract")

            self._conn.execute(
                """INSERT INTO tasks
                   (task_id, project, objective, cwd, state, lsm_session_id,
                    checkpoint_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, '{}', ?, ?)""",
                (
                    child_task_id,
                    project,
                    objective,
                    cwd,
                    SupervisorState.QUEUED.value,
                    timestamp,
                    timestamp,
                ),
            )
            self._conn.execute(
                """INSERT INTO worker_protocol_tasks
                   (task_id, revision, generation, task_status, current_worker_id,
                    handoff_target_worker_id, handoff_requested_at, parent_task_id,
                    root_task_id, child_key, completed_at, completion_ref)
                   VALUES (?, 0, 0, ?, NULL, NULL, NULL, ?, ?, ?, NULL, NULL)""",
                (
                    child_task_id,
                    worker_protocol_model.DurableTaskStatus.OPEN.value,
                    parent_task_id,
                    parent_protocol["root_task_id"],
                    child_key,
                ),
            )
            dispatch_id = f"dispatch_{uuid.uuid4().hex[:16]}"
            self._conn.execute(
                """INSERT INTO child_dispatches
                   (dispatch_id, parent_task_id, child_task_id, child_key, prompt_text,
                    prompt_sha256, expected_branch, base_ref, web_project_url, created_at, updated_at,
                    payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dispatch_id,
                    parent_task_id,
                    child_task_id,
                    child_key,
                    prompt_text,
                    prompt_sha256,
                    expected_branch,
                    base_ref,
                    web_project_url,
                    timestamp,
                    timestamp,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()
            return self.get_child_dispatch(child_task_id)
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def get_child_dispatch(self, child_task_id: str) -> ChildDispatchRecord:
        row = self._conn.execute(
            "SELECT * FROM child_dispatches WHERE child_task_id = ?", (child_task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown child dispatch: {child_task_id}")
        return _child_dispatch_from_row(row)

    def bind_child_lsm_session(self, child_task_id: str, session_id: str) -> TaskRecord:
        """Bind the durable LSM logical session exactly once; replacements reuse it."""

        self.get_child_dispatch(child_task_id)
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must be non-empty")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT lsm_session_id FROM tasks WHERE task_id = ?", (child_task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {child_task_id}")
            existing = row["lsm_session_id"]
            if existing not in (None, session_id):
                raise RuntimeError(
                    "child already belongs to a different durable LSM session; "
                    "replacement workers must reuse the existing logical session"
                )
            if existing is None:
                self._conn.execute(
                    "UPDATE tasks SET lsm_session_id = ?, updated_at = ? WHERE task_id = ?",
                    (session_id, time.time(), child_task_id),
                )
            self._conn.commit()
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        return self.get_task(child_task_id)

    def complete_child_dispatch(
        self,
        child_task_id: str,
        *,
        completion_ref: str,
        now: float | None = None,
    ) -> worker_protocol_model.WorkerTaskState:
        """Finish current child worker, then its durable task, with crash-safe replay semantics."""

        self.get_child_dispatch(child_task_id)
        completion_ref = str(completion_ref).strip()
        if not completion_ref:
            raise ValueError("completion_ref must be non-empty")
        ts = time.time() if now is None else float(now)
        unresolved_action = self.unresolved_action_attempt(child_task_id)
        if unresolved_action is not None:
            raise RuntimeError(
                f"child has unresolved external action {unresolved_action.attempt_id} "
                f"in state {unresolved_action.state.value}"
            )
        unresolved_replacement = self.unresolved_replacement_attempt(child_task_id)
        if unresolved_replacement is not None:
            raise RuntimeError(
                f"child has unresolved replacement {unresolved_replacement.attempt_id} "
                f"in state {unresolved_replacement.state.value}"
            )
        unresolved_spawn = self.unresolved_child_spawn_attempt(child_task_id)
        if unresolved_spawn is not None:
            raise RuntimeError(
                f"child has unresolved spawn {unresolved_spawn.attempt_id} "
                f"in state {unresolved_spawn.state.value}"
            )

        state = self.load_worker_protocol(child_task_id)
        if state.task_status == worker_protocol_model.DurableTaskStatus.COMPLETED:
            if state.completion_ref != completion_ref:
                raise RuntimeError(
                    "child is already completed with a different completion_ref"
                )
            if self.get_task(child_task_id).state != SupervisorState.COMPLETED:
                self.update_state(child_task_id, SupervisorState.COMPLETED)
            return state

        if state.current_worker_id is not None:
            completed_worker = self.protocol_complete_worker(
                child_task_id,
                state.current_worker_id,
                generation=state.generation,
                expected_revision=state.revision,
                now=ts,
            )
            if not completed_worker.accepted:
                raise RuntimeError(
                    f"current child worker completion rejected: {completed_worker.code.value}"
                )
            state = completed_worker.state

        completed_task = self.protocol_complete_task(
            child_task_id,
            completion_ref=completion_ref,
            expected_revision=state.revision,
            now=ts,
        )
        if not completed_task.accepted:
            raise RuntimeError(f"child task completion rejected: {completed_task.code.value}")
        self.update_state(child_task_id, SupervisorState.COMPLETED)
        return completed_task.state

    def child_dispatches_for_parent(self, parent_task_id: str) -> list[ChildDispatchRecord]:
        rows = self._conn.execute(
            """SELECT * FROM child_dispatches WHERE parent_task_id = ?
               ORDER BY created_at, dispatch_id""",
            (parent_task_id,),
        ).fetchall()
        return [_child_dispatch_from_row(row) for row in rows]

    def adopt_child_worker(
        self,
        child_task_id: str,
        conversation_url: str,
        *,
        lease_seconds: float = 7200.0,
        worker_id: str | None = None,
        now: float | None = None,
    ) -> worker_protocol_model.WorkerTaskState:
        """Register/claim one exact child conversation when no other worker owns authority."""

        self.get_child_dispatch(child_task_id)
        conversation_url = str(conversation_url).strip()
        if not conversation_url:
            raise ValueError("conversation_url must be non-empty")
        if float(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        timestamp = time.time() if now is None else float(now)
        state = self.load_worker_protocol(child_task_id)
        matching = [w for w in state.workers if w.conversation_ref == conversation_url]
        if len(matching) > 1:
            raise RuntimeError("multiple protocol workers unexpectedly share the conversation URL")
        if state.current_worker_id is not None:
            if matching and matching[0].worker_id == state.current_worker_id:
                return state
            raise RuntimeError("child already has an authoritative worker; use replacement protocol")
        if matching:
            candidate = matching[0]
            if candidate.status != worker_protocol_model.WorkerLeaseStatus.CANDIDATE:
                raise RuntimeError("existing conversation worker is terminal and cannot be re-adopted")
        else:
            registered = self.protocol_register_worker(
                child_task_id,
                conversation_url,
                expected_revision=state.revision,
                worker_id=worker_id,
                now=timestamp,
            )
            if not registered.accepted:
                raise RuntimeError(f"child worker registration rejected: {registered.code.value}")
            state = registered.state
            candidate = next(w for w in state.workers if w.conversation_ref == conversation_url)
        claimed = self.protocol_claim_worker(
            child_task_id,
            candidate.worker_id,
            expected_revision=state.revision,
            lease_seconds=float(lease_seconds),
            now=timestamp,
        )
        if not claimed.accepted:
            raise RuntimeError(f"child worker claim rejected: {claimed.code.value}")
        return claimed.state

    def register_replacement_candidate(
        self,
        child_task_id: str,
        conversation_url: str,
        *,
        worker_id: str | None = None,
        now: float | None = None,
    ) -> worker_protocol_model.WorkerTaskState:
        """Register an exact replacement conversation as CANDIDATE without granting authority."""

        self.get_child_dispatch(child_task_id)
        conversation_url = str(conversation_url).strip()
        if not conversation_url:
            raise ValueError("conversation_url must be non-empty")
        state = self.load_worker_protocol(child_task_id)
        matching = [worker for worker in state.workers if worker.conversation_ref == conversation_url]
        if matching:
            if len(matching) != 1:
                raise RuntimeError("multiple protocol workers unexpectedly share the conversation URL")
            if matching[0].status != worker_protocol_model.WorkerLeaseStatus.CANDIDATE:
                raise RuntimeError("replacement conversation already belongs to a terminal/active worker")
            return state
        decision = self.protocol_register_worker(
            child_task_id,
            conversation_url,
            worker_id=worker_id,
            expected_revision=state.revision,
            now=now,
        )
        if not decision.accepted:
            raise RuntimeError(f"replacement candidate rejected: {decision.code.value}")
        return decision.state

    def record_replacement_attempt(self, attempt: ReplacementAttempt) -> ReplacementAttempt:
        self.get_child_dispatch(attempt.task_id)
        worker = self.get_worker(attempt.candidate_worker_id)
        if worker.task_id != attempt.task_id:
            raise ValueError("replacement candidate belongs to a different task")
        if attempt.state != ReplacementAttemptState.ARMED:
            raise ValueError("replacement attempt must be ARMED before durable recording")
        unresolved = self.unresolved_replacement_attempt(attempt.task_id)
        if unresolved is not None and unresolved.attempt_id != attempt.attempt_id:
            raise RuntimeError(
                f"task {attempt.task_id} already has unresolved replacement {unresolved.attempt_id}"
            )
        payload = _replacement_attempt_payload(attempt)
        self._conn.execute(
            """INSERT INTO replacement_attempts
               (attempt_id, task_id, candidate_worker_id, state, created_at, updated_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt.attempt_id,
                attempt.task_id,
                attempt.candidate_worker_id,
                attempt.state.value,
                attempt.created_at,
                attempt.updated_at,
                payload,
            ),
        )
        self._conn.commit()
        return self.get_replacement_attempt(attempt.attempt_id)

    def get_replacement_attempt(self, attempt_id: str) -> ReplacementAttempt:
        row = self._conn.execute(
            "SELECT payload_json FROM replacement_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown replacement attempt: {attempt_id}")
        payload = json.loads(row["payload_json"])
        payload["state"] = ReplacementAttemptState(payload["state"])
        return ReplacementAttempt(**payload)

    def unresolved_replacement_attempt(self, task_id: str) -> ReplacementAttempt | None:
        states = (
            ReplacementAttemptState.ARMED.value,
            ReplacementAttemptState.LSM_TAKEOVER_SUBMITTED.value,
            ReplacementAttemptState.RECONCILE_REQUIRED.value,
        )
        row = self._conn.execute(
            """SELECT attempt_id FROM replacement_attempts
               WHERE task_id = ? AND state IN (?, ?, ?)
               ORDER BY created_at DESC LIMIT 1""",
            (task_id, *states),
        ).fetchone()
        return self.get_replacement_attempt(row["attempt_id"]) if row is not None else None

    def replacement_attempts(self, task_id: str, *, limit: int = 50) -> list[ReplacementAttempt]:
        rows = self._conn.execute(
            """SELECT attempt_id FROM replacement_attempts
               WHERE task_id = ? ORDER BY created_at DESC, attempt_id LIMIT ?""",
            (task_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [self.get_replacement_attempt(row["attempt_id"]) for row in rows]

    def update_replacement_attempt(
        self,
        attempt_id: str,
        *,
        state: ReplacementAttemptState,
        new_active_run_id: str | None = None,
        last_error: str | None = None,
        now: float | None = None,
    ) -> ReplacementAttempt:
        attempt = self.get_replacement_attempt(attempt_id)
        allowed = {
            ReplacementAttemptState.ARMED: {
                ReplacementAttemptState.LSM_TAKEOVER_SUBMITTED,
                ReplacementAttemptState.FAILED,
            },
            ReplacementAttemptState.LSM_TAKEOVER_SUBMITTED: {
                ReplacementAttemptState.RECONCILE_REQUIRED,
                ReplacementAttemptState.COMPLETED,
            },
            ReplacementAttemptState.RECONCILE_REQUIRED: {
                ReplacementAttemptState.RECONCILE_REQUIRED,
                ReplacementAttemptState.COMPLETED,
            },
        }
        if state == attempt.state:
            return attempt
        if state not in allowed.get(attempt.state, set()):
            raise RuntimeError(
                f"invalid replacement transition {attempt.state.value} -> {state.value}"
            )
        attempt.state = state
        attempt.updated_at = time.time() if now is None else float(now)
        if new_active_run_id is not None:
            attempt.new_active_run_id = str(new_active_run_id).strip() or None
        if last_error is not None:
            attempt.last_error = str(last_error)[:1000]
        payload = _replacement_attempt_payload(attempt)
        cursor = self._conn.execute(
            """UPDATE replacement_attempts
               SET state = ?, updated_at = ?, payload_json = ? WHERE attempt_id = ?""",
            (attempt.state.value, attempt.updated_at, payload, attempt.attempt_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise KeyError(f"unknown replacement attempt: {attempt_id}")
        self._conn.commit()
        return self.get_replacement_attempt(attempt_id)

    def record_child_spawn_attempt(self, attempt: ChildSpawnAttempt) -> ChildSpawnAttempt:
        self.get_child_dispatch(attempt.child_task_id)
        if attempt.state != ChildSpawnAttemptState.ARMED:
            raise ValueError("child spawn attempt must be ARMED before durable recording")
        if self.unresolved_child_spawn_attempt(attempt.child_task_id) is not None:
            raise RuntimeError("child task already has an unresolved spawn attempt")
        payload = _child_spawn_attempt_payload(attempt)
        self._conn.execute(
            """INSERT INTO child_spawn_attempts
               (attempt_id, child_task_id, state, created_at, updated_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                attempt.attempt_id,
                attempt.child_task_id,
                attempt.state.value,
                attempt.created_at,
                attempt.updated_at,
                payload,
            ),
        )
        self._conn.commit()
        return self.get_child_spawn_attempt(attempt.attempt_id)

    def get_child_spawn_attempt(self, attempt_id: str) -> ChildSpawnAttempt:
        row = self._conn.execute(
            "SELECT payload_json FROM child_spawn_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown child spawn attempt: {attempt_id}")
        payload = json.loads(row["payload_json"])
        payload["state"] = ChildSpawnAttemptState(payload["state"])
        return ChildSpawnAttempt(**payload)

    def annotate_child_spawn_attempt(
        self,
        attempt_id: str,
        *,
        metadata_updates: dict,
        now: float | None = None,
    ) -> ChildSpawnAttempt:
        """Merge durable metadata onto a spawn record without changing its state."""

        attempt = self.get_child_spawn_attempt(attempt_id)
        attempt.metadata = {**attempt.metadata, **dict(metadata_updates)}
        attempt.updated_at = time.time() if now is None else float(now)
        payload = _child_spawn_attempt_payload(attempt)
        cursor = self._conn.execute(
            """UPDATE child_spawn_attempts
               SET updated_at = ?, payload_json = ? WHERE attempt_id = ?""",
            (attempt.updated_at, payload, attempt.attempt_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise KeyError(f"unknown child spawn attempt: {attempt_id}")
        self._conn.commit()
        return self.get_child_spawn_attempt(attempt_id)

    def unresolved_child_spawn_attempt(self, child_task_id: str) -> ChildSpawnAttempt | None:
        states = tuple(
            state.value
            for state in (
                ChildSpawnAttemptState.ARMED,
                ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
                ChildSpawnAttemptState.WINDOW_BOUND,
                ChildSpawnAttemptState.PROMPT_SUBMITTED,
                ChildSpawnAttemptState.RECONCILE_REQUIRED,
            )
        )
        placeholders = ",".join("?" for _ in states)
        row = self._conn.execute(
            f"""SELECT attempt_id FROM child_spawn_attempts
                WHERE child_task_id = ? AND state IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            (child_task_id, *states),
        ).fetchone()
        return self.get_child_spawn_attempt(row["attempt_id"]) if row is not None else None

    def child_spawn_attempts(self, child_task_id: str, *, limit: int = 50) -> list[ChildSpawnAttempt]:
        rows = self._conn.execute(
            """SELECT attempt_id FROM child_spawn_attempts
               WHERE child_task_id = ? ORDER BY created_at DESC, attempt_id LIMIT ?""",
            (child_task_id, max(1, min(int(limit), 500))),
        ).fetchall()
        return [self.get_child_spawn_attempt(row["attempt_id"]) for row in rows]

    def update_child_spawn_attempt(
        self,
        attempt_id: str,
        *,
        state: ChildSpawnAttemptState,
        window_handle: int | None = None,
        browser_pid: int | None = None,
        conversation_url: str | None = None,
        worker_id: str | None = None,
        last_error: str | None = None,
        metadata_updates: dict | None = None,
        now: float | None = None,
    ) -> ChildSpawnAttempt:
        attempt = self.get_child_spawn_attempt(attempt_id)
        allowed = {
            ChildSpawnAttemptState.ARMED: {
                ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
                ChildSpawnAttemptState.FAILED,
            },
            ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED: {
                ChildSpawnAttemptState.WINDOW_BOUND,
                ChildSpawnAttemptState.RECONCILE_REQUIRED,
                ChildSpawnAttemptState.FAILED,
            },
            ChildSpawnAttemptState.WINDOW_BOUND: {
                ChildSpawnAttemptState.PROMPT_SUBMITTED,
                ChildSpawnAttemptState.RECONCILE_REQUIRED,
            },
            ChildSpawnAttemptState.PROMPT_SUBMITTED: {
                ChildSpawnAttemptState.WINDOW_BOUND,
                ChildSpawnAttemptState.COMPLETED,
                ChildSpawnAttemptState.RECONCILE_REQUIRED,
            },
            ChildSpawnAttemptState.RECONCILE_REQUIRED: {
                ChildSpawnAttemptState.WINDOW_BOUND,
                ChildSpawnAttemptState.COMPLETED,
                ChildSpawnAttemptState.RECONCILE_REQUIRED,
            },
        }
        if state != attempt.state and state not in allowed.get(attempt.state, set()):
            raise RuntimeError(
                f"invalid child spawn transition {attempt.state.value} -> {state.value}"
            )
        attempt.state = state
        attempt.updated_at = time.time() if now is None else float(now)
        if window_handle is not None:
            attempt.window_handle = int(window_handle)
        if browser_pid is not None:
            attempt.browser_pid = int(browser_pid)
        if conversation_url is not None:
            attempt.conversation_url = str(conversation_url).strip() or None
        if worker_id is not None:
            attempt.worker_id = str(worker_id).strip() or None
        if last_error is not None:
            attempt.last_error = str(last_error)[:1000]
        if metadata_updates:
            attempt.metadata.update(dict(metadata_updates))
        payload = _child_spawn_attempt_payload(attempt)
        cursor = self._conn.execute(
            """UPDATE child_spawn_attempts
               SET state = ?, updated_at = ?, payload_json = ? WHERE attempt_id = ?""",
            (attempt.state.value, attempt.updated_at, payload, attempt.attempt_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            raise KeyError(f"unknown child spawn attempt: {attempt_id}")
        self._conn.commit()
        return self.get_child_spawn_attempt(attempt_id)


    def set_runtime_cooldown(
        self,
        name: str,
        *,
        until_at: float,
        reason: str,
        metadata: dict | None = None,
        now: float | None = None,
    ) -> dict:
        key = str(name).strip()
        note = str(reason).strip()
        if not key or not note:
            raise ValueError("runtime cooldown name and reason are required")
        ts = time.time() if now is None else float(now)
        until = float(until_at)
        if until <= ts:
            raise ValueError("runtime cooldown must end in the future")
        payload = dict(metadata or {})
        self._conn.execute(
            """INSERT INTO runtime_cooldowns(name, until_at, reason, updated_at, payload_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   until_at=excluded.until_at, reason=excluded.reason,
                   updated_at=excluded.updated_at, payload_json=excluded.payload_json""",
            (key, until, note, ts, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        self._conn.commit()
        return self.get_runtime_cooldown(key) or {}

    def get_runtime_cooldown(self, name: str) -> dict | None:
        key = str(name).strip()
        if not key:
            raise ValueError("runtime cooldown name is required")
        row = self._conn.execute(
            "SELECT name, until_at, reason, updated_at, payload_json FROM runtime_cooldowns WHERE name = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "until_at": float(row["until_at"]),
            "reason": row["reason"],
            "updated_at": float(row["updated_at"]),
            "metadata": json.loads(row["payload_json"] or "{}"),
        }

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

    def worker_protocol_exists(self, task_id: str) -> bool:
        self.get_task(task_id)
        return worker_persistence.protocol_state_exists(self._conn, task_id)

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
        source: str = "windows_uia_lws_probe",
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
                    f"LWS supports only one durable probe slot; existing slot is {other_slot['slot_id']}"
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
            (worker_id, max(1, min(int(limit), OBSERVATION_RETENTION_PER_ENTITY))),
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


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _child_dispatch_from_row(row) -> ChildDispatchRecord:
    return ChildDispatchRecord(
        dispatch_id=row["dispatch_id"],
        parent_task_id=row["parent_task_id"],
        child_task_id=row["child_task_id"],
        child_key=row["child_key"],
        prompt_text=row["prompt_text"],
        prompt_sha256=row["prompt_sha256"],
        expected_branch=row["expected_branch"],
        base_ref=row["base_ref"],
        web_project_url=row["web_project_url"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        metadata=json.loads(row["payload_json"] or "{}"),
    )


def _replacement_attempt_payload(attempt: ReplacementAttempt) -> str:
    payload = asdict(attempt)
    payload["state"] = attempt.state.value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _child_spawn_attempt_payload(attempt: ChildSpawnAttempt) -> str:
    payload = asdict(attempt)
    payload["state"] = attempt.state.value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
