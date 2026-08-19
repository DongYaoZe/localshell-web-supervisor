import unittest
from dataclasses import replace

from lws.actions import ActionAttempt, ActionAttemptState
from lws.capabilities import PAGE_CLOSE_EVALUATOR_VERSION, runtime_context
from lws.models import (
    Assessment,
    LsmObservation,
    PageCapabilityKind,
    PageCapabilityRecord,
    SupervisorState,
    TaskRecord,
    WorkerRecord,
    WorkerStatus,
    WorkerWindowBinding,
    WorkspaceObservation,
)
from lws.orchestration import (
    OrchestrationDecisionKind,
    OrchestrationPolicy,
    TaskOrchestrationInput,
    evaluate_task,
    plan_orchestration,
)
from lws.reconcile import build_reconciliation_record


NOW = 1000.0
URL = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def make_task(task_id="t1", state=SupervisorState.SUSPECT, *, created_at=100.0):
    return TaskRecord(
        task_id=task_id,
        project="p",
        objective="obj",
        cwd="C:/repo",
        state=state,
        lsm_session_id=f"s-{task_id}",
        checkpoint={"git_head": "abc"},
        current_worker_id=f"w-{task_id}",
        recovery_attempts=0,
        max_recovery_attempts=3,
        created_at=created_at,
        updated_at=900.0,
    )


def make_assessment(state=SupervisorState.SUSPECT):
    return Assessment(
        state=state,
        reason="stalled after stable silence",
        confidence="high",
        evidence=["stable-silence"],
        requires_reconcile=True,
    )


def make_worker(task):
    return WorkerRecord(
        worker_id=task.current_worker_id,
        task_id=task.task_id,
        conversation_url=URL,
        conversation_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        status=WorkerStatus.ACTIVE,
        started_at=100.0,
        last_seen_at=995.0,
    )


def make_lsm(task, **changes):
    values = dict(
        task_id=task.task_id,
        observed_at=995.0,
        session_id=task.lsm_session_id,
        session_status="active",
        active_run_id=f"r-{task.task_id}",
        plan_status="active",
        plan_last_agent_activity=900.0,
        continuation_due=True,
        continuation_pending=False,
        in_flight_calls=0,
        active_jobs=0,
        failed_jobs=0,
        succeeded_jobs=1,
        recent_event_type="tool.completed",
        recent_event_at=900.0,
    )
    values.update(changes)
    return LsmObservation(**values)


def make_workspace(task, **changes):
    values = dict(
        task_id=task.task_id,
        observed_at=995.0,
        cwd=task.cwd,
        cwd_exists=True,
        is_git_repo=True,
        git_root=task.cwd,
        git_head="abc",
        git_dirty=False,
        git_status_hash="clean-hash",
    )
    values.update(changes)
    return WorkspaceObservation(**values)


def make_binding(task, *, expires_at=1030.0):
    return WorkerWindowBinding(
        worker_id=task.current_worker_id,
        window_handle=1234,
        browser_pid=4321,
        chrome_executable=r"C:\Chrome\chrome.exe",
        conversation_url=URL,
        source="windows_uia_chrome",
        bound_at=990.0,
        observed_at=995.0,
        expires_at=expires_at,
    )


def make_reconciliations(task, worker, lsm, workspace):
    assessment = make_assessment(task.state)
    previous = build_reconciliation_record(
        task,
        assessment,
        worker=worker,
        browser=None,
        network=None,
        lsm=lsm,
        workspace=workspace,
        created_at=970.0,
    )
    current = build_reconciliation_record(
        task,
        assessment,
        worker=worker,
        browser=None,
        network=None,
        lsm=replace(lsm, observed_at=996.0),
        workspace=replace(workspace, observed_at=996.0),
        created_at=980.0,
    )
    return previous, current


