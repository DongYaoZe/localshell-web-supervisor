import sqlite3
import tempfile
import unittest
from pathlib import Path

from cws.action_runtime import submit_armed_action
from cws.actions import (
    ActionAcknowledgement,
    ActionAttemptState,
    ActionBlocked,
    ActionTransportDisabled,
    DisabledActionTransport,
    TransportSubmission,
    apply_unresolved_action_gate,
    build_action_attempt,
    evidence_digest,
    intent_from_attempt,
)
from cws.db import SCHEMA_VERSION, connect
from cws.dispatcher import DispatchAction, DispatchPlan
from cws.models import SupervisorState, TaskRecord, WorkerRecord, WorkerStatus
from cws.registry import Registry


NOW = 1000.0
PROMPT = "continue safely from the first incomplete step"


def task():
    return TaskRecord(
        task_id="t1",
        project="p",
        objective="obj",
        cwd="C:/repo",
        state=SupervisorState.SUSPECT,
        lsm_session_id="s1",
        current_worker_id="w1",
        recovery_attempts=0,
        max_recovery_attempts=3,
    )


def worker(*, worker_id="w1", status=WorkerStatus.ACTIVE):
    return WorkerRecord(
        worker_id=worker_id,
        task_id="t1",
        conversation_url="https://chatgpt.com/c/x",
        conversation_id="x",
        status=status,
        started_at=1.0,
    )


def plan(*, ready=True):
    return DispatchPlan(
        task_id="t1",
        created_at=NOW,
        action=DispatchAction.CONTINUE_CURRENT_WORKER,
        candidate_ready=ready,
        transport_enabled=False,
        would_dispatch=False,
        reason="fixture",
        previous_reconcile_id="rec1",
        current_reconcile_id="rec2",
        fence_token="fixture-" + "fence-v2",
    )


def attempt(*, now=NOW):
    return build_action_attempt(
        plan(),
        task(),
        worker(),
        PROMPT,
        fence_version=2,
        pre_action_signature="before",
        now=now,
    )


class FakeTransport:
    name = "fake"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def submit(self, intent):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class ActionModelTests(unittest.TestCase):
    def test_unresolved_attempt_forces_dispatch_plan_closed(self):
        armed = attempt()
        current_plan = plan()
        gated = apply_unresolved_action_gate(current_plan, armed)
        self.assertFalse(gated.candidate_ready)
        self.assertFalse(gated.would_dispatch)
        self.assertFalse(gated.checks["no_unresolved_action_attempt"])
        self.assertTrue(any("unresolved" in blocker for blocker in gated.blockers))

    def test_candidate_plan_and_current_active_worker_are_required(self):
        with self.assertRaises(ActionBlocked):
            build_action_attempt(
                plan(ready=False), task(), worker(), PROMPT,
                fence_version=2, pre_action_signature="before", now=NOW,
            )
        with self.assertRaises(ActionBlocked):
            build_action_attempt(
                plan(), task(), worker(worker_id="other"), PROMPT,
                fence_version=2, pre_action_signature="before", now=NOW,
            )
        with self.assertRaises(ActionBlocked):
            build_action_attempt(
                plan(), task(), worker(status=WorkerStatus.PARKED), PROMPT,
                fence_version=2, pre_action_signature="before", now=NOW,
            )

    def test_prompt_hash_is_bound_to_armed_attempt(self):
        armed = attempt()
        intent = intent_from_attempt(armed, PROMPT)
        self.assertEqual(intent.prompt_hash, armed.prompt_hash)
        with self.assertRaises(ActionBlocked):
            intent_from_attempt(armed, PROMPT + " changed")


class ActionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "registry.sqlite3"
        self.registry = Registry(self.db)
        registered = self.registry.register_task(
            task_id="t1",
            project="p",
            objective="obj",
            cwd="C:/repo",
            lsm_session_id="s1",
            conversation_url="https://chatgpt.com/c/x",
            conversation_id="x",
        )
        # Test fixtures use stable worker id w1, while register_task generates one.
        generated = registered.current_worker_id
        self.registry._conn.execute("UPDATE workers SET worker_id = 'w1' WHERE worker_id = ?", (generated,))
        self.registry._conn.execute("UPDATE tasks SET current_worker_id = 'w1' WHERE task_id = 't1'")
        self.registry._conn.commit()

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def arm(self, *, now=NOW):
        row = attempt(now=now)
        self.registry.record_action_attempt(row)
        return row

    def test_only_one_unresolved_attempt_per_task(self):
        first = self.arm()
        self.assertEqual(self.registry.unresolved_action_attempt("t1").attempt_id, first.attempt_id)
        second = attempt(now=NOW + 1)
        with self.assertRaises((RuntimeError, sqlite3.IntegrityError)):
            self.registry.record_action_attempt(second)

    def test_disabled_transport_keeps_write_ahead_lock_armed(self):
        armed = self.arm()
        with self.assertRaises(ActionTransportDisabled):
            submit_armed_action(
                self.registry,
                attempt_id=armed.attempt_id,
                prompt=PROMPT,
                transport=DisabledActionTransport(),
            )
        self.assertEqual(
            self.registry.get_action_attempt(armed.attempt_id).state,
            ActionAttemptState.ARMED,
        )

    def test_successful_submission_becomes_unresolved_submitted(self):
        armed = self.arm()
        transport = FakeTransport(
            TransportSubmission(True, True, "fake", "clicked and observed submission UI")
        )
        outcome = submit_armed_action(
            self.registry, attempt_id=armed.attempt_id, prompt=PROMPT, transport=transport
        )
        self.assertEqual(outcome.state, ActionAttemptState.SUBMITTED.value)
        self.assertTrue(outcome.side_effect_possible)
        self.assertEqual(
            self.registry.unresolved_action_attempt("t1").state,
            ActionAttemptState.SUBMITTED,
        )

    def test_transport_exception_becomes_reconcile_required(self):
        armed = self.arm()
        outcome = submit_armed_action(
            self.registry,
            attempt_id=armed.attempt_id,
            prompt=PROMPT,
            transport=FakeTransport(error=RuntimeError("lost connection after click")),
        )
        self.assertEqual(outcome.state, ActionAttemptState.RECONCILE_REQUIRED.value)
        self.assertTrue(outcome.side_effect_possible)

    def test_proven_no_side_effect_is_terminal_failed_and_releases_lock(self):
        armed = self.arm()
        outcome = submit_armed_action(
            self.registry,
            attempt_id=armed.attempt_id,
            prompt=PROMPT,
            transport=FakeTransport(
                TransportSubmission(False, False, "fake", "selector absent before click")
            ),
        )
        self.assertEqual(outcome.state, ActionAttemptState.FAILED.value)
        self.assertIsNone(self.registry.unresolved_action_attempt("t1"))
        self.registry.record_action_attempt(attempt(now=NOW + 2))

    def test_positive_acknowledgement_releases_duplicate_send_lock(self):
        armed = self.arm()
        self.registry.mark_action_submitted(
            armed.attempt_id, transport_name="fake", submitted_at=NOW + 1
        )
        ack = ActionAcknowledgement(
            attempt_id=armed.attempt_id,
            worker_id="w1",
            observed_at=NOW + 2,
            accepted=True,
            kind="visible-user-turn",
            evidence_hash=evidence_digest("new user message signature"),
        )
        updated = self.registry.acknowledge_action(ack)
        self.assertEqual(updated.state, ActionAttemptState.ACKNOWLEDGED)
        self.assertIsNone(self.registry.unresolved_action_attempt("t1"))
        self.registry.record_action_attempt(attempt(now=NOW + 3))


class RegistryMigrationTests(unittest.TestCase):
    def test_version_one_registry_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            raw = sqlite3.connect(path)
            raw.executescript(
                """
                PRAGMA user_version = 1;
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY, project TEXT NOT NULL, objective TEXT NOT NULL,
                    cwd TEXT NOT NULL, state TEXT NOT NULL, lsm_session_id TEXT,
                    checkpoint_json TEXT NOT NULL DEFAULT '{}', current_worker_id TEXT,
                    recovery_attempts INTEGER NOT NULL DEFAULT 0,
                    max_recovery_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE workers (
                    worker_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    conversation_url TEXT NOT NULL, conversation_id TEXT, status TEXT NOT NULL,
                    started_at REAL NOT NULL, last_seen_at REAL, ended_at REAL
                );
                INSERT INTO tasks VALUES
                    ('legacy','p','o','C:/repo','QUEUED','s1','{}','w1',0,3,1,1);
                INSERT INTO workers VALUES
                    ('w1','legacy','https://chatgpt.com/c/x','x','active',1,NULL,NULL);
                """
            )
            raw.commit()
            raw.close()

            conn = connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("SELECT project FROM tasks WHERE task_id='legacy'").fetchone()[0], "p")
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='action_attempts'"
            ).fetchone()
            self.assertIsNotNone(table)
            conn.close()


if __name__ == "__main__":
    unittest.main()
