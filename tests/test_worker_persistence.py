import sqlite3
import tempfile
import unittest
from pathlib import Path

from cws.db import SCHEMA_V5, SCHEMA_V6, SCHEMA_VERSION
from cws.models import SupervisorState, WorkerStatus
from cws.registry import Registry
from cws.worker_persistence import WorkerProtocolPersistenceError
from cws.worker_protocol import (
    DecisionCode,
    DurableTaskStatus,
    EventKind,
    WorkerLeaseStatus,
    lease_is_fresh,
    worker_by_id,
)


URL_A = "https://chatgpt.com/c/worker-a"
URL_B = "https://chatgpt.com/c/worker-b"


def logical_snapshot(registry: Registry, task_id: str):
    tables = (
        "tasks",
        "workers",
        "worker_protocol_tasks",
        "worker_protocol_leases",
        "worker_protocol_events",
    )
    snapshot = {}
    for table in tables:
        if table == "tasks":
            rows = registry._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ? ORDER BY task_id", (task_id,)
            ).fetchall()
        elif table == "workers":
            rows = registry._conn.execute(
                "SELECT * FROM workers WHERE task_id = ? ORDER BY worker_id", (task_id,)
            ).fetchall()
        else:
            rows = registry._conn.execute(
                f"SELECT * FROM {table} WHERE task_id = ? ORDER BY 1", (task_id,)
            ).fetchall()
        snapshot[table] = tuple(tuple(row) for row in rows)
    return snapshot


