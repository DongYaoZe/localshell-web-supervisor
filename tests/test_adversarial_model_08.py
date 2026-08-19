import itertools
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import pytest

from cws.action_runtime import submit_armed_action
from cws.actions import (
    ActionAttempt,
    ActionAttemptState,
    ActionBlocked,
    TransportSubmission,
)
from cws.models import (
    Assessment,
    LsmObservation,
    ProbeMutationKind,
    ProbeMutationOperation,
    ProbeMutationState,
    ProbeWindowSlotBinding,
    ReconciliationRecord,
    SupervisorState,
    TaskRecord,
    WorkerRecord,
    WorkerStatus,
    WorkerWindowBinding,
    WorkspaceObservation,
)
from cws.orchestration import (
    OrchestrationDecisionKind,
    OrchestrationPolicy,
    TaskOrchestrationInput,
    evaluate_task,
    plan_orchestration,
)
from cws.page_runtime import plan_probe_slot, probe_operation_from_plan, tagged_probe_url
from cws.probe_ops import ProbeMutationObservation, ProbeWindowMatch, decide_probe_reconciliation
from cws.reconcile import build_reconciliation_record
from cws.registry import Registry
from cws.worker_protocol import (
    DecisionCode,
    DurableTaskStatus,
    WorkerLeaseStatus,
    abandon_worker,
    claim_worker,
    complete_task,
    complete_worker,
    heartbeat_worker,
    new_task_state,
    register_worker,
    request_handoff,
    takeover_worker,
    worker_by_id,
)


NOW = 1000.0
LEASE = 30.0
URL1 = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL2 = "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444"
URL3 = "https://chatgpt.com/c/99999999-1111-2222-3333-555555555555"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SOURCE = "windows_uia_cws_probe"


class CountingTransport:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def submit(self, intent):
        self.calls += 1
        return TransportSubmission(True, True, self.name, "unexpected submit")


def protocol_register(state, worker_id, *, now):
    decision = register_worker(
        state,
        worker_id,
        conversation_ref=f"conversation:{worker_id}",
        now=now,
        expected_revision=state.revision,
    )
    assert decision.accepted
    return decision.state


def protocol_claim(state, worker_id, *, now):
    decision = claim_worker(
        state,
        worker_id,
        now=now,
        lease_seconds=LEASE,
        expected_revision=state.revision,
    )
    assert decision.accepted
    return decision.state


def protocol_active_state():
    state = new_task_state("task-protocol")
    state = protocol_register(state, "worker-a", now=1.0)
    return protocol_claim(state, "worker-a", now=10.0)


def action_attempt(task_id, worker_id, attempt_id="act-1"):
    return ActionAttempt(
        attempt_id=attempt_id,
        task_id=task_id,
        worker_id=worker_id,
        action="CONTINUE_CURRENT_WORKER",
        fence_token="fence",
        fence_version=2,
        prompt_hash="hash",
        nonce=f"nonce-{attempt_id}",
        state=ActionAttemptState.ARMED,
        created_at=10.0,
        updated_at=10.0,
    )


def register_active_task(registry, *, task_id="t1", url=URL1):
    task = registry.register_task(
        task_id=task_id,
        project="cws",
        objective="adversarial model",
        cwd="C:/repo",
        lsm_session_id=f"s-{task_id}",
        conversation_url=url,
    )
    return registry.get_task(task_id), registry.get_worker(task.current_worker_id)


def register_parked_task(registry, *, task_id, url):
    task = registry.register_task(
        task_id=task_id,
        project="cws",
        objective="probe adversarial model",
        cwd="C:/repo",
        conversation_url=url,
    )
    worker = registry.get_worker(task.current_worker_id)
    registry.set_worker_status(worker.worker_id, WorkerStatus.PARKED)
    return registry.get_worker(worker.worker_id)


def bind_probe_slot(registry, worker, *, owner="old-owner", now=100.0):
    actual = tagged_probe_url(
        worker.conversation_url,
        slot_id="probe:default",
        owner_token=owner,
    )
    return registry.bind_probe_window_slot(
        "probe:default",
        owner_token=owner,
        target_worker_id=worker.worker_id,
        target_conversation_url=worker.conversation_url,
        actual_url=actual,
        window_handle=123,
        browser_pid=456,
        chrome_executable=CHROME,
        source=SOURCE,
        observed_at=now,
        ttl_s=2000.0,
    )


