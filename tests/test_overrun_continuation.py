import tempfile
import unittest
from pathlib import Path

from lws.actions import build_action_attempt
from lws.dispatcher import DispatchAction, DispatchPlan
from lws.models import BrowserObservation, ReconciliationRecord, SupervisorState
from lws.overrun_continuation import (
    DEFAULT_OVERRUN_AFTER_S,
    OVERRUN_TRIGGER_KIND,
    OverrunContinuationPolicy,
    build_overrun_dispatch_plan,
    latest_observed_generation_started_at,
    overrun_clock,
)
from lws.registry import Registry


URL = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
NOW = 5000.0


class OverrunContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "registry.sqlite3"
        self.registry = Registry(self.db)
        self.task = self.registry.register_task(
            task_id="overrun-task",
            project="lws",
            objective="long work turn",
            cwd=self.tmp.name,
            conversation_url=URL,
        )
        self.worker = self.registry.get_worker(self.task.current_worker_id)
        self._set_worker_started_at(NOW - DEFAULT_OVERRUN_AFTER_S)
        self.task = self.registry.get_task(self.task.task_id)
        self.worker = self.registry.get_worker(self.task.current_worker_id)
        self.policy = OverrunContinuationPolicy(enabled=True)

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def _set_worker_started_at(self, value):
        self.registry._conn.execute(
            "UPDATE workers SET started_at=? WHERE worker_id=?",
            (float(value), self.worker.worker_id),
        )
        self.registry._conn.commit()

    def _browser(self, *, observed_at=NOW, generating=False, signature="stable"):
        return BrowserObservation(
            worker_id=self.worker.worker_id,
            observed_at=float(observed_at),
            url=URL,
            generating=generating,
            send_button_ready=not generating,
            last_dom_change_at=float(observed_at) - 10.0,
            message_signature=signature,
            raw={"source": "windows_uia_chrome", "composer_present": not generating},
        )

    def _reconciliation(self, name, created_at, *, state="RUNNING", fence="stable-fence"):
        return ReconciliationRecord(
            reconcile_id=name,
            task_id=self.task.task_id,
            created_at=float(created_at),
            state=state,
            confidence="high",
            reason="fixture",
            requires_reconcile=False,
            current_worker_id=self.worker.worker_id,
            fence_token=fence,
            fence_version=2,
            snapshot={},
        )

    def _action_plan(self, *, created_at=NOW):
        return DispatchPlan(
            task_id=self.task.task_id,
            created_at=float(created_at),
            action=DispatchAction.CONTINUE_CURRENT_WORKER,
            candidate_ready=True,
            transport_enabled=True,
            would_dispatch=True,
            reason="fixture",
            previous_reconcile_id="rec-a",
            current_reconcile_id="rec-b",
            fence_token="stable-fence",
        )

    def _record_continue(self, *, submitted_at, state="submitted"):
        attempt = build_action_attempt(
            self._action_plan(created_at=submitted_at),
            self.task,
            self.worker,
            "continue",
            fence_version=2,
            pre_action_signature="before",
            now=float(submitted_at) - 1.0,
        )
        attempt.metadata["trigger_kind"] = OVERRUN_TRIGGER_KIND
        self.registry.record_current_worker_action_attempt(attempt)
        if state == "submitted":
            self.registry.mark_action_submitted(
                attempt.attempt_id,
                transport_name="fake",
                submitted_at=float(submitted_at),
            )
        elif state == "failed":
            self.registry.fail_action_attempt(
                attempt.attempt_id,
                error="proven pre-send failure",
                transport_name="fake",
                now=float(submitted_at),
            )
        else:
            raise AssertionError(state)
        return attempt

    def test_exact_1520_second_boundary(self):
        self._set_worker_started_at(NOW - DEFAULT_OVERRUN_AFTER_S)
        clock = overrun_clock(self.registry, self.task.task_id, policy=self.policy, now=NOW)
        self.assertIsNotNone(clock)
        self.assertEqual(clock.elapsed_s, DEFAULT_OVERRUN_AFTER_S)
        self.assertTrue(clock.due)

        self._set_worker_started_at(NOW - DEFAULT_OVERRUN_AFTER_S + 1)
        clock = overrun_clock(self.registry, self.task.task_id, policy=self.policy, now=NOW)
        self.assertFalse(clock.due)
        self.assertEqual(clock.elapsed_s, DEFAULT_OVERRUN_AFTER_S - 1)

    def test_latest_manual_generation_episode_resets_timer_but_streaming_changes_do_not(self):
        rows = [
            self._browser(observed_at=4000, generating=False, signature="idle-before"),
            self._browser(observed_at=4100, generating=True, signature="stream-1"),
            self._browser(observed_at=4110, generating=True, signature="stream-2"),
            self._browser(observed_at=4120, generating=True, signature="stream-3"),
            self._browser(observed_at=4130, generating=False, signature="idle-after"),
        ]
        for row in rows:
            self.registry.record_browser_observation(row)
        self.assertEqual(
            latest_observed_generation_started_at(self.registry, self.worker.worker_id),
            4100.0,
        )
        clock = overrun_clock(self.registry, self.task.task_id, policy=self.policy, now=NOW)
        self.assertEqual(clock.anchor_at, 4100.0)
        self.assertFalse(clock.due)

    def test_submitted_continue_resets_timer_and_survives_registry_restart(self):
        self._record_continue(submitted_at=4500.0)
        clock = overrun_clock(self.registry, self.task.task_id, policy=self.policy, now=NOW)
        self.assertEqual(clock.anchor_at, 4500.0)
        self.assertFalse(clock.due)

        self.registry.close()
        self.registry = Registry(self.db)
        clock = overrun_clock(self.registry, self.task.task_id, policy=self.policy, now=NOW)
        self.assertEqual(clock.anchor_at, 4500.0)
        self.assertIsNotNone(self.registry.unresolved_action_attempt(self.task.task_id))

    def test_failed_pre_send_attempt_does_not_reset_clock(self):
        original_anchor = self.worker.started_at
        self._record_continue(submitted_at=NOW - 30.0, state="failed")
        clock = overrun_clock(self.registry, self.task.task_id, policy=self.policy, now=NOW)
        self.assertEqual(clock.anchor_at, original_anchor)
        self.assertTrue(clock.due)

    def test_stable_running_task_is_allowed_even_when_fault_recovery_budget_is_exhausted(self):
        self.registry._conn.execute(
            "UPDATE tasks SET recovery_attempts=max_recovery_attempts WHERE task_id=?",
            (self.task.task_id,),
        )
        self.registry._conn.commit()
        task = self.registry.get_task(self.task.task_id)
        browser = self._browser()
        clock = overrun_clock(self.registry, task.task_id, policy=self.policy, now=NOW)
        previous = self._reconciliation("rec-a", NOW - 10)
        current = self._reconciliation("rec-b", NOW - 2)
        plan = build_overrun_dispatch_plan(
            self.registry,
            task,
            clock=clock,
            previous=previous,
            current=current,
            browser=browser,
            policy=self.policy,
            transport_enabled=True,
            now=NOW,
        )
        self.assertTrue(plan.candidate_ready)
        self.assertTrue(plan.would_dispatch)
        self.assertNotIn("recovery_budget_available", plan.checks)

    def test_active_generation_terminal_state_and_unstable_fence_block(self):
        task = self.registry.get_task(self.task.task_id)
        clock = overrun_clock(self.registry, task.task_id, policy=self.policy, now=NOW)
        previous = self._reconciliation("rec-a", NOW - 10)

        generating = build_overrun_dispatch_plan(
            self.registry,
            task,
            clock=clock,
            previous=previous,
            current=self._reconciliation("rec-b", NOW - 2),
            browser=self._browser(generating=True),
            policy=self.policy,
            transport_enabled=True,
            now=NOW,
        )
        self.assertFalse(generating.would_dispatch)
        self.assertFalse(generating.checks["browser_not_generating"])

        terminal_assessment = build_overrun_dispatch_plan(
            self.registry,
            task,
            clock=clock,
            previous=previous,
            current=self._reconciliation("rec-c", NOW - 2, state=SupervisorState.COMPLETED.value),
            browser=self._browser(),
            policy=self.policy,
            transport_enabled=True,
            now=NOW,
        )
        self.assertFalse(terminal_assessment.would_dispatch)
        self.assertFalse(terminal_assessment.checks["assessment_allows_overrun_nudge"])

        unstable = build_overrun_dispatch_plan(
            self.registry,
            task,
            clock=clock,
            previous=previous,
            current=self._reconciliation("rec-d", NOW - 2, fence="changed"),
            browser=self._browser(),
            policy=self.policy,
            transport_enabled=True,
            now=NOW,
        )
        self.assertFalse(unstable.would_dispatch)
        self.assertFalse(unstable.checks["fence_stable"])

    def test_failed_attempt_enforces_retry_cooldown(self):
        self._record_continue(submitted_at=NOW - 30.0, state="failed")
        task = self.registry.get_task(self.task.task_id)
        browser = self._browser()
        clock = overrun_clock(self.registry, task.task_id, policy=self.policy, now=NOW)
        previous = self._reconciliation("rec-a", NOW - 10)
        current = self._reconciliation("rec-b", NOW - 2)
        blocked = build_overrun_dispatch_plan(
            self.registry,
            task,
            clock=clock,
            previous=previous,
            current=current,
            browser=browser,
            policy=self.policy,
            transport_enabled=True,
            now=NOW,
        )
        self.assertFalse(blocked.would_dispatch)
        self.assertFalse(blocked.checks["overrun_retry_cooldown_elapsed"])

        allowed = build_overrun_dispatch_plan(
            self.registry,
            task,
            clock=clock,
            previous=self._reconciliation("rec-c", NOW + 91),
            current=self._reconciliation("rec-d", NOW + 99),
            browser=self._browser(observed_at=NOW + 100),
            policy=self.policy,
            transport_enabled=True,
            now=NOW + 100,
        )
        self.assertTrue(allowed.checks["overrun_retry_cooldown_elapsed"])


if __name__ == "__main__":
    unittest.main()
