import unittest

from cws.dispatcher import (
    DispatchAction,
    DispatchDisabled,
    DispatchPolicy,
    build_dispatch_plan,
    execute_dispatch,
)
from cws.models import (
    Assessment,
    BrowserObservation,
    LsmObservation,
    NetworkObservation,
    RecoveryRecommendation,
    SupervisorState,
    TaskRecord,
    WorkerRecord,
    WorkerStatus,
    WorkspaceObservation,
)
from cws.reconcile import build_reconciliation_record


NOW = 1000.0


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


def worker(*, url="https://chatgpt.com/c/x", status=WorkerStatus.ACTIVE):
    return WorkerRecord(
        worker_id="w1",
        task_id="t1",
        conversation_url=url,
        conversation_id="x",
        status=status,
        started_at=1,
    )


def assessment():
    return Assessment(
        state=SupervisorState.SUSPECT,
        reason="triple-signal silence",
        confidence="high",
        evidence=["browser.dom_silence_s=100", "lsm.event_silence_s=100"],
        requires_reconcile=True,
    )


def browser(
    *,
    signature="sig",
    generating=False,
    send_ready=True,
    composer_present=True,
    observed_at=NOW,
):
    return BrowserObservation(
        worker_id="w1",
        observed_at=observed_at,
        url="https://chatgpt.com/c/x",
        generating=generating,
        send_button_ready=send_ready,
        pending_tool_calls=1,
        visible_error=None,
        last_dom_change_at=NOW - 100,
        message_signature=signature,
        raw={"composer_present": composer_present},
    )


def lsm(*, inflight=0, active_jobs=0, continuation_pending=False):
    return LsmObservation(
        task_id="t1",
        observed_at=NOW,
        session_id="s1",
        session_status="active",
        active_run_id="r1",
        plan_status="active",
        plan_last_agent_activity=NOW - 100,
        continuation_due=True,
        continuation_pending=continuation_pending,
        in_flight_calls=inflight,
        active_jobs=active_jobs,
        recent_event_type="tool.completed",
        recent_event_at=NOW - 100,
    )


def workspace(*, head="abc"):
    return WorkspaceObservation(
        task_id="t1",
        observed_at=NOW,
        cwd="C:/repo",
        cwd_exists=True,
        is_git_repo=True,
        git_root="C:/repo",
        git_head=head,
        git_dirty=False,
        git_status_hash="status-hash",
    )


def network(*, quiet_since=NOW - 100, observed_at=NOW):
    return NetworkObservation(
        worker_id="w1",
        observed_at=observed_at,
        source="cdp",
        sample_started_at=NOW - 2,
        sample_ended_at=NOW,
        page_url="https://chatgpt.com/c/x",
        event_count=0,
        last_activity_at=quiet_since,
        quiet_since_at=quiet_since,
    )


def record(*, created_at, browser_obs=None, lsm_obs=None, workspace_obs=None, network_obs=None):
    return build_reconciliation_record(
        task(),
        assessment(),
        worker=worker(),
        browser=browser_obs if browser_obs is not None else browser(),
        network=network_obs,
        lsm=lsm_obs if lsm_obs is not None else lsm(),
        workspace=workspace_obs if workspace_obs is not None else workspace(),
        created_at=created_at,
    )


def recommendation():
    return RecoveryRecommendation(
        task_id="t1",
        action="reconcile_then_continue",
        safe_to_dispatch=False,
        reason="candidate after reconciliation",
        prompt="safe prompt",
    )


