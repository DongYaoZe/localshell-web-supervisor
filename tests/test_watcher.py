import unittest

from cws.models import BrowserObservation, LsmObservation, SupervisorState, TaskRecord
from cws.recovery import recommend
from cws.watcher import WatchPolicy, assess


NOW = 10_000.0


def task(state=SupervisorState.RUNNING):
    return TaskRecord(
        task_id="t1",
        project="p",
        objective="obj",
        cwd="C:/repo",
        state=state,
        lsm_session_id="s1",
        created_at=1,
        updated_at=1,
    )


def lsm(**kw):
    base = dict(
        task_id="t1",
        observed_at=NOW,
        session_id="s1",
        session_status="active",
        plan_status="active",
        plan_last_agent_activity=NOW - 10,
        continuation_due=False,
        continuation_pending=False,
        in_flight_calls=0,
        active_jobs=0,
        failed_jobs=0,
        succeeded_jobs=0,
        recent_event_type="tool.completed",
        recent_event_at=NOW - 10,
    )
    base.update(kw)
    return LsmObservation(**base)


class WatcherTests(unittest.TestCase):
    def test_lsm_inflight_wins_over_stale_ui(self):
        browser = BrowserObservation(
            worker_id="w1",
            observed_at=NOW,
            generating=False,
            send_button_ready=True,
            pending_tool_calls=1,
            last_dom_change_at=NOW - 1000,
        )
        result = assess(task(), browser, lsm(in_flight_calls=1), now=NOW)
        self.assertEqual(result.state, SupervisorState.RUNNING)

    def test_contradictory_ui_reconciles_not_replays(self):
        browser = BrowserObservation(
            worker_id="w1",
            observed_at=NOW,
            generating=False,
            send_button_ready=True,
            pending_tool_calls=1,
            last_dom_change_at=NOW - 300,
        )
        result = assess(task(), browser, lsm(recent_event_at=NOW - 300), now=NOW)
        self.assertEqual(result.state, SupervisorState.RECONCILING)
        self.assertTrue(result.requires_reconcile)

    def test_delivery_error_reconciles(self):
        browser = BrowserObservation(
            worker_id="w1",
            observed_at=NOW,
            visible_error="Message delivery timed out. Please try again.",
            last_dom_change_at=NOW - 10,
        )
        result = assess(task(), browser, lsm(), now=NOW)
        self.assertEqual(result.state, SupervisorState.RECONCILING)

    def test_continuation_pending_prevents_competing_recovery(self):
        result = assess(task(), None, lsm(continuation_pending=True, recent_event_at=NOW - 1000), now=NOW)
        self.assertEqual(result.state, SupervisorState.RUNNING)

    def test_execution_lease_due_is_suspect(self):
        result = assess(
            task(), None, lsm(continuation_due=True, recent_event_at=NOW - 1000), now=NOW
        )
        self.assertEqual(result.state, SupervisorState.SUSPECT)
        self.assertTrue(result.requires_reconcile)

    def test_completed_plan_is_completion_evidence(self):
        result = assess(task(), None, lsm(plan_status="completed"), now=NOW)
        self.assertEqual(result.state, SupervisorState.COMPLETED)

    def test_recovery_stays_advisory(self):
        assessment = assess(
            task(), None, lsm(continuation_due=True, recent_event_at=NOW - 1000), now=NOW
        )
        rec = recommend(task(), assessment, lsm(continuation_due=True))
        self.assertEqual(rec.action, "reconcile_then_continue")
        self.assertFalse(rec.safe_to_dispatch)
        self.assertIn("Do not repeat completed operations", rec.prompt)


if __name__ == "__main__":
    unittest.main()