class WorkerProtocolMigrationTests(unittest.TestCase):
    def test_schema_v5_migrates_additively_and_preserves_rows_and_uniqueness(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy-v5.sqlite3"
            raw = sqlite3.connect(db)
            raw.execute("PRAGMA foreign_keys = ON")
            raw.executescript(SCHEMA_V5)
            raw.execute("PRAGMA user_version = 5")
            raw.execute(
                """INSERT INTO tasks
                   (task_id, project, objective, cwd, state, checkpoint_json,
                    current_worker_id, created_at, updated_at)
                   VALUES ('legacy', 'cws', 'persist me', '.', 'QUEUED', '{}', 'w1', 1, 2)"""
            )
            raw.execute(
                """INSERT INTO workers
                   (worker_id, task_id, conversation_url, conversation_id, status, started_at)
                   VALUES ('w1', 'legacy', ?, 'conv-1', 'active', 1)""",
                (URL_A,),
            )
            raw.execute(
                """INSERT INTO action_attempts
                   (attempt_id, task_id, worker_id, state, created_at, updated_at, payload_json)
                   VALUES ('a1', 'legacy', 'w1', 'ARMED', 2, 2, '{}')"""
            )
            raw.execute(
                """INSERT INTO worker_window_bindings
                   (worker_id, window_handle, browser_pid, chrome_executable,
                    conversation_url, source, bound_at, observed_at, expires_at)
                   VALUES ('w1', 10, 20, 'chrome.exe', ?, 'test', 1, 2, 999)""",
                (URL_A,),
            )
            raw.execute(
                """INSERT INTO probe_window_slots
                   (slot_id, owner_token, target_worker_id, target_conversation_url,
                    actual_url, window_handle, browser_pid, chrome_executable, source,
                    bound_at, observed_at, expires_at)
                   VALUES ('probe:default', 'owner', 'w1', ?, ?, 11, 21,
                           'chrome.exe', 'test', 1, 2, 999)""",
                (URL_A, URL_A + "#probe"),
            )
            raw.execute(
                """INSERT INTO probe_mutation_operations
                   (operation_id, nonce, kind, state, slot_id, target_task_id,
                    target_worker_id, created_at, updated_at, payload_json)
                   VALUES ('p1', 'nonce-1', 'OPEN', 'ARMED', 'probe:default',
                           'legacy', 'w1', 2, 2, '{}')"""
            )
            raw.execute(
                """INSERT INTO page_capabilities
                   (capability_id, kind, scope_host, browser_family, browser_major,
                    platform, surface, isolation_mode, evaluator_version, evidence_digest,
                    source_experiment_id, observed_at, recorded_at, expires_at, payload_json)
                   VALUES ('cap1', 'page_close_generation', 'chatgpt.com', 'chromium', 151,
                           'windows', 'web', 'dedicated_profile', 'v1', 'digest', 'exp1',
                           1, 2, 999, '{}')"""
            )
            raw.commit()
            self.assertIsNone(
                raw.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='worker_protocol_tasks'"
                ).fetchone()
            )
            raw.close()

            registry = Registry(db)
            try:
                self.assertEqual(SCHEMA_VERSION, 8)
                self.assertEqual(
                    registry._conn.execute("PRAGMA user_version").fetchone()[0], 8
                )
                for table in (
                    "worker_protocol_tasks",
                    "worker_protocol_leases",
                    "worker_protocol_events",
                ):
                    self.assertIsNotNone(
                        registry._conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                            (table,),
                        ).fetchone()
                    )
                self.assertEqual(registry.get_task("legacy").current_worker_id, "w1")
                self.assertEqual(registry.get_worker("w1").conversation_id, "conv-1")
                self.assertIsNotNone(registry.get_worker_window_binding("w1"))
                self.assertIsNotNone(registry.get_probe_window_slot("probe:default"))
                self.assertEqual(
                    registry._conn.execute(
                        "SELECT capability_id FROM page_capabilities"
                    ).fetchone()[0],
                    "cap1",
                )
                self.assertEqual(
                    registry._conn.execute(
                        "SELECT operation_id FROM probe_mutation_operations"
                    ).fetchone()[0],
                    "p1",
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    registry._conn.execute(
                        """INSERT INTO action_attempts
                           (attempt_id, task_id, worker_id, state, created_at,
                            updated_at, payload_json)
                           VALUES ('a2', 'legacy', 'w1', 'SUBMITTED', 3, 3, '{}')"""
                    )
                registry._conn.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    registry._conn.execute(
                        """INSERT INTO probe_mutation_operations
                           (operation_id, nonce, kind, state, slot_id, target_task_id,
                            target_worker_id, created_at, updated_at, payload_json)
                           VALUES ('p2', 'nonce-2', 'OPEN', 'ARMED', 'probe:default',
                                   'legacy', 'w1', 3, 3, '{}')"""
                    )
                registry._conn.rollback()
                self.assertEqual(
                    registry._conn.execute("SELECT COUNT(*) FROM action_attempts").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    registry._conn.execute(
                        "SELECT COUNT(*) FROM probe_mutation_operations"
                    ).fetchone()[0],
                    1,
                )
            finally:
                registry.close()

    def test_schema_v6_migrates_additively_to_child_dispatch_table(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy-v6.sqlite3"
            raw = sqlite3.connect(db)
            raw.execute("PRAGMA foreign_keys = ON")
            raw.executescript(SCHEMA_V5 + SCHEMA_V6)
            raw.execute("PRAGMA user_version = 6")
            raw.execute(
                """INSERT INTO tasks
                   (task_id, project, objective, cwd, state, checkpoint_json,
                    created_at, updated_at)
                   VALUES ('root', 'cws', 'keep v6', '.', 'QUEUED', '{}', 1, 2)"""
            )
            raw.execute(
                """INSERT INTO worker_protocol_tasks
                   (task_id, revision, generation, task_status, root_task_id)
                   VALUES ('root', 0, 0, 'open', 'root')"""
            )
            raw.commit()
            raw.close()

            registry = Registry(db)
            try:
                self.assertEqual(registry._conn.execute("PRAGMA user_version").fetchone()[0], 8)
                self.assertEqual(registry.get_task("root").objective, "keep v6")
                self.assertEqual(
                    registry.load_worker_protocol("root").lineage.root_task_id, "root"
                )
                self.assertIsNotNone(
                    registry._conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='child_dispatches'"
                    ).fetchone()
                )
            finally:
                registry.close()

    def test_unknown_future_schema_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "future.sqlite3"
            raw = sqlite3.connect(db)
            raw.execute("PRAGMA user_version = 9")
            raw.commit()
            raw.close()
            with self.assertRaisesRegex(RuntimeError, "unsupported CWS registry schema"):
                Registry(db)


class WorkerProtocolPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "registry.sqlite3"
        self.registry = Registry(self.db)

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def register_task(self, task_id="t1", *, conversation_url=None):
        return self.registry.register_task(
            task_id=task_id,
            project="cws",
            objective="worker protocol persistence",
            cwd=".",
            conversation_url=conversation_url,
        )

    def activate(self, task_id="t1", *, worker_id="worker-a", now=10.0, lease=30.0):
        self.register_task(task_id)
        state = self.registry.bootstrap_worker_protocol(task_id)
        registered = self.registry.protocol_register_worker(
            task_id,
            URL_A,
            worker_id=worker_id,
            expected_revision=state.revision,
            now=now,
        )
        claimed = self.registry.protocol_claim_worker(
            task_id,
            worker_id,
            expected_revision=registered.state.revision,
            lease_seconds=lease,
            now=now + 1,
        )
        return claimed.state

    def test_clean_legacy_single_worker_bootstrap_is_expired_not_fresh_authority(self):
        task = self.register_task(conversation_url=URL_A)
        legacy = self.registry.get_worker(task.current_worker_id)
        state = self.registry.bootstrap_worker_protocol("t1")
        self.assertEqual(state.revision, 0)
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.current_worker_id, legacy.worker_id)
        lease = worker_by_id(state, legacy.worker_id)
        self.assertEqual(lease.status, WorkerLeaseStatus.ACTIVE)
        self.assertEqual(lease.lease_expires_at, lease.last_heartbeat_at)
        self.assertFalse(lease_is_fresh(state, legacy.worker_id, now=lease.lease_expires_at))
        self.assertEqual(self.registry.load_worker_protocol("t1"), state)
        self.assertEqual(self.registry.get_worker(legacy.worker_id).status, WorkerStatus.ACTIVE)
        self.assertEqual(self.registry.worker_protocol_events("t1"), ())

    def test_ambiguous_legacy_worker_state_fails_closed_without_partial_bootstrap(self):
        task = self.register_task(conversation_url=URL_A)
        self.registry.set_worker_status(task.current_worker_id, WorkerStatus.PARKED)
        with self.assertRaisesRegex(WorkerProtocolPersistenceError, "PARKED"):
            self.registry.bootstrap_worker_protocol("t1")
        self.assertEqual(
            self.registry._conn.execute(
                "SELECT COUNT(*) FROM worker_protocol_tasks WHERE task_id='t1'"
            ).fetchone()[0],
            0,
        )

    def test_register_claim_heartbeat_round_trip_and_legacy_mapping(self):
        self.register_task()
        state = self.registry.bootstrap_worker_protocol("t1")
        registered = self.registry.protocol_register_worker(
            "t1", URL_A, worker_id="worker-a", expected_revision=0, now=1
        )
        self.assertEqual(registered.state.revision, 1)
        self.assertEqual(self.registry.get_worker("worker-a").status, WorkerStatus.PARKED)
        self.assertIsNone(self.registry.get_task("t1").current_worker_id)

        claimed = self.registry.protocol_claim_worker(
            "t1", "worker-a", expected_revision=1, lease_seconds=30, now=2
        )
        self.assertEqual(claimed.state.generation, 1)
        self.assertEqual(self.registry.get_worker("worker-a").status, WorkerStatus.ACTIVE)
        self.assertEqual(self.registry.get_task("t1").current_worker_id, "worker-a")

        heartbeat = self.registry.protocol_heartbeat_worker(
            "t1",
            "worker-a",
            generation=1,
            expected_revision=2,
            lease_seconds=30,
            now=3,
        )
        self.assertEqual(heartbeat.state.revision, 3)
        self.assertEqual(worker_by_id(heartbeat.state, "worker-a").lease_expires_at, 33)
        self.assertEqual(self.registry.get_worker("worker-a").last_seen_at, 3)
        self.assertEqual(self.registry.load_worker_protocol("t1"), heartbeat.state)
        self.assertEqual(
            [event.kind for event in self.registry.worker_protocol_events("t1")],
            [
                EventKind.WORKER_REGISTERED,
                EventKind.LEASE_CLAIMED,
                EventKind.HEARTBEAT_ACCEPTED,
            ],
        )

    def test_stale_expected_revision_is_rejected_across_registry_connections(self):
        self.register_task()
        self.registry.bootstrap_worker_protocol("t1")
        second = Registry(self.db)
        try:
            self.assertEqual(second.load_worker_protocol("t1").revision, 0)
            winner = self.registry.protocol_register_worker(
                "t1", URL_A, worker_id="worker-a", expected_revision=0, now=1
            )
            self.assertTrue(winner.accepted)
            loser = second.protocol_register_worker(
                "t1", URL_B, worker_id="worker-b", expected_revision=0, now=1
            )
            self.assertFalse(loser.accepted)
            self.assertEqual(loser.code, DecisionCode.STALE_REVISION)
            self.assertEqual(second.load_worker_protocol("t1").revision, 1)
            self.assertEqual(
                [worker.worker_id for worker in second.load_worker_protocol("t1").workers],
                ["worker-a"],
            )
            self.assertEqual(len(second.worker_protocol_events("t1")), 1)
        finally:
            second.close()

    def test_handoff_takeover_fences_old_generation_and_rejected_calls_do_not_write(self):
        state = self.activate(now=10, lease=100)
        registered = self.registry.protocol_register_worker(
            "t1", URL_B, worker_id="worker-b", expected_revision=state.revision, now=12
        )
        handoff = self.registry.protocol_request_handoff(
            "t1",
            "worker-a",
            "worker-b",
            generation=1,
            expected_revision=registered.state.revision,
            now=13,
        )
        takeover = self.registry.protocol_takeover_worker(
            "t1",
            "worker-b",
            expected_revision=handoff.state.revision,
            lease_seconds=100,
            now=14,
        )
        self.assertEqual(takeover.state.generation, 2)
        self.assertEqual(takeover.state.current_worker_id, "worker-b")
        self.assertEqual(
            worker_by_id(takeover.state, "worker-a").status,
            WorkerLeaseStatus.SUPERSEDED,
        )
        self.assertEqual(self.registry.get_worker("worker-a").status, WorkerStatus.SUPERSEDED)
        self.assertEqual(self.registry.get_worker("worker-b").status, WorkerStatus.ACTIVE)

        before = logical_snapshot(self.registry, "t1")
        rejected = (
            self.registry.protocol_heartbeat_worker(
                "t1",
                "worker-a",
                generation=1,
                expected_revision=takeover.state.revision,
                lease_seconds=30,
                now=15,
            ),
            self.registry.protocol_complete_worker(
                "t1",
                "worker-a",
                generation=1,
                expected_revision=takeover.state.revision,
                now=15,
            ),
            self.registry.protocol_request_handoff(
                "t1",
                "worker-a",
                "worker-b",
                generation=1,
                expected_revision=takeover.state.revision,
                now=15,
            ),
        )
        self.assertTrue(all(not decision.accepted for decision in rejected))
        self.assertTrue(all(decision.code == DecisionCode.FENCED_WORKER for decision in rejected))
        self.assertEqual(logical_snapshot(self.registry, "t1"), before)

    def test_lease_expiry_takeover_persists_one_active_generation(self):
        state = self.activate(now=10, lease=10)
        candidate = self.registry.protocol_register_worker(
            "t1", URL_B, worker_id="worker-b", expected_revision=state.revision, now=12
        )
        takeover = self.registry.protocol_takeover_worker(
            "t1",
            "worker-b",
            expected_revision=candidate.state.revision,
            lease_seconds=30,
            now=21,
        )
        self.assertTrue(takeover.accepted)
        self.assertEqual(takeover.state.generation, 2)
        self.assertEqual(takeover.state.current_worker_id, "worker-b")
        active_protocol = [
            worker.worker_id
            for worker in takeover.state.workers
            if worker.status == WorkerLeaseStatus.ACTIVE
        ]
        self.assertEqual(active_protocol, ["worker-b"])
        active_legacy = self.registry._conn.execute(
            "SELECT worker_id FROM workers WHERE task_id='t1' AND status='active'"
        ).fetchall()
        self.assertEqual([row[0] for row in active_legacy], ["worker-b"])
        takeover_events = [
            event
            for event in self.registry.worker_protocol_events("t1")
            if event.revision == takeover.state.revision
        ]
        self.assertEqual(
            [event.kind for event in takeover_events],
            [EventKind.WORKER_SUPERSEDED, EventKind.LEASE_TAKEN_OVER],
        )

    def test_worker_completion_and_durable_task_completion_remain_distinct(self):
        state = self.activate(now=10, lease=100)
        worker_done = self.registry.protocol_complete_worker(
            "t1",
            "worker-a",
            generation=1,
            expected_revision=state.revision,
            now=12,
        )
        self.assertEqual(worker_done.state.task_status, DurableTaskStatus.OPEN)
        self.assertEqual(self.registry.get_task("t1").state, SupervisorState.QUEUED)
        self.assertEqual(self.registry.get_worker("worker-a").status, WorkerStatus.DEAD)
        self.assertIsNone(self.registry.get_task("t1").current_worker_id)

        task_done = self.registry.protocol_complete_task(
            "t1",
            completion_ref="workspace:commit:abc",
            expected_revision=worker_done.state.revision,
            now=13,
        )
        self.assertEqual(task_done.state.task_status, DurableTaskStatus.COMPLETED)
        self.assertEqual(self.registry.get_task("t1").state, SupervisorState.COMPLETED)
        with self.assertRaisesRegex(RuntimeError, "cannot be reopened"):
            self.registry.update_state("t1", SupervisorState.RUNNING)

    def test_abandonment_preserves_durable_task_and_next_claim_advances_generation(self):
        state = self.activate(now=10, lease=100)
        abandoned = self.registry.protocol_abandon_worker(
            "t1",
            "worker-a",
            generation=1,
            expected_revision=state.revision,
            now=12,
        )
        self.assertEqual(abandoned.state.task_status, DurableTaskStatus.OPEN)
        self.assertIsNone(abandoned.state.current_worker_id)
        self.assertEqual(self.registry.get_task("t1").task_id, "t1")
        replacement = self.registry.protocol_register_worker(
            "t1",
            URL_B,
            worker_id="worker-b",
            expected_revision=abandoned.state.revision,
            now=13,
        )
        claimed = self.registry.protocol_claim_worker(
            "t1",
            "worker-b",
            expected_revision=replacement.state.revision,
            lease_seconds=30,
            now=14,
        )
        self.assertEqual(claimed.state.generation, 2)
        self.assertEqual(claimed.state.current_worker_id, "worker-b")

    def test_parent_root_child_lineage_survives_reload_and_uses_task_foreign_keys(self):
        for task_id in ("root", "child", "grandchild"):
            self.register_task(task_id)
        root = self.registry.bootstrap_worker_protocol("root")
        child = self.registry.bootstrap_worker_protocol(
            "child", parent_task_id="root", child_key="research"
        )
        grandchild = self.registry.bootstrap_worker_protocol(
            "grandchild", parent_task_id="child", child_key="test"
        )
        self.assertEqual(root.lineage.root_task_id, "root")
        self.assertEqual(child.lineage.root_task_id, "root")
        self.assertEqual(grandchild.lineage.parent_task_id, "child")
        self.assertEqual(grandchild.lineage.root_task_id, "root")
        self.assertEqual(
            self.registry.load_worker_protocol("grandchild").lineage.child_key, "test"
        )
        self.assertEqual(
            self.registry.bootstrap_worker_protocol(
                "child", parent_task_id="root", child_key="research"
            ),
            child,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry._conn.execute("DELETE FROM tasks WHERE task_id='root'")
        self.registry._conn.rollback()
        self.assertEqual(self.registry.get_task("root").task_id, "root")

    def test_event_append_and_legacy_mapping_rollback_with_accepted_transition_failure(self):
        state = self.activate(now=10, lease=100)
        before = logical_snapshot(self.registry, "t1")
        self.registry._conn.executescript(
            """
            CREATE TRIGGER fail_worker_protocol_event
            BEFORE INSERT ON worker_protocol_events
            BEGIN
                SELECT RAISE(ABORT, 'injected event append failure');
            END;
            """
        )
        self.registry._conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.protocol_heartbeat_worker(
                "t1",
                "worker-a",
                generation=1,
                expected_revision=state.revision,
                lease_seconds=100,
                now=12,
            )
        self.assertEqual(logical_snapshot(self.registry, "t1"), before)
        self.registry._conn.execute("DROP TRIGGER fail_worker_protocol_event")
        self.registry._conn.commit()

    def test_legacy_worker_mutators_are_fenced_after_protocol_bootstrap(self):
        self.register_task()
        self.registry.bootstrap_worker_protocol("t1")
        with self.assertRaisesRegex(RuntimeError, "protocol_register_worker"):
            self.registry.add_worker("t1", URL_A)
        registered = self.registry.protocol_register_worker(
            "t1", URL_A, worker_id="worker-a", expected_revision=0, now=1
        )
        with self.assertRaisesRegex(RuntimeError, "worker-protocol transitions"):
            self.registry.set_worker_status("worker-a", WorkerStatus.ACTIVE)
        with self.assertRaisesRegex(RuntimeError, "task semantics"):
            self.registry.update_state("t1", SupervisorState.ABANDONED)
        self.assertEqual(registered.state.revision, 1)


if __name__ == "__main__":
    unittest.main()