class DispatcherTests(unittest.TestCase):
    def test_two_matching_fences_can_be_candidate_but_transport_stays_disabled(self):
        previous = record(created_at=NOW - 20)
        current = record(created_at=NOW - 10)
        plan = build_dispatch_plan(
            task(),
            recommendation(),
            previous=previous,
            current=current,
            now=NOW,
        )
        self.assertEqual(plan.action, DispatchAction.CONTINUE_CURRENT_WORKER)
        self.assertTrue(plan.candidate_ready)
        self.assertFalse(plan.transport_enabled)
        self.assertFalse(plan.would_dispatch)
        self.assertEqual(plan.blockers, [])
        self.assertIn("dry-run only", plan.reason)

    def test_single_fence_is_not_enough(self):
        current = record(created_at=NOW - 10)
        plan = build_dispatch_plan(
            task(), recommendation(), previous=None, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertTrue(any("previous reconciliation" in blocker for blocker in plan.blockers))

    def test_changed_fence_blocks_dispatch(self):
        previous = record(created_at=NOW - 20)
        current = record(created_at=NOW - 10, workspace_obs=workspace(head="def"))
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["fence_stable"])

    def test_lsm_inflight_blocks_dispatch(self):
        previous = record(created_at=NOW - 20, lsm_obs=lsm(inflight=1))
        current = record(created_at=NOW - 10, lsm_obs=lsm(inflight=1))
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertIn("LSM has an in-flight tool call", plan.blockers)

    def test_generating_or_unready_composer_blocks_dispatch(self):
        previous = record(
            created_at=NOW - 20,
            browser_obs=browser(generating=True, send_ready=None, composer_present=False),
        )
        current = record(
            created_at=NOW - 10,
            browser_obs=browser(generating=True, send_ready=None, composer_present=False),
        )
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertIn("browser still reports active generation", plan.blockers)
        self.assertIn("no positive composer-presence evidence", plan.blockers)

    def test_empty_but_present_composer_is_recovery_candidate(self):
        previous = record(
            created_at=NOW - 20,
            browser_obs=browser(send_ready=False, composer_present=True),
        )
        current = record(
            created_at=NOW - 10,
            browser_obs=browser(send_ready=False, composer_present=True),
        )
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertTrue(plan.candidate_ready)
        self.assertTrue(plan.checks["composer_available"])

    def test_recent_network_activity_blocks_dispatch_when_network_is_observed(self):
        previous = record(created_at=NOW - 20, network_obs=network(quiet_since=NOW - 1))
        current = record(created_at=NOW - 10, network_obs=network(quiet_since=NOW - 1))
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertTrue(
            any("network lifecycle is not stably quiet" in blocker for blocker in plan.blockers)
        )

    def test_missing_browser_becomes_blocked_takeover_design(self):
        previous = build_reconciliation_record(
            task(), assessment(), worker=worker(), browser=None, network=None,
            lsm=lsm(), workspace=workspace(), created_at=NOW - 20
        )
        current = build_reconciliation_record(
            task(), assessment(), worker=worker(), browser=None, network=None,
            lsm=lsm(), workspace=workspace(), created_at=NOW - 10
        )
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertEqual(plan.action, DispatchAction.TAKEOVER_NEW_WORKER)
        self.assertFalse(plan.candidate_ready)
        self.assertTrue(any("new-worker binding" in blocker for blocker in plan.blockers))

    def test_stale_reconciliation_blocks_dispatch(self):
        previous = record(created_at=NOW - 500)
        current = record(created_at=NOW - 400)
        plan = build_dispatch_plan(
            task(),
            recommendation(),
            previous=previous,
            current=current,
            policy=DispatchPolicy(max_reconciliation_age_s=120),
            now=NOW,
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["current_reconciliation_fresh"])

    def test_stale_previous_reconciliation_blocks_even_with_fresh_current(self):
        previous = record(created_at=NOW - 500)
        current = record(created_at=NOW - 10)
        plan = build_dispatch_plan(
            task(),
            recommendation(),
            previous=previous,
            current=current,
            policy=DispatchPolicy(max_reconciliation_age_s=120),
            now=NOW,
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["previous_reconciliation_fresh"])

    def test_matching_samples_must_be_separated_long_enough(self):
        previous = record(created_at=NOW - 2)
        current = record(created_at=NOW - 1)
        plan = build_dispatch_plan(
            task(),
            recommendation(),
            previous=previous,
            current=current,
            policy=DispatchPolicy(min_reconciliation_separation_s=3),
            now=NOW,
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["reconciliation_separation_sufficient"])

    def test_recovery_budget_blocks_dispatch(self):
        t = task()
        t.recovery_attempts = t.max_recovery_attempts
        previous = record(created_at=NOW - 20)
        current = record(created_at=NOW - 10)
        plan = build_dispatch_plan(
            t, recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertIn("recovery attempt budget is exhausted", plan.blockers)

    def test_stale_browser_observation_blocks_candidate(self):
        previous = record(
            created_at=NOW - 20,
            browser_obs=browser(observed_at=NOW - 100),
        )
        current = record(
            created_at=NOW - 10,
            browser_obs=browser(observed_at=NOW - 100),
        )
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["browser_observation_fresh"])

    def test_stale_network_observation_blocks_candidate(self):
        previous = record(
            created_at=NOW - 20,
            network_obs=network(quiet_since=NOW - 100, observed_at=NOW - 100),
        )
        current = record(
            created_at=NOW - 10,
            network_obs=network(quiet_since=NOW - 100, observed_at=NOW - 100),
        )
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["network_observation_fresh"])

    def test_registered_worker_url_must_match_observed_url(self):
        previous = build_reconciliation_record(
            task(), assessment(), worker=worker(url="https://chatgpt.com/c/expected"),
            browser=browser(), network=None, lsm=lsm(), workspace=workspace(),
            created_at=NOW - 20,
        )
        current = build_reconciliation_record(
            task(), assessment(), worker=worker(url="https://chatgpt.com/c/expected"),
            browser=browser(), network=None, lsm=lsm(), workspace=workspace(),
            created_at=NOW - 10,
        )
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["browser_url_matches_registered"])

    def test_parked_worker_is_not_a_candidate(self):
        previous = build_reconciliation_record(
            task(), assessment(), worker=worker(status=WorkerStatus.PARKED),
            browser=browser(), network=None, lsm=lsm(), workspace=workspace(),
            created_at=NOW - 20,
        )
        current = build_reconciliation_record(
            task(), assessment(), worker=worker(status=WorkerStatus.PARKED),
            browser=browser(), network=None, lsm=lsm(), workspace=workspace(),
            created_at=NOW - 10,
        )
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        self.assertFalse(plan.candidate_ready)
        self.assertFalse(plan.checks["registered_worker_active"])

    def test_execute_dispatch_is_unconditionally_disabled(self):
        previous = record(created_at=NOW - 20)
        current = record(created_at=NOW - 10)
        plan = build_dispatch_plan(
            task(), recommendation(), previous=previous, current=current, now=NOW
        )
        with self.assertRaises(DispatchDisabled):
            execute_dispatch(plan)


if __name__ == "__main__":
    unittest.main()
