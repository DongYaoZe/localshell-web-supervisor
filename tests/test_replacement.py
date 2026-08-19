import tempfile
import unittest
from pathlib import Path

from lws.actions import ActionAttempt, ActionAttemptState
from lws.models import LsmObservation, ReplacementAttemptState, WorkspaceObservation
from lws.registry import Registry
from lws.replacement import (
    ReplacementBlocked,
    arm_replacement,
    complete_replacement,
    submit_replacement,
)
from lws.worker_protocol import WorkerLeaseStatus, worker_by_id


OLD_URL = "https://web.example/g/project/c/old-worker"
NEW_URL = "https://web.example/g/project/c/new-worker"


class ReplacementWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.tmp.name) / "registry.sqlite3")
        self.registry.register_task(
            task_id="parent",
            project="lws",
            objective="parent",
            cwd="D:/repo",
        )
        self.registry.create_child_dispatch(
            "parent",
            child_key="A",
            child_task_id="child",
            project="lws",
            objective="child work",
            cwd="D:/repo-wt",
            prompt_text="Do child work; test; commit.",
            now=1,
        )
        self.registry.bind_child_lsm_session("child", "s-child")
        self.registry.adopt_child_worker(
            "child", OLD_URL, worker_id="old", lease_seconds=100, now=10
        )
        self.registry.register_replacement_candidate(
            "child", NEW_URL, worker_id="new", now=20
        )

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def lsm(self, *, run="run-old", at=30, in_flight=0, jobs=0, pending=False):
        return LsmObservation(
            task_id="child",
            observed_at=at,
            session_id="s-child",
            session_status="active",
            active_run_id=run,
            plan_status="active",
            continuation_pending=pending,
            in_flight_calls=in_flight,
            active_jobs=jobs,
        )

    def workspace(self, *, at=30, status_hash=None, head=None):
        return WorkspaceObservation(
            task_id="child",
            observed_at=at,
            cwd="D:/repo-wt",
            cwd_exists=True,
            is_git_repo=False,
            git_head=head,
            git_status_hash=status_hash,
            error=None,
        )

    def arm(self, **kwargs):
        values = {
            "task_id": "child",
            "candidate_worker_id": "new",
            "lsm": self.lsm(),
            "workspace": self.workspace(),
            "now": 30,
        }
        values.update(kwargs)
        return arm_replacement(self.registry, **values)

    def unresolved_action(self):
        attempt = ActionAttempt(
            attempt_id="browser-send",
            task_id="child",
            worker_id="old",
            action="CONTINUE_CURRENT_WORKER",
            fence_token="fence",
            fence_version=2,
            prompt_hash="hash",
            nonce="nonce-browser-send",
            state=ActionAttemptState.SUBMITTED,
            created_at=25,
            updated_at=25,
        )
        self.registry.record_action_attempt(attempt)
        return attempt

    def test_fresh_old_lease_replaced_only_after_new_lsm_run_is_observed(self):
        armed = self.arm()
        self.assertEqual(armed.state, ReplacementAttemptState.ARMED)
        self.assertEqual(armed.mode, "LSM_FENCE_THEN_CLAIM")
        self.assertEqual(armed.previous_worker_id, "old")
        self.assertEqual(armed.previous_active_run_id, "run-old")

        submitted = submit_replacement(self.registry, armed.attempt_id, now=31)
        self.assertEqual(submitted.state, ReplacementAttemptState.LSM_TAKEOVER_SUBMITTED)
        completed = complete_replacement(
            self.registry,
            attempt_id=armed.attempt_id,
            new_active_run_id="run-new",
            lsm=self.lsm(run="run-new", at=32),
            workspace=self.workspace(at=32),
            lease_seconds=100,
            now=32,
        )
        self.assertEqual(completed.state, ReplacementAttemptState.COMPLETED)
        self.assertEqual(completed.new_active_run_id, "run-new")
        state = self.registry.load_worker_protocol("child")
        self.assertEqual(state.current_worker_id, "new")
        self.assertEqual(state.generation, 2)
        self.assertEqual(worker_by_id(state, "new").status, WorkerLeaseStatus.ACTIVE)
        self.assertEqual(worker_by_id(state, "old").status, WorkerLeaseStatus.ABANDONED)

    def test_inherited_submitted_browser_action_blocks_replacement_even_if_lsm_idle(self):
        self.unresolved_action()
        before = self.registry.load_worker_protocol("child")
        with self.assertRaises(ReplacementBlocked) as ctx:
            self.arm()
        self.assertTrue(any("browser-send" in item and "SUBMITTED" in item for item in ctx.exception.blockers))
        self.assertIsNone(self.registry.unresolved_replacement_attempt("child"))
        self.assertEqual(self.registry.load_worker_protocol("child"), before)

    def test_live_lsm_tool_job_or_pending_continuation_blocks_arm(self):
        cases = (
            self.lsm(in_flight=1),
            self.lsm(jobs=1),
            self.lsm(pending=True),
        )
        for observation in cases:
            with self.subTest(observation=observation):
                with self.assertRaises(ReplacementBlocked):
                    self.arm(lsm=observation)
                self.assertIsNone(self.registry.unresolved_replacement_attempt("child"))

    def test_lost_or_ineffective_lsm_takeover_does_not_publish_new_worker_or_replay(self):
        armed = self.arm()
        submit_replacement(self.registry, armed.attempt_id, now=31)
        before = self.registry.load_worker_protocol("child")
        with self.assertRaises(ReplacementBlocked):
            complete_replacement(
                self.registry,
                attempt_id=armed.attempt_id,
                new_active_run_id="run-old",
                lsm=self.lsm(run="run-old", at=32),
                workspace=self.workspace(at=32),
                now=32,
            )
        attempt = self.registry.get_replacement_attempt(armed.attempt_id)
        self.assertEqual(attempt.state, ReplacementAttemptState.RECONCILE_REQUIRED)
        self.assertEqual(self.registry.load_worker_protocol("child"), before)
        with self.assertRaisesRegex(RuntimeError, "not ARMED"):
            submit_replacement(self.registry, armed.attempt_id, now=33)

    def test_workspace_change_during_lsm_takeover_blocks_protocol_generation_change(self):
        armed = self.arm(workspace=self.workspace(status_hash="clean"))
        submit_replacement(self.registry, armed.attempt_id, now=31)
        before = self.registry.load_worker_protocol("child")
        with self.assertRaises(ReplacementBlocked) as ctx:
            complete_replacement(
                self.registry,
                attempt_id=armed.attempt_id,
                new_active_run_id="run-new",
                lsm=self.lsm(run="run-new", at=32),
                workspace=self.workspace(at=32, status_hash="changed"),
                now=32,
            )
        self.assertTrue(any("workspace status changed" in item for item in ctx.exception.blockers))
        self.assertEqual(self.registry.load_worker_protocol("child"), before)
        self.assertEqual(
            self.registry.get_replacement_attempt(armed.attempt_id).state,
            ReplacementAttemptState.RECONCILE_REQUIRED,
        )

    def test_expired_old_lease_uses_protocol_takeover_after_lsm_takeover(self):
        # Rebuild with a short old lease in an independent task.
        self.registry.create_child_dispatch(
            "parent",
            child_key="B",
            child_task_id="expired",
            project="lws",
            objective="expired child",
            cwd="D:/expired",
            prompt_text="Continue expired child.",
            now=2,
        )
        self.registry.bind_child_lsm_session("expired", "s-expired")
        self.registry.adopt_child_worker(
            "expired", OLD_URL + "-2", worker_id="old-2", lease_seconds=5, now=10
        )
        self.registry.register_replacement_candidate(
            "expired", NEW_URL + "-2", worker_id="new-2", now=12
        )
        old_lsm = LsmObservation(
            task_id="expired",
            observed_at=30,
            session_id="s-expired",
            active_run_id="run-old-2",
            continuation_pending=False,
        )
        old_ws = WorkspaceObservation(
            task_id="expired", observed_at=30, cwd="D:/expired", cwd_exists=True, is_git_repo=False
        )
        armed = arm_replacement(
            self.registry,
            task_id="expired",
            candidate_worker_id="new-2",
            lsm=old_lsm,
            workspace=old_ws,
            now=30,
        )
        self.assertEqual(armed.mode, "PROTOCOL_TAKEOVER")
        submit_replacement(self.registry, armed.attempt_id, now=31)
        new_lsm = LsmObservation(
            task_id="expired",
            observed_at=32,
            session_id="s-expired",
            active_run_id="run-new-2",
            continuation_pending=False,
        )
        new_ws = WorkspaceObservation(
            task_id="expired", observed_at=32, cwd="D:/expired", cwd_exists=True, is_git_repo=False
        )
        complete_replacement(
            self.registry,
            attempt_id=armed.attempt_id,
            new_active_run_id="run-new-2",
            lsm=new_lsm,
            workspace=new_ws,
            now=32,
        )
        state = self.registry.load_worker_protocol("expired")
        self.assertEqual(state.current_worker_id, "new-2")
        self.assertEqual(state.generation, 2)
        self.assertEqual(worker_by_id(state, "old-2").status, WorkerLeaseStatus.SUPERSEDED)


if __name__ == "__main__":
    unittest.main()