def candidate_input(
    task_id="t1",
    state=SupervisorState.SUSPECT,
    *,
    created_at=100.0,
    last_scheduled_at=None,
    consecutive_waits=0,
):
    task = make_task(task_id, state, created_at=created_at)
    worker = make_worker(task)
    lsm = make_lsm(task)
    workspace = make_workspace(task)
    previous, current = make_reconciliations(task, worker, lsm, workspace)
    return TaskOrchestrationInput(
        task=task,
        assessment=make_assessment(state),
        worker=worker,
        lsm=lsm,
        workspace=workspace,
        previous_reconciliation=previous,
        current_reconciliation=current,
        worker_lease_expires_at=1030.0,
        last_scheduled_at=last_scheduled_at,
        consecutive_waits=consecutive_waits,
        window_binding=make_binding(task),
    )


def unresolved_attempt(item, state=ActionAttemptState.SUBMITTED):
    return ActionAttempt(
        attempt_id=f"act-{item.task.task_id}",
        task_id=item.task.task_id,
        worker_id=item.task.current_worker_id,
        action="CONTINUE_CURRENT_WORKER",
        fence_token="fence",
        fence_version=2,
        prompt_hash="hash",
        nonce="nonce",
        state=state,
        created_at=900.0,
        updated_at=901.0,
    )


def page_capability(*, expires_at=1100.0, browser_major=151):
    return PageCapabilityRecord(
        capability_id="cap-test",
        kind=PageCapabilityKind.GENERATION,
        scope_host="chatgpt.com",
        browser_family="chrome",
        browser_major=browser_major,
        platform="windows",
        surface="normal_chrome_uia",
        isolation_mode="existing_profile_disposable_window",
        evaluator_version=PAGE_CLOSE_EVALUATOR_VERSION,
        evidence_digest="digest",
        source_experiment_id="exp-test",
        observed_at=900.0,
        recorded_at=901.0,
        expires_at=expires_at,
    )


