import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cws.actions import ActionAttempt, ActionAttemptState
from cws.capabilities import PAGE_CLOSE_EVALUATOR_VERSION, runtime_context
from cws.models import (
    Assessment,
    LsmObservation,
    PageCapabilityKind,
    PageCapabilityRecord,
    SupervisorState,
    WorkspaceObservation,
)
from cws.orchestration import OrchestrationDecisionKind, OrchestrationPolicy
from cws.orchestration_adapter import (
    AdvisoryOrchestrationAdapter,
    PageContinuityRequest,
    TaskRuntimeHints,
    TaskSchedulingHistory,
)
from cws.reconcile import build_reconciliation_record
from cws.registry import Registry


NOW = 2_000_000_000.0


class FakeLsmTelemetry:
    def __init__(self, rows):
        self.rows = rows

    def observe(self, *, task_id, session_id, tracked_job_ids):
        value = self.rows[task_id]
        if isinstance(value, Exception):
            raise value
        if value.session_id != session_id:
            raise AssertionError("unexpected session id")
        return value


class FakeWorkspaceProbe:
    def __init__(self, rows):
        self.rows = rows

    def observe(self, *, task_id, cwd):
        value = self.rows[task_id]
        if isinstance(value, Exception):
            raise value
        if value.cwd != cwd:
            raise AssertionError("unexpected cwd")
        return value


class OrchestrationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.tempdir.name) / "cws.sqlite3")
        self.lsm_rows = {}
        self.workspace_rows = {}
        self.adapter = AdvisoryOrchestrationAdapter(
            self.registry,
            lsm_telemetry=FakeLsmTelemetry(self.lsm_rows),
            workspace_probe=FakeWorkspaceProbe(self.workspace_rows),
        )

    def tearDown(self):
        self.registry.close()
        self.tempdir.cleanup()

    def register_task(self, task_id, state=SupervisorState.SUSPECT):
        self.registry.register_task(
            task_id=task_id,
            project="chatgpt-web-supervisor",
            objective="test advisory orchestration",
            cwd=f"C:/repo/{task_id}",
            lsm_session_id=f"s-{task_id}",
            conversation_url=f"https://chatgpt.com/c/{task_id}",
        )
        self.registry.update_state(task_id, state)
        self.registry.set_checkpoint(task_id, {"git_head": "abc"})
        task = self.registry.get_task(task_id)
        self.lsm_rows[task_id] = self.make_lsm(task)
        self.workspace_rows[task_id] = self.make_workspace(task)
        return task

    def make_lsm(self, task, **changes):
        values = {
            "task_id": task.task_id,
            "observed_at": NOW - 5,
            "session_id": task.lsm_session_id,
            "session_status": "active",
            "active_run_id": f"r-{task.task_id}",
            "plan_status": "active",
            "plan_last_agent_activity": NOW - 500,
            "continuation_due": True,
            "continuation_pending": False,
            "in_flight_calls": 0,
            "active_jobs": 0,
            "failed_jobs": 0,
            "succeeded_jobs": 0,
            "recent_event_type": "tool.completed",
            "recent_event_at": NOW - 500,
        }
        values.update(changes)
        return LsmObservation(**values)

    def make_workspace(self, task, **changes):
        values = {
            "task_id": task.task_id,
            "observed_at": NOW - 5,
            "cwd": task.cwd,
            "cwd_exists": True,
            "is_git_repo": True,
            "git_root": task.cwd,
            "git_head": "abc",
            "git_dirty": False,
            "git_status_hash": "clean-status",
        }
        values.update(changes)
        return WorkspaceObservation(**values)

    def record_fence(self, task_id, *, created_at, lsm=None, workspace=None):
        task = self.registry.get_task(task_id)
        worker = self.registry.get_worker(task.current_worker_id)
        lsm = lsm or self.lsm_rows[task_id]
        workspace = workspace or self.workspace_rows[task_id]
        assessment_state = (
            task.state
            if task.state
            in {
                SupervisorState.SUSPECT,
                SupervisorState.RECONCILING,
                SupervisorState.RECOVERING,
            }
            else SupervisorState.SUSPECT
        )
        assessment = Assessment(
            state=assessment_state,
            reason="stable stalled evidence",
            confidence="high",
            requires_reconcile=True,
        )
        record = build_reconciliation_record(
            task,
            assessment,
            worker=worker,
            browser=None,
            network=None,
            lsm=lsm,
            workspace=workspace,
            created_at=created_at,
        )
        self.registry.record_reconciliation(record)
        return record

    def bind_window(self, task_id, *, observed_at=NOW - 5, ttl_s=60):
        task = self.registry.get_task(task_id)
        worker = self.registry.get_worker(task.current_worker_id)
        return self.registry.bind_worker_window(
            worker.worker_id,
            window_handle=1000 + len(task_id),
            browser_pid=4000 + len(task_id),
            chrome_executable=r"C:\Chrome\chrome.exe",
            conversation_url=worker.conversation_url,
            observed_at=observed_at,
            ttl_s=ttl_s,
        )

    def ready_task(self, task_id="t1", state=SupervisorState.SUSPECT):
        task = self.register_task(task_id, state)
        self.record_fence(task_id, created_at=NOW - 30)
        self.record_fence(task_id, created_at=NOW - 20)
        self.bind_window(task_id)
        return task

    def runtime(self, **changes):
        values = {"worker_lease_expires_at": NOW + 60}
        values.update(changes)
        return TaskRuntimeHints(**values)

    def schedule(self, last_scheduled_at=NOW - 100, consecutive_waits=0):
        return TaskSchedulingHistory(
            last_scheduled_at=last_scheduled_at,
            consecutive_waits=consecutive_waits,
        )

    def action_attempt(self, task_id, attempt_id, *, created_at, state=ActionAttemptState.ARMED):
        task = self.registry.get_task(task_id)
        values = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "worker_id": task.current_worker_id,
            "action": "CONTINUE_CURRENT_WORKER",
            "fence_token": "semantic-fence",
            "fence_version": 2,
            "prompt_hash": "prompt-digest",
            "nonce": f"nonce-{attempt_id}",
            "state": state,
            "created_at": created_at,
            "updated_at": created_at,
        }
        return ActionAttempt(**values)

    def consume_recovery_attempt(self, task_id, index, *, created_at):
        attempt = self.action_attempt(
            task_id,
            f"{task_id}-attempt-{index}",
            created_at=created_at,
        )
        self.registry.record_recovery_action_attempt(attempt)
        self.registry.fail_action_attempt(
            attempt.attempt_id,
            error="proved no side effect",
            now=created_at + 0.1,
        )

    def record_page_capability(self):
        capability = PageCapabilityRecord(
            capability_id="cap-adapter-test",
            kind=PageCapabilityKind.GENERATION,
            scope_host="chatgpt.com",
            browser_family="chrome",
            browser_major=151,
            platform="windows",
            surface="normal_chrome_uia",
            isolation_mode="existing_profile_disposable_window",
            evaluator_version=PAGE_CLOSE_EVALUATOR_VERSION,
            evidence_digest="digest-adapter-test",
            source_experiment_id="experiment-adapter-test",
            observed_at=NOW - 100,
            recorded_at=NOW - 90,
            expires_at=NOW + 100,
        )
        self.registry.record_page_capability(capability)
        return capability

    def test_healthy_running_task_stays_observe_without_browser(self):
        task = self.register_task("healthy", SupervisorState.RUNNING)
        self.lsm_rows[task.task_id] = self.make_lsm(
            task,
            continuation_due=False,
            plan_last_agent_activity=NOW - 5,
            recent_event_at=NOW - 5,
        )
        decision = self.adapter.evaluate(task.task_id, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.OBSERVE)
        self.assertFalse(decision.mutation_allowed)
        self.assertFalse(decision.selected)

    def test_active_lsm_call_job_or_continuation_cannot_recommend_action(self):
        task = self.register_task("active-work")
        variants = [
            {"in_flight_calls": 1},
            {"active_jobs": 1},
            {"continuation_pending": True},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                self.lsm_rows[task.task_id] = self.make_lsm(task, **changes)
                decision = self.adapter.evaluate(task.task_id, now=NOW)
                self.assertEqual(decision.kind, OrchestrationDecisionKind.OBSERVE)
                self.assertNotEqual(decision.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
                self.assertFalse(decision.mutation_allowed)

    def test_dirty_changed_or_stale_workspace_blocks_action_recommendation(self):
        task = self.ready_task("workspace")
        cases = [
            self.make_workspace(task, git_dirty=True, git_status_hash="dirty-status"),
            self.make_workspace(task, git_head="def"),
            self.make_workspace(task, observed_at=NOW - 100),
        ]
        for workspace in cases:
            with self.subTest(workspace=workspace):
                self.workspace_rows[task.task_id] = workspace
                decision = self.adapter.evaluate(task.task_id, runtime=self.runtime(), now=NOW)
                self.assertEqual(decision.kind, OrchestrationDecisionKind.RECONCILE)
                self.assertFalse(decision.mutation_allowed)
        self.workspace_rows[task.task_id] = self.make_workspace(task)

    def test_unresolved_action_attempt_blocks(self):
        task = self.ready_task("action-lock")
        attempt = self.action_attempt(
            task.task_id,
            "unresolved-action",
            created_at=NOW - 10,
            state=ActionAttemptState.SUBMITTED,
        )
        self.registry.record_action_attempt(attempt)
        decision = self.adapter.evaluate(task.task_id, runtime=self.runtime(), now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.BLOCKED_HUMAN)
        self.assertTrue(any("unresolved action" in blocker for blocker in decision.blockers))
        self.assertFalse(decision.mutation_allowed)

    def test_unresolved_probe_mutation_blocks_page_continuity_recommendation(self):
        task = self.ready_task("page-reopen")
        self.record_page_capability()
        page_request = PageContinuityRequest(runtime_context(browser_major=151))
        runtime = self.runtime(page_continuity=page_request)
        probe = SimpleNamespace(
            operation_id="probe-operation",
            state=SimpleNamespace(value="OPEN_SUBMITTED"),
        )
        with patch.object(
            self.registry,
            "unresolved_probe_mutation_operation",
            return_value=probe,
        ):
            decision = self.adapter.evaluate(task.task_id, runtime=runtime, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.BLOCKED_HUMAN)
        self.assertTrue(any("probe mutation" in blocker for blocker in decision.blockers))
        self.assertFalse(decision.mutation_allowed)

    def test_two_stable_fresh_semantic_fences_are_required(self):
        task = self.register_task("two-fences")
        self.bind_window(task.task_id)
        self.record_fence(task.task_id, created_at=NOW - 20)
        one = self.adapter.evaluate(task.task_id, runtime=self.runtime(), now=NOW)
        self.assertEqual(one.kind, OrchestrationDecisionKind.RECONCILE)

        self.record_fence(task.task_id, created_at=NOW - 10)
        two = self.adapter.evaluate(task.task_id, runtime=self.runtime(), now=NOW)
        self.assertEqual(two.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
        self.assertFalse(two.mutation_allowed)

    def test_live_evidence_change_after_stable_fences_requires_reconciliation(self):
        task = self.ready_task("live-change")
        self.lsm_rows[task.task_id] = replace(
            self.lsm_rows[task.task_id],
            recent_event_type="plan.updated",
            recent_event_at=NOW - 450,
        )
        decision = self.adapter.evaluate(task.task_id, runtime=self.runtime(), now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.RECONCILE)
        self.assertTrue(any("latest reconciliation fence" in blocker for blocker in decision.blockers))

    def test_stale_worker_lease_and_future_timestamps_fail_closed(self):
        task = self.ready_task("timestamps")
        stale_lease = self.adapter.evaluate(
            task.task_id,
            runtime=self.runtime(worker_lease_expires_at=NOW),
            now=NOW,
        )
        self.assertEqual(stale_lease.kind, OrchestrationDecisionKind.RECONCILE)

        self.workspace_rows[task.task_id] = self.make_workspace(task, observed_at=NOW + 1)
        future_workspace = self.adapter.evaluate(
            task.task_id,
            runtime=self.runtime(),
            now=NOW,
        )
        self.assertEqual(future_workspace.kind, OrchestrationDecisionKind.RECONCILE)
        self.assertTrue(any("future" in blocker for blocker in future_workspace.blockers))

    def test_recovery_budget_exhaustion_and_cooldown_use_durable_action_history(self):
        cooldown_task = self.register_task("cooldown")
        self.consume_recovery_attempt(cooldown_task.task_id, 1, created_at=NOW - 10)
        self.record_fence(cooldown_task.task_id, created_at=NOW - 8)
        self.record_fence(cooldown_task.task_id, created_at=NOW - 4)
        self.bind_window(cooldown_task.task_id)
        cooldown = self.adapter.evaluate(
            cooldown_task.task_id,
            runtime=self.runtime(),
            now=NOW,
            policy=OrchestrationPolicy(recovery_cooldown_s=60),
        )
        self.assertEqual(cooldown.kind, OrchestrationDecisionKind.WAIT_COOLDOWN)

        budget_task = self.register_task("budget")
        for index in range(3):
            self.consume_recovery_attempt(
                budget_task.task_id,
                index,
                created_at=NOW - 100 + index,
            )
        self.record_fence(budget_task.task_id, created_at=NOW - 20)
        self.record_fence(budget_task.task_id, created_at=NOW - 10)
        self.bind_window(budget_task.task_id)
        exhausted = self.adapter.evaluate(
            budget_task.task_id,
            runtime=self.runtime(),
            now=NOW,
        )
        self.assertEqual(exhausted.kind, OrchestrationDecisionKind.BLOCKED_HUMAN)
        self.assertTrue(any("budget" in blocker for blocker in exhausted.blockers))

    def test_missing_durable_cooldown_timestamp_fails_closed(self):
        task = self.ready_task("missing-cooldown")
        self.registry._conn.execute(
            "UPDATE tasks SET recovery_attempts = 1 WHERE task_id = ?",
            (task.task_id,),
        )
        self.registry._conn.commit()
        self.record_fence(task.task_id, created_at=NOW - 10)
        decision = self.adapter.evaluate(task.task_id, runtime=self.runtime(), now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.RECONCILE)
        self.assertTrue(any("cooldown timestamp" in blocker for blocker in decision.blockers))

    def test_missing_scheduler_history_withholds_batch_selection(self):
        task = self.ready_task("no-schedule")
        decisions = self.adapter.plan(
            [task.task_id],
            runtime_hints={task.task_id: self.runtime()},
            now=NOW,
        )
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertEqual(decision.kind, OrchestrationDecisionKind.WAIT_COOLDOWN)
        self.assertEqual(decision.deferred_kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
        self.assertFalse(decision.selected)
        self.assertTrue(any("scheduling history" in blocker for blocker in decision.blockers))
        self.assertFalse(decision.mutation_allowed)

    def test_fair_multi_task_plan_is_stable_bounded_and_duplicate_free(self):
        tasks = [self.ready_task(name, state) for name, state in [
            ("a", SupervisorState.RECOVERING),
            ("b", SupervisorState.RECONCILING),
            ("c", SupervisorState.SUSPECT),
        ]]
        runtime_hints = {task.task_id: self.runtime() for task in tasks}
        scheduling = {
            "a": self.schedule(NOW - 200),
            "b": self.schedule(NOW - 300),
            "c": self.schedule(NOW - 100),
        }
        decisions = self.adapter.plan(
            ["c", "a", "b", "a"],
            runtime_hints=runtime_hints,
            scheduling_history=scheduling,
            now=NOW,
            policy=OrchestrationPolicy(max_selected_tasks=1),
        )
        self.assertEqual([decision.task_id for decision in decisions], ["a", "b", "c"])
        self.assertEqual(len({decision.task_id for decision in decisions}), 3)
        self.assertEqual([decision.task_id for decision in decisions if decision.selected], ["b"])
        self.assertEqual(
            next(decision for decision in decisions if decision.task_id == "b").kind,
            OrchestrationDecisionKind.RECOMMEND_ACTION,
        )
        self.assertTrue(all(not decision.mutation_allowed for decision in decisions))

    def test_invalid_future_scheduler_history_is_never_selected(self):
        task = self.ready_task("future-schedule")
        decisions = self.adapter.plan(
            [task.task_id],
            runtime_hints={task.task_id: self.runtime()},
            scheduling_history={task.task_id: self.schedule(NOW + 1)},
            now=NOW,
        )
        decision = decisions[0]
        self.assertFalse(decision.selected)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.WAIT_COOLDOWN)
        self.assertTrue(any("scheduler history" in blocker for blocker in decision.blockers))
        self.assertFalse(decision.mutation_allowed)


if __name__ == "__main__":
    unittest.main()