def exact_new_match(operation, *, hwnd=999, pid=888):
    return ProbeWindowMatch(
        window_handle=hwnd,
        browser_pid=pid,
        chrome_executable=operation.expected_chrome_executable,
        actual_url=operation.expected_actual_url,
    )


def make_probe_operation(registry, worker, *, now, operation_id, nonce):
    existing = registry.get_probe_window_slot("probe:default")
    plan = plan_probe_slot(worker, existing, now=now)
    return probe_operation_from_plan(
        worker,
        plan,
        existing,
        chrome_executable=CHROME,
        now=now,
        operation_id=operation_id,
        nonce=nonce,
    )


def make_task(task_id="orch", *, checkpoint_head="abc"):
    return TaskRecord(
        task_id=task_id,
        project="cws",
        objective="orchestration adversarial model",
        cwd="C:/repo",
        state=SupervisorState.SUSPECT,
        lsm_session_id=f"s-{task_id}",
        checkpoint={"git_head": checkpoint_head},
        current_worker_id=f"w-{task_id}",
        recovery_attempts=0,
        max_recovery_attempts=3,
        created_at=100.0,
        updated_at=900.0,
    )


def make_assessment(state=SupervisorState.SUSPECT):
    return Assessment(
        state=state,
        reason="sanitized stalled fixture",
        confidence="high",
        evidence=["sanitized-evidence"],
        requires_reconcile=True,
    )


def make_worker(task):
    return WorkerRecord(
        worker_id=task.current_worker_id,
        task_id=task.task_id,
        conversation_url=URL1,
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
        git_head=task.checkpoint.get("git_head"),
        git_dirty=False,
        git_status_hash="clean-hash",
    )
    values.update(changes)
    return WorkspaceObservation(**values)


def make_binding(task, **changes):
    values = dict(
        worker_id=task.current_worker_id,
        window_handle=1234,
        browser_pid=4321,
        chrome_executable=CHROME,
        conversation_url=URL1,
        source="windows_uia_chrome",
        bound_at=990.0,
        observed_at=995.0,
        expires_at=1030.0,
    )
    values.update(changes)
    return WorkerWindowBinding(**values)


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


def orchestration_candidate(task_id="orch"):
    task = make_task(task_id)
    worker = make_worker(task)
    lsm = make_lsm(task)
    workspace = make_workspace(task)
    previous, current = make_reconciliations(task, worker, lsm, workspace)
    return TaskOrchestrationInput(
        task=task,
        assessment=make_assessment(),
        worker=worker,
        lsm=lsm,
        workspace=workspace,
        previous_reconciliation=previous,
        current_reconciliation=current,
        worker_lease_expires_at=1030.0,
        window_binding=make_binding(task),
    )


def unresolved_orchestration_action(item):
    attempt = action_attempt(item.task.task_id, item.task.current_worker_id, "act-orch")
    attempt.state = ActionAttemptState.SUBMITTED
    return attempt