class OrchestrationPolicyTests(unittest.TestCase):
    def test_all_gates_only_recommend_action_and_never_enable_mutation(self):
        decision = evaluate_task(candidate_input(), now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
        self.assertFalse(decision.mutation_allowed)
        self.assertFalse(decision.selected)

    def test_cooldown_waits_with_bounded_retry(self):
        item = candidate_input()
        item.last_recovery_at = NOW - 10
        decision = evaluate_task(
            item,
            now=NOW,
            policy=OrchestrationPolicy(recovery_cooldown_s=60, max_backoff_s=20),
        )
        self.assertEqual(decision.kind, OrchestrationDecisionKind.WAIT_COOLDOWN)
        self.assertEqual(decision.retry_after_s, 20)
        self.assertIn("cooldown", decision.reason)

    def test_budget_exhaustion_requires_human(self):
        item = candidate_input()
        item.task.recovery_attempts = item.task.max_recovery_attempts
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.BLOCKED_HUMAN)
        self.assertFalse(decision.checks["recovery_budget_available"])

    def test_stale_workspace_evidence_requires_reconciliation(self):
        item = candidate_input()
        item.workspace = replace(item.workspace, observed_at=NOW - 100)
        decision = evaluate_task(
            item,
            now=NOW,
            policy=OrchestrationPolicy(max_workspace_observation_age_s=30),
        )
        self.assertEqual(decision.kind, OrchestrationDecisionKind.RECONCILE)
        self.assertFalse(decision.checks["workspace_fresh"])

    def test_unresolved_action_locks_out_any_new_recovery(self):
        item = candidate_input()
        item.unresolved_action = unresolved_attempt(item)
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.BLOCKED_HUMAN)
        self.assertFalse(decision.checks["no_unresolved_action_attempt"])
        self.assertIn("never replay", decision.reason)

    def test_active_tracked_job_is_observed_not_recovered(self):
        item = candidate_input()
        item.lsm = replace(item.lsm, active_jobs=1)
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.OBSERVE)
        self.assertFalse(decision.checks["lsm_no_active_jobs"])

    def test_inflight_call_is_observed_not_recovered(self):
        item = candidate_input()
        item.lsm = replace(item.lsm, in_flight_calls=1)
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.OBSERVE)
        self.assertFalse(decision.checks["lsm_no_inflight_calls"])

    def test_continuation_pending_is_observed_not_competed_with(self):
        item = candidate_input()
        item.lsm = replace(item.lsm, continuation_pending=True)
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.OBSERVE)
        self.assertFalse(decision.checks["lsm_no_continuation_pending"])

    def test_two_sample_semantic_change_forces_more_reconciliation(self):
        item = candidate_input()
        changed_workspace = replace(item.workspace, git_head="def")
        item.current_reconciliation = build_reconciliation_record(
            item.task,
            item.assessment,
            worker=item.worker,
            browser=None,
            network=None,
            lsm=replace(item.lsm, observed_at=996.0),
            workspace=changed_workspace,
            created_at=980.0,
        )
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.RECONCILE)
        self.assertFalse(decision.checks["reconciliation_fence_stable"])

    def test_stale_exact_window_binding_forces_reconciliation(self):
        item = candidate_input()
        item.window_binding = make_binding(item.task, expires_at=NOW)
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(decision.kind, OrchestrationDecisionKind.RECONCILE)
        self.assertFalse(decision.checks["window_binding_fresh"])

    def test_page_continuity_requires_matching_durable_provenance(self):
        item = candidate_input()
        item.page_continuity_relevant = True
        missing = evaluate_task(item, now=NOW)
        self.assertEqual(missing.kind, OrchestrationDecisionKind.BLOCKED_HUMAN)
        self.assertFalse(missing.checks["page_capability_present"])

        item.page_capability = page_capability()
        item.capability_context = runtime_context(browser_major=151)
        allowed = evaluate_task(item, now=NOW)
        self.assertEqual(allowed.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
        self.assertTrue(allowed.checks["page_capability_matches_runtime"])
        self.assertFalse(allowed.mutation_allowed)

    def test_fairness_selects_oldest_service_across_competing_states(self):
        recovering = candidate_input(
            "recovering",
            SupervisorState.RECOVERING,
            last_scheduled_at=950.0,
        )
        reconciling = candidate_input(
            "reconciling",
            SupervisorState.RECONCILING,
            last_scheduled_at=900.0,
        )
        suspect = candidate_input(
            "suspect",
            SupervisorState.SUSPECT,
            last_scheduled_at=975.0,
        )
        decisions = plan_orchestration(
            [recovering, reconciling, suspect],
            now=NOW,
            policy=OrchestrationPolicy(max_selected_tasks=1, base_backoff_s=5),
        )
        by_id = {decision.task_id: decision for decision in decisions}
        self.assertTrue(by_id["reconciling"].selected)
        self.assertEqual(
            by_id["reconciling"].kind,
            OrchestrationDecisionKind.RECOMMEND_ACTION,
        )
        self.assertEqual(
            by_id["recovering"].kind,
            OrchestrationDecisionKind.WAIT_COOLDOWN,
        )
        self.assertEqual(
            by_id["recovering"].deferred_kind,
            OrchestrationDecisionKind.RECOMMEND_ACTION,
        )
        self.assertEqual(by_id["recovering"].retry_after_s, 5)
        self.assertEqual(by_id["suspect"].kind, OrchestrationDecisionKind.WAIT_COOLDOWN)

    def test_fairness_rotates_when_integrator_updates_last_service_time(self):
        first = candidate_input("first", last_scheduled_at=900.0)
        second = candidate_input("second", last_scheduled_at=950.0)
        third = candidate_input("third", last_scheduled_at=975.0)
        initial = plan_orchestration([first, second, third], now=NOW)
        self.assertEqual([d.task_id for d in initial if d.selected], ["first"])

        first.last_scheduled_at = NOW
        next_cycle = plan_orchestration([first, second, third], now=NOW + 1)
        self.assertEqual([d.task_id for d in next_cycle if d.selected], ["second"])

    def test_backoff_is_bounded_for_deferred_competing_tasks(self):
        older = candidate_input("older", last_scheduled_at=900.0)
        deferred = candidate_input(
            "deferred",
            last_scheduled_at=950.0,
            consecutive_waits=50,
        )
        decisions = plan_orchestration(
            [older, deferred],
            now=NOW,
            policy=OrchestrationPolicy(
                max_selected_tasks=1,
                base_backoff_s=5,
                max_backoff_s=40,
            ),
        )
        by_id = {decision.task_id: decision for decision in decisions}
        self.assertEqual(by_id["deferred"].retry_after_s, 40)


if __name__ == "__main__":
    unittest.main()
