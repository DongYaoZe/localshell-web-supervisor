import unittest

from lws.models import (
    BrowserObservation,
    LsmObservation,
    NetworkObservation,
    SupervisorState,
    TaskRecord,
)
from lws.recovery import recommend
from lws.watcher import WatchPolicy, assess


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


def network(**kw):
    base = dict(
        worker_id="w1",
        observed_at=NOW,
        source="cdp",
        sample_started_at=NOW - 2,
        sample_ended_at=NOW,
        page_url="https://web.example/c/x",
        event_count=0,
        request_count=0,
        response_count=0,
        data_event_count=0,
        encoded_data_bytes=0,
        loading_finished=0,
        loading_failed=0,
        websocket_frames=0,
        last_activity_at=None,
        quiet_since_at=NOW - 2,
        inflight_requests=0,
    )
    base.update(kw)
    return NetworkObservation(**base)


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

    def test_recent_network_activity_conflicts_with_due_goal_lease(self):
        result = assess(
            task(),
            None,
            lsm(continuation_due=True, recent_event_at=NOW - 1000),
            network=network(
                event_count=5,
                last_activity_at=NOW - 5,
                quiet_since_at=NOW - 5,
            ),
            now=NOW,
        )
        self.assertEqual(result.state, SupervisorState.RECONCILING)
        self.assertTrue(result.requires_reconcile)
        self.assertIn("network lifecycle activity is recent", result.reason)

    def test_recent_network_activity_does_not_prove_running(self):
        browser = BrowserObservation(
            worker_id="w1",
            observed_at=NOW,
            generating=None,
            send_button_ready=None,
            last_dom_change_at=NOW - 400,
        )
        result = assess(
            task(),
            browser,
            lsm(recent_event_at=NOW - 400),
            network=network(
                event_count=4,
                last_activity_at=NOW - 5,
                quiet_since_at=NOW - 5,
            ),
            now=NOW,
        )
        self.assertEqual(result.state, SupervisorState.RECONCILING)
        self.assertNotEqual(result.state, SupervisorState.RUNNING)

    def test_triple_silence_is_high_confidence_suspect(self):
        browser = BrowserObservation(
            worker_id="w1",
            observed_at=NOW,
            generating=None,
            send_button_ready=None,
            last_dom_change_at=NOW - 800,
        )
        result = assess(
            task(),
            browser,
            lsm(recent_event_at=NOW - 800),
            network=network(
                last_activity_at=NOW - 800,
                quiet_since_at=NOW - 800,
            ),
            now=NOW,
        )
        self.assertEqual(result.state, SupervisorState.SUSPECT)
        self.assertEqual(result.confidence, "high")
        self.assertIn("network lifecycle", result.reason)

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
        self.assertIn("Do not bypass authentication or platform controls", rec.prompt)
        self.assertIn("Do not access, copy, or move cookies", rec.prompt)


if __name__ == "__main__":
    unittest.main()