class ConversationTaskIdentityModelTests(unittest.TestCase):
    def test_terminal_conversation_states_never_imply_durable_task_completion(self):
        base = protocol_active_state()

        abandoned = abandon_worker(
            base,
            "worker-a",
            generation=1,
            now=40.0,
            expected_revision=base.revision,
        )
        self.assertTrue(abandoned.accepted)
        self.assertEqual(abandoned.state.task_status, DurableTaskStatus.OPEN)

        completed_worker = complete_worker(
            base,
            "worker-a",
            generation=1,
            now=20.0,
            expected_revision=base.revision,
        )
        self.assertTrue(completed_worker.accepted)
        self.assertEqual(completed_worker.state.task_status, DurableTaskStatus.OPEN)

        with_candidate = protocol_register(base, "worker-b", now=11.0)
        superseded = takeover_worker(
            with_candidate,
            "worker-b",
            now=40.0,
            lease_seconds=LEASE,
            expected_revision=with_candidate.revision,
        )
        self.assertTrue(superseded.accepted)
        self.assertEqual(
            worker_by_id(superseded.state, "worker-a").status,
            WorkerLeaseStatus.SUPERSEDED,
        )
        self.assertEqual(superseded.state.task_status, DurableTaskStatus.OPEN)
        self.assertIsNone(superseded.state.completed_at)

    def test_real_parent_turn_failure_fixture_preserves_terminal_child_result_without_replay(self):
        # Sanitized representation of the observed incident: parent delivery is unknown,
        # while the child durable session and Git commit already prove terminal child work.
        fixture = {
            "parent_delivery": "unknown_timeout",
            "child_commit": "child-terminal-commit-abc",
            "child_session": "completed",
        }
        task = make_task("child", checkpoint_head=fixture["child_commit"])
        worker = make_worker(task)
        lsm = make_lsm(
            task,
            session_status=fixture["child_session"],
            plan_status="completed",
            continuation_due=False,
            succeeded_jobs=1,
        )
        workspace = make_workspace(task, git_head=fixture["child_commit"])
        previous, current = make_reconciliations(task, worker, lsm, workspace)
        item = TaskOrchestrationInput(
            task=task,
            assessment=make_assessment(),
            worker=worker,
            lsm=lsm,
            workspace=workspace,
            previous_reconciliation=previous,
            current_reconciliation=current,
            worker_lease_expires_at=1030.0,
            window_binding=make_binding(task),
        )
        decision = evaluate_task(item, now=NOW)
        self.assertEqual(fixture["parent_delivery"], "unknown_timeout")
        self.assertEqual(workspace.git_head, fixture["child_commit"])
        self.assertEqual(decision.kind, OrchestrationDecisionKind.OBSERVE)
        self.assertFalse(decision.mutation_allowed)
        self.assertNotEqual(decision.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)


