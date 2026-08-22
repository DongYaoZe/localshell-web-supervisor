import hashlib
import tempfile
import unittest
from pathlib import Path

from lws.registry import Registry
from lws.worker_protocol import DurableTaskStatus, WorkerLeaseStatus


URL_A = "https://web.example/g/project/c/child-a"
URL_B = "https://web.example/g/project/c/child-b"


class ChildDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "registry.sqlite3"
        self.registry = Registry(self.db)
        self.registry.register_task(
            task_id="parent",
            project="lws",
            objective="parent orchestration",
            cwd="D:/repo",
        )

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def create(self, **overrides):
        values = {
            "child_key": "worker-a",
            "child_task_id": "child-a",
            "project": "lws",
            "objective": "implement isolated feature A",
            "cwd": "D:/repo-wt-a",
            "prompt_text": "Read bootstrap, implement feature A, test, commit.",
            "expected_branch": "agent/feature-a",
            "base_ref": "abc123",
            "metadata": {"owner": "parent-ai"},
            "now": 10.0,
        }
        values.update(overrides)
        return self.registry.create_child_dispatch("parent", **values)

    def test_create_is_atomic_lineaged_and_idempotent_for_same_contract(self):
        dispatch = self.create()
        self.assertEqual(dispatch.child_task_id, "child-a")
        self.assertEqual(
            dispatch.prompt_sha256,
            hashlib.sha256(dispatch.prompt_text.encode("utf-8")).hexdigest(),
        )
        child = self.registry.get_task("child-a")
        self.assertEqual(child.cwd, "D:/repo-wt-a")
        protocol = self.registry.load_worker_protocol("child-a")
        self.assertEqual(protocol.revision, 0)
        self.assertEqual(protocol.generation, 0)
        self.assertEqual(protocol.task_status, DurableTaskStatus.OPEN)
        self.assertEqual(protocol.lineage.parent_task_id, "parent")
        self.assertEqual(protocol.lineage.root_task_id, "parent")
        self.assertEqual(protocol.lineage.child_key, "worker-a")
        self.assertEqual(protocol.workers, ())

        again = self.create()
        self.assertEqual(again, dispatch)
        self.assertEqual(len(self.registry.child_dispatches_for_parent("parent")), 1)
        self.assertEqual(
            self.registry._conn.execute("SELECT COUNT(*) FROM tasks WHERE task_id='child-a'").fetchone()[0],
            1,
        )

    def test_reusing_child_key_with_different_contract_fails_without_partial_second_child(self):
        self.create()
        before = self.registry._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "different dispatch contract"):
            self.create(
                child_task_id="child-b",
                prompt_text="different assignment",
                objective="different objective",
            )
        self.assertEqual(self.registry._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)
        self.assertIsNone(
            self.registry._conn.execute("SELECT 1 FROM tasks WHERE task_id='child-b'").fetchone()
        )

    def test_parent_legacy_protocol_bootstrap_is_conservative_but_automatic(self):
        self.assertEqual(
            self.registry._conn.execute(
                "SELECT COUNT(*) FROM worker_protocol_tasks WHERE task_id='parent'"
            ).fetchone()[0],
            0,
        )
        self.create()
        parent = self.registry.load_worker_protocol("parent")
        self.assertEqual(parent.lineage.root_task_id, "parent")
        self.assertEqual(parent.generation, 0)

    def test_adopt_claims_first_exact_conversation_and_is_idempotent(self):
        self.create()
        state = self.registry.adopt_child_worker(
            "child-a", URL_A, worker_id="worker-a", lease_seconds=30, now=20
        )
        self.assertEqual(state.current_worker_id, "worker-a")
        self.assertEqual(state.generation, 1)
        self.assertEqual(state.revision, 2)
        worker = next(worker for worker in state.workers if worker.worker_id == "worker-a")
        self.assertEqual(worker.status, WorkerLeaseStatus.ACTIVE)
        self.assertEqual(worker.conversation_ref, URL_A)
        self.assertEqual(worker.lease_expires_at, 50)

        same = self.registry.adopt_child_worker("child-a", URL_A, lease_seconds=30, now=21)
        self.assertEqual(same.revision, 2)
        self.assertEqual(same.current_worker_id, "worker-a")
        with self.assertRaisesRegex(RuntimeError, "authoritative worker"):
            self.registry.adopt_child_worker("child-a", URL_B, lease_seconds=30, now=21)

    def test_adopt_recovers_candidate_left_between_register_and_claim(self):
        self.create()
        initial = self.registry.load_worker_protocol("child-a")
        registered = self.registry.protocol_register_worker(
            "child-a",
            URL_A,
            worker_id="worker-a",
            expected_revision=initial.revision,
            now=20,
        )
        self.assertEqual(registered.state.revision, 1)
        self.assertIsNone(registered.state.current_worker_id)
        recovered = self.registry.adopt_child_worker("child-a", URL_A, lease_seconds=30, now=21)
        self.assertEqual(recovered.revision, 2)
        self.assertEqual(recovered.current_worker_id, "worker-a")

    def test_child_task_can_supervise_grandchild_while_preserving_root_lineage(self):
        self.create()
        nested = self.registry.create_child_dispatch(
            "child-a",
            child_key="nested-test",
            child_task_id="grandchild",
            project="lws",
            objective="nested child verification",
            cwd="D:/repo-wt-grandchild",
            prompt_text="verify nested child supervisor",
            expected_branch="agent/grandchild",
            base_ref="abc123",
            now=20.0,
        )
        self.assertEqual(nested.parent_task_id, "child-a")
        state = self.registry.load_worker_protocol("grandchild")
        self.assertEqual(state.lineage.parent_task_id, "child-a")
        self.assertEqual(state.lineage.root_task_id, "parent")
        self.assertEqual(state.lineage.child_key, "nested-test")
        self.assertEqual(
            [d.child_task_id for d in self.registry.child_dispatches_for_parent("child-a")],
            ["grandchild"],
        )

    def test_child_lsm_session_binds_once_and_replacement_must_reuse_it(self):
        self.create()
        task = self.registry.bind_child_lsm_session("child-a", "s-child-a")
        self.assertEqual(task.lsm_session_id, "s-child-a")
        same = self.registry.bind_child_lsm_session("child-a", "s-child-a")
        self.assertEqual(same.lsm_session_id, "s-child-a")
        with self.assertRaisesRegex(RuntimeError, "different durable LSM session"):
            self.registry.bind_child_lsm_session("child-a", "s-other")
        self.assertEqual(self.registry.get_task("child-a").lsm_session_id, "s-child-a")

    def test_child_complete_finishes_worker_and_task_and_is_idempotent(self):
        self.create()
        self.registry.adopt_child_worker(
            "child-a", URL_A, worker_id="worker-a", lease_seconds=60, now=20
        )
        completed = self.registry.complete_child_dispatch(
            "child-a", completion_ref="commit:abc123", now=30
        )
        self.assertEqual(completed.task_status, DurableTaskStatus.COMPLETED)
        self.assertEqual(completed.completion_ref, "commit:abc123")
        self.assertIsNone(completed.current_worker_id)
        worker = next(worker for worker in completed.workers if worker.worker_id == "worker-a")
        self.assertEqual(worker.status, WorkerLeaseStatus.COMPLETED)
        self.assertEqual(self.registry.get_task("child-a").state.value, "COMPLETED")

        same = self.registry.complete_child_dispatch(
            "child-a", completion_ref="commit:abc123", now=31
        )
        self.assertEqual(same.revision, completed.revision)
        with self.assertRaisesRegex(RuntimeError, "different completion_ref"):
            self.registry.complete_child_dispatch(
                "child-a", completion_ref="commit:different", now=32
            )

    def test_child_complete_recovers_if_worker_completion_persisted_before_task_completion(self):
        self.create()
        state = self.registry.adopt_child_worker(
            "child-a", URL_A, worker_id="worker-a", lease_seconds=60, now=20
        )
        ended = self.registry.protocol_complete_worker(
            "child-a",
            "worker-a",
            generation=state.generation,
            expected_revision=state.revision,
            now=30,
        )
        self.assertTrue(ended.accepted)
        self.assertIsNone(ended.state.current_worker_id)
        completed = self.registry.complete_child_dispatch(
            "child-a", completion_ref="commit:abc123", now=31
        )
        self.assertEqual(completed.task_status, DurableTaskStatus.COMPLETED)
        self.assertEqual(completed.completion_ref, "commit:abc123")

    def test_empty_prompt_rejected_before_any_child_write(self):
        with self.assertRaisesRegex(ValueError, "prompt"):
            self.create(prompt_text="   ")
        self.assertEqual(len(self.registry.child_dispatches_for_parent("parent")), 0)
        self.assertIsNone(
            self.registry._conn.execute("SELECT 1 FROM tasks WHERE task_id='child-a'").fetchone()
        )


if __name__ == "__main__":
    unittest.main()