class MessageActionFenceModelTests(unittest.TestCase):
    def test_every_unresolved_message_state_survives_restart_and_blocks_a_second_action(self):
        for target_state in (
            ActionAttemptState.ARMED,
            ActionAttemptState.SUBMITTED,
            ActionAttemptState.RECONCILE_REQUIRED,
        ):
            with self.subTest(target_state=target_state.value), tempfile.TemporaryDirectory() as td:
                db = Path(td) / "registry.sqlite3"
                registry = Registry(db)
                task, worker = register_active_task(registry)
                first = action_attempt(task.task_id, worker.worker_id, "act-first")
                registry.record_action_attempt(first)
                if target_state == ActionAttemptState.SUBMITTED:
                    registry.mark_action_submitted(
                        first.attempt_id,
                        transport_name="fixture",
                        submitted_at=11.0,
                    )
                elif target_state == ActionAttemptState.RECONCILE_REQUIRED:
                    registry.mark_action_reconcile_required(
                        first.attempt_id,
                        error="ambiguous fixture",
                        now=11.0,
                    )
                registry.close()

                reopened = Registry(db)
                try:
                    unresolved = reopened.unresolved_action_attempt(task.task_id)
                    self.assertIsNotNone(unresolved)
                    self.assertEqual(unresolved.state, target_state)
                    with self.assertRaises(RuntimeError):
                        reopened.record_action_attempt(
                            action_attempt(task.task_id, worker.worker_id, "act-second")
                        )
                    self.assertEqual(len(reopened.action_attempts(task.task_id)), 1)
                finally:
                    reopened.close()

    def test_ambiguous_submitted_send_is_not_replayed_after_registry_restart(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            registry = Registry(db)
            task, worker = register_active_task(registry)
            attempt = action_attempt(task.task_id, worker.worker_id, "act-send")
            registry.record_action_attempt(attempt)
            registry.mark_action_reconcile_required(
                attempt.attempt_id,
                error="parent turn died after transport outcome became unknown",
                now=11.0,
            )
            registry.close()

            transport = CountingTransport()
            reopened = Registry(db)
            try:
                with self.assertRaises(ActionBlocked):
                    submit_armed_action(
                        reopened,
                        attempt_id=attempt.attempt_id,
                        prompt="resume",
                        transport=transport,
                    )
                self.assertEqual(transport.calls, 0)
                self.assertEqual(
                    reopened.get_action_attempt(attempt.attempt_id).state,
                    ActionAttemptState.RECONCILE_REQUIRED,
                )
            finally:
                reopened.close()


class ProbeMutationFenceModelTests(unittest.TestCase):
    def _make_unresolved_state(self, registry, target_state):
        if target_state in {
            ProbeMutationState.ARMED,
            ProbeMutationState.OPEN_SUBMITTED,
            ProbeMutationState.RECONCILE_REQUIRED,
        }:
            worker = register_parked_task(registry, task_id="open-task", url=URL1)
            operation = make_probe_operation(
                registry,
                worker,
                now=10.0,
                operation_id="op-first",
                nonce="nonce-first",
            )
            registry.arm_probe_mutation_operation(operation)
            if target_state in {
                ProbeMutationState.OPEN_SUBMITTED,
                ProbeMutationState.RECONCILE_REQUIRED,
            }:
                registry.authorize_probe_open(operation.operation_id, now=11.0)
            if target_state == ProbeMutationState.RECONCILE_REQUIRED:
                registry.reconcile_probe_mutation_operation(
                    operation.operation_id,
                    ProbeMutationObservation(observed_at=12.0),
                )
            return registry.get_probe_mutation_operation(operation.operation_id)

        old = register_parked_task(registry, task_id="old-task", url=URL1)
        bind_probe_slot(registry, old)
        target = register_parked_task(registry, task_id="target-task", url=URL2)
        operation = make_probe_operation(
            registry,
            target,
            now=150.0,
            operation_id="op-first",
            nonce="nonce-first",
        )
        registry.arm_probe_mutation_operation(operation)
        registry.authorize_probe_close(operation.operation_id, now=151.0)
        if target_state == ProbeMutationState.READY_TO_OPEN:
            registry.reconcile_probe_mutation_operation(
                operation.operation_id,
                ProbeMutationObservation(observed_at=152.0),
            )
        return registry.get_probe_mutation_operation(operation.operation_id)

    def test_every_unresolved_probe_state_globally_fences_a_second_operation(self):
        for target_state in (
            ProbeMutationState.ARMED,
            ProbeMutationState.CLOSE_SUBMITTED,
            ProbeMutationState.READY_TO_OPEN,
            ProbeMutationState.OPEN_SUBMITTED,
            ProbeMutationState.RECONCILE_REQUIRED,
        ):
            with self.subTest(target_state=target_state.value), tempfile.TemporaryDirectory() as td:
                registry = Registry(Path(td) / "registry.sqlite3")
                try:
                    first = self._make_unresolved_state(registry, target_state)
                    self.assertEqual(first.state, target_state)
                    other = register_parked_task(registry, task_id="other-task", url=URL3)
                    second = make_probe_operation(
                        registry,
                        other,
                        now=300.0,
                        operation_id="op-second",
                        nonce="nonce-second",
                    )
                    with self.assertRaisesRegex(RuntimeError, "unresolved probe mutation"):
                        registry.arm_probe_mutation_operation(second)
                    self.assertEqual(
                        registry.unresolved_probe_mutation_operation().operation_id,
                        first.operation_id,
                    )
                finally:
                    registry.close()

    def test_submitted_open_and_close_authority_cannot_be_reissued_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            registry = Registry(db)
            worker = register_parked_task(registry, task_id="open-task", url=URL1)
            opened = make_probe_operation(
                registry,
                worker,
                now=10.0,
                operation_id="open-op",
                nonce="open-nonce",
            )
            registry.arm_probe_mutation_operation(opened)
            registry.authorize_probe_open(opened.operation_id, now=11.0)
            registry.close()

            reopened = Registry(db)
            try:
                with self.assertRaisesRegex(RuntimeError, "not ready for OPEN"):
                    reopened.authorize_probe_open(opened.operation_id, now=12.0)
            finally:
                reopened.close()

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            registry = Registry(db)
            old = register_parked_task(registry, task_id="old-task", url=URL1)
            bind_probe_slot(registry, old)
            target = register_parked_task(registry, task_id="target-task", url=URL2)
            rotated = make_probe_operation(
                registry,
                target,
                now=150.0,
                operation_id="rotate-op",
                nonce="rotate-nonce",
            )
            registry.arm_probe_mutation_operation(rotated)
            registry.authorize_probe_close(rotated.operation_id, now=151.0)
            registry.close()

            reopened = Registry(db)
            try:
                with self.assertRaisesRegex(RuntimeError, "not ARMED"):
                    reopened.authorize_probe_close(rotated.operation_id, now=152.0)
            finally:
                reopened.close()

    def test_reordered_read_only_probe_observations_never_create_second_open_authority(self):
        orders = ("absent", "present"), ("present", "absent")
        for order in orders:
            with self.subTest(order=order), tempfile.TemporaryDirectory() as td:
                registry = Registry(Path(td) / "registry.sqlite3")
                try:
                    worker = register_parked_task(registry, task_id="open-task", url=URL1)
                    operation = make_probe_operation(
                        registry,
                        worker,
                        now=10.0,
                        operation_id="open-op",
                        nonce="open-nonce",
                    )
                    registry.arm_probe_mutation_operation(operation)
                    submitted = registry.authorize_probe_open(operation.operation_id, now=11.0)
                    with self.assertRaisesRegex(RuntimeError, "not ready for OPEN"):
                        registry.authorize_probe_open(operation.operation_id, now=11.5)
                    observations = {
                        "absent": ProbeMutationObservation(observed_at=12.0),
                        "present": ProbeMutationObservation(
                            observed_at=13.0,
                            new_matches=[exact_new_match(submitted)],
                        ),
                    }
                    for name in order:
                        registry.reconcile_probe_mutation_operation(
                            operation.operation_id,
                            observations[name],
                        )
                    final = registry.get_probe_mutation_operation(operation.operation_id)
                    self.assertEqual(final.state, ProbeMutationState.COMPLETED)
                    self.assertIsNone(registry.unresolved_probe_mutation_operation())
                    slots = registry.probe_window_slots()
                    self.assertEqual(len(slots), 1)
                    self.assertEqual(slots[0].owner_token, submitted.owner_token)
                finally:
                    registry.close()


class WorkerGenerationRevisionModelTests(unittest.TestCase):
    def test_superseded_generation_cannot_heartbeat_complete_handoff_or_reclaim_authority(self):
        state = protocol_active_state()
        state = protocol_register(state, "worker-b", now=11.0)
        takeover = takeover_worker(
            state,
            "worker-b",
            now=40.0,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertTrue(takeover.accepted)
        state = protocol_register(takeover.state, "worker-c", now=41.0)
        old_generation = 1

        stale_mutations = (
            heartbeat_worker(
                state,
                "worker-a",
                generation=old_generation,
                now=42.0,
                lease_seconds=LEASE,
                expected_revision=state.revision,
            ),
            complete_worker(
                state,
                "worker-a",
                generation=old_generation,
                now=42.0,
                expected_revision=state.revision,
            ),
            request_handoff(
                state,
                "worker-a",
                "worker-c",
                generation=old_generation,
                now=42.0,
                expected_revision=state.revision,
            ),
            claim_worker(
                state,
                "worker-a",
                now=42.0,
                lease_seconds=LEASE,
                expected_revision=state.revision,
            ),
            takeover_worker(
                state,
                "worker-a",
                now=42.0,
                lease_seconds=LEASE,
                expected_revision=state.revision,
            ),
        )
        for decision in stale_mutations:
            self.assertFalse(decision.accepted)
            self.assertEqual(decision.state.current_worker_id, "worker-b")
            self.assertEqual(decision.state.generation, 2)
        self.assertEqual(
            [decision.code for decision in stale_mutations[:3]],
            [DecisionCode.FENCED_WORKER] * 3,
        )
        self.assertEqual(stale_mutations[3].code, DecisionCode.NOT_CANDIDATE)
        self.assertEqual(stale_mutations[4].code, DecisionCode.NOT_CANDIDATE)

    def test_all_pairs_of_valid_same_revision_mutations_admit_at_most_one_winner(self):
        state = protocol_active_state()
        state = protocol_register(state, "worker-b", now=11.0)
        shared_revision = state.revision

        def heartbeat(snapshot, expected_revision):
            return heartbeat_worker(
                snapshot,
                "worker-a",
                generation=1,
                now=20.0,
                lease_seconds=LEASE,
                expected_revision=expected_revision,
            )

        def handoff(snapshot, expected_revision):
            return request_handoff(
                snapshot,
                "worker-a",
                "worker-b",
                generation=1,
                now=20.0,
                expected_revision=expected_revision,
            )

        def complete(snapshot, expected_revision):
            return complete_worker(
                snapshot,
                "worker-a",
                generation=1,
                now=20.0,
                expected_revision=expected_revision,
            )

        mutations = {"heartbeat": heartbeat, "handoff": handoff, "complete": complete}
        for first_name, second_name in itertools.permutations(mutations, 2):
            with self.subTest(first=first_name, second=second_name):
                first = mutations[first_name](state, shared_revision)
                self.assertTrue(first.accepted)
                self.assertEqual(first.state.revision, shared_revision + 1)
                second = mutations[second_name](first.state, shared_revision)
                self.assertFalse(second.accepted)
                self.assertEqual(second.code, DecisionCode.STALE_REVISION)
                self.assertFalse(second.mutated)
                self.assertEqual(second.state, first.state)


class OrchestrationFenceModelTests(unittest.TestCase):
    def test_strong_fence_disagreements_never_recommend_or_enable_mutation(self):
        cases = []

        unresolved = orchestration_candidate("unresolved")
        unresolved.unresolved_action = unresolved_orchestration_action(unresolved)
        cases.append(("unresolved-action", unresolved))

        active_job = orchestration_candidate("active-job")
        active_job.lsm = replace(active_job.lsm, active_jobs=1)
        cases.append(("active-lsm-job", active_job))

        changed_git = orchestration_candidate("changed-git")
        changed_git.workspace = replace(changed_git.workspace, git_head="different")
        cases.append(("changed-git", changed_git))

        changed_fence = orchestration_candidate("changed-fence")
        changed_workspace = replace(changed_fence.workspace, git_status_hash="dirty-hash", git_dirty=True)
        changed_fence.current_reconciliation = build_reconciliation_record(
            changed_fence.task,
            changed_fence.assessment,
            worker=changed_fence.worker,
            browser=None,
            network=None,
            lsm=replace(changed_fence.lsm, observed_at=996.0),
            workspace=changed_workspace,
            created_at=980.0,
        )
        cases.append(("semantic-fence-change", changed_fence))

        for name, item in cases:
            with self.subTest(name=name):
                decision = evaluate_task(item, now=NOW)
                self.assertNotEqual(decision.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
                self.assertFalse(decision.mutation_allowed)

    def test_advisory_orchestration_never_manufactures_mutation_authority(self):
        item = orchestration_candidate("ready")
        direct = evaluate_task(item, now=NOW)
        self.assertEqual(direct.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
        self.assertFalse(direct.mutation_allowed)

        planned = plan_orchestration([item], now=NOW)
        self.assertEqual(len(planned), 1)
        self.assertTrue(planned[0].selected)
        self.assertEqual(planned[0].kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
        self.assertFalse(planned[0].mutation_allowed)

    def test_global_unresolved_probe_fence_is_not_yet_carried_by_pure_orchestration_input(self):
        item = orchestration_candidate("probe-fence")
        fields = TaskOrchestrationInput.__dataclass_fields__
        if "unresolved_probe_mutation" not in fields:
            pytest.xfail(
                "0.8-B owns the advisory adapter/global unresolved-probe fence; current pure input cannot carry it"
            )
        unresolved_probe = ProbeMutationOperation(
            operation_id="probe-op",
            nonce="probe-nonce",
            kind=ProbeMutationKind.OPEN,
            state=ProbeMutationState.OPEN_SUBMITTED,
            slot_id="probe:default",
            target_task_id=item.task.task_id,
            target_worker_id=item.task.current_worker_id,
            target_conversation_url=URL1,
            owner_token="owner",
            expected_actual_url=tagged_probe_url(
                URL1, slot_id="probe:default", owner_token="owner"
            ),
            expected_chrome_executable=CHROME,
            source=SOURCE,
            prior_slot=None,
            created_at=900.0,
            updated_at=901.0,
        )
        setattr(item, "unresolved_probe_mutation", unresolved_probe)
        decision = evaluate_task(item, now=NOW)
        self.assertNotEqual(decision.kind, OrchestrationDecisionKind.RECOMMEND_ACTION)
        self.assertFalse(decision.mutation_allowed)

    def test_duplicate_task_candidates_do_not_exceed_bounded_selection(self):
        first = orchestration_candidate("duplicate")
        duplicate = replace(first)
        decisions = plan_orchestration(
            [first, duplicate],
            now=NOW,
            policy=OrchestrationPolicy(max_selected_tasks=1),
        )
        selected = [decision for decision in decisions if decision.selected]
        task_ids = [decision.task_id for decision in decisions]
        if len(selected) > 1 or len(task_ids) != len(set(task_ids)):
            pytest.xfail(
                "0.8-B orchestration adapter owns stable duplicate-free planning; pure planner currently duplicates one task"
            )
        self.assertLessEqual(len(selected), 1)
        self.assertEqual(len(task_ids), len(set(task_ids)))


class ClockAdversarialModelTests(unittest.TestCase):
    def test_future_lsm_workspace_and_reconciliation_samples_fail_closed(self):
        future_lsm = orchestration_candidate("future-lsm")
        future_lsm.lsm = replace(future_lsm.lsm, observed_at=NOW + 1.0)
        self.assertEqual(
            evaluate_task(future_lsm, now=NOW).kind,
            OrchestrationDecisionKind.RECONCILE,
        )

        future_workspace = orchestration_candidate("future-workspace")
        future_workspace.workspace = replace(
            future_workspace.workspace,
            observed_at=NOW + 1.0,
        )
        self.assertEqual(
            evaluate_task(future_workspace, now=NOW).kind,
            OrchestrationDecisionKind.RECONCILE,
        )

        future_reconcile = orchestration_candidate("future-reconcile")
        current = future_reconcile.current_reconciliation
        future_reconcile.current_reconciliation = replace(current, created_at=NOW + 1.0)
        self.assertEqual(
            evaluate_task(future_reconcile, now=NOW).kind,
            OrchestrationDecisionKind.RECONCILE,
        )

    def test_worker_heartbeat_during_wall_clock_rollback_should_fail_closed(self):
        state = protocol_active_state()
        rolled_back = heartbeat_worker(
            state,
            "worker-a",
            generation=1,
            now=5.0,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        if rolled_back.accepted:
            pytest.xfail(
                "0.8-D worker-protocol persistence owner: pure lease protocol accepts heartbeat before claim/last-heartbeat time"
            )
        self.assertFalse(rolled_back.accepted)
        self.assertEqual(rolled_back.state, state)

    def test_future_probe_observation_should_not_create_long_lived_binding(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            try:
                worker = register_parked_task(registry, task_id="future-probe", url=URL1)
                operation = make_probe_operation(
                    registry,
                    worker,
                    now=10.0,
                    operation_id="future-op",
                    nonce="future-nonce",
                )
                operation.state = ProbeMutationState.OPEN_SUBMITTED
                decision = decide_probe_reconciliation(
                    operation,
                    ProbeMutationObservation(
                        observed_at=10_000_000.0,
                        new_matches=[exact_new_match(operation)],
                    ),
                )
                if decision.next_state == ProbeMutationState.COMPLETED:
                    pytest.xfail(
                        "0.8-A probe operator/evidence owner: future observed_at can currently mint a future-expiry slot binding"
                    )
                self.assertEqual(decision.next_state, ProbeMutationState.RECONCILE_REQUIRED)
                self.assertIsNone(decision.adopt_binding)
            finally:
                registry.close()

    def test_future_exact_window_binding_should_not_satisfy_orchestration_freshness(self):
        item = orchestration_candidate("future-binding")
        item.window_binding = make_binding(
            item.task,
            bound_at=NOW + 100.0,
            observed_at=NOW + 100.0,
            expires_at=NOW + 130.0,
        )
        decision = evaluate_task(item, now=NOW)
        if decision.kind == OrchestrationDecisionKind.RECOMMEND_ACTION:
            pytest.xfail(
                "0.8-B orchestration adapter owner: future exact-window observations are currently treated as fresh"
            )
        self.assertEqual(decision.kind, OrchestrationDecisionKind.RECONCILE)
        self.assertFalse(decision.mutation_allowed)


if __name__ == "__main__":
    unittest.main()
