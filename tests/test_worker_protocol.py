import unittest
from dataclasses import replace

from cws.worker_protocol import (
    DecisionCode,
    DurableTaskStatus,
    EventKind,
    ProtocolInvariantError,
    TakeoverReason,
    WorkerLeaseStatus,
    abandon_worker,
    claim_worker,
    complete_task,
    complete_worker,
    heartbeat_worker,
    lease_is_fresh,
    new_task_state,
    register_worker,
    request_handoff,
    takeover_eligibility,
    takeover_worker,
    validate_state,
    worker_by_id,
)


LEASE = 30.0


class WorkerProtocolTests(unittest.TestCase):
    def register(self, state, worker_id, *, now=0.0, conversation_ref=None):
        decision = register_worker(
            state,
            worker_id,
            conversation_ref=conversation_ref,
            now=now,
            expected_revision=state.revision,
        )
        self.assertTrue(decision.accepted)
        return decision.state

    def claim(self, state, worker_id, *, now=1.0):
        decision = claim_worker(
            state,
            worker_id,
            now=now,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertTrue(decision.accepted)
        return decision.state

    def active_state(self):
        state = new_task_state("task-1")
        state = self.register(state, "worker-a", conversation_ref="conversation:a")
        return self.claim(state, "worker-a", now=10.0)

    def test_root_and_child_lineage_are_durable_task_metadata(self):
        root = new_task_state("root")
        child = new_task_state(
            "child",
            parent_task_id="root",
            root_task_id=root.lineage.root_task_id,
            child_key="research-1",
        )
        grandchild = new_task_state(
            "grandchild",
            parent_task_id="child",
            root_task_id=child.lineage.root_task_id,
            child_key="test-1",
        )
        self.assertEqual(root.lineage.root_task_id, "root")
        self.assertEqual(child.lineage.parent_task_id, "root")
        self.assertEqual(grandchild.lineage.root_task_id, "root")
        self.assertEqual(grandchild.lineage.child_key, "test-1")
        with self.assertRaises(ValueError):
            new_task_state("orphan-child", parent_task_id="missing-root-id")

    def test_registration_is_candidate_only_and_is_idempotent(self):
        state = new_task_state("task-1")
        decision = register_worker(
            state,
            "worker-a",
            conversation_ref="conversation:a",
            now=1,
            expected_revision=0,
        )
        self.assertEqual(decision.code, DecisionCode.REGISTERED)
        self.assertEqual(decision.state.current_worker_id, None)
        self.assertEqual(
            worker_by_id(decision.state, "worker-a").status,
            WorkerLeaseStatus.CANDIDATE,
        )
        self.assertEqual(worker_by_id(decision.state, "worker-a").task_id, "task-1")
        duplicate = register_worker(
            decision.state,
            "worker-a",
            conversation_ref="conversation:other",
            now=2,
            expected_revision=decision.state.revision,
        )
        self.assertTrue(duplicate.accepted)
        self.assertEqual(duplicate.code, DecisionCode.ALREADY_REGISTERED)
        self.assertFalse(duplicate.mutated)
        self.assertEqual(
            worker_by_id(duplicate.state, "worker-a").conversation_ref,
            "conversation:a",
        )

    def test_claim_assigns_first_generation_and_heartbeat_renews_fresh_lease(self):
        state = self.active_state()
        worker = worker_by_id(state, "worker-a")
        self.assertEqual(state.generation, 1)
        self.assertEqual(worker.generation, 1)
        self.assertTrue(lease_is_fresh(state, "worker-a", now=39.9))
        heartbeat = heartbeat_worker(
            state,
            "worker-a",
            generation=1,
            now=20,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertTrue(heartbeat.accepted)
        self.assertEqual(heartbeat.code, DecisionCode.HEARTBEAT_RECORDED)
        self.assertEqual(worker_by_id(heartbeat.state, "worker-a").lease_expires_at, 50)

    def test_expired_lease_cannot_be_revived_by_late_heartbeat(self):
        state = self.active_state()
        late = heartbeat_worker(
            state,
            "worker-a",
            generation=1,
            now=40,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertFalse(late.accepted)
        self.assertEqual(late.code, DecisionCode.LEASE_EXPIRED)
        self.assertEqual(late.state, state)
        self.assertFalse(lease_is_fresh(state, "worker-a", now=40))

    def test_fresh_active_worker_blocks_unrequested_takeover(self):
        state = self.active_state()
        state = self.register(state, "worker-b", now=11)
        eligibility = takeover_eligibility(state, "worker-b", now=20)
        self.assertFalse(eligibility.eligible)
        self.assertEqual(eligibility.reason, TakeoverReason.ACTIVE_LEASE_FRESH)
        decision = takeover_worker(
            state,
            "worker-b",
            now=20,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, DecisionCode.TAKEOVER_NOT_ELIGIBLE)

    def test_handoff_allows_takeover_before_old_lease_expires(self):
        state = self.active_state()
        state = self.register(state, "worker-b", now=11)
        handoff = request_handoff(
            state,
            "worker-a",
            "worker-b",
            generation=1,
            now=12,
            expected_revision=state.revision,
        )
        self.assertTrue(handoff.accepted)
        eligibility = takeover_eligibility(handoff.state, "worker-b", now=13)
        self.assertTrue(eligibility.eligible)
        self.assertEqual(eligibility.reason, TakeoverReason.HANDOFF)
        takeover = takeover_worker(
            handoff.state,
            "worker-b",
            now=13,
            lease_seconds=LEASE,
            expected_revision=handoff.state.revision,
        )
        self.assertTrue(takeover.accepted)
        self.assertEqual(takeover.state.generation, 2)
        self.assertEqual(takeover.state.current_worker_id, "worker-b")
        self.assertEqual(
            worker_by_id(takeover.state, "worker-a").status,
            WorkerLeaseStatus.SUPERSEDED,
        )
        self.assertEqual(worker_by_id(takeover.state, "worker-a").superseded_by, "worker-b")
        self.assertEqual(
            [event.kind for event in takeover.events],
            [EventKind.WORKER_SUPERSEDED, EventKind.LEASE_TAKEN_OVER],
        )
        self.assertEqual({event.revision for event in takeover.events}, {takeover.state.revision})

    def test_expired_active_lease_makes_candidate_takeover_eligible(self):
        state = self.active_state()
        state = self.register(state, "worker-b", now=11)
        eligibility = takeover_eligibility(state, "worker-b", now=40)
        self.assertTrue(eligibility.eligible)
        self.assertEqual(eligibility.reason, TakeoverReason.LEASE_EXPIRED)
        takeover = takeover_worker(
            state,
            "worker-b",
            now=40,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertTrue(takeover.accepted)
        self.assertEqual(takeover.state.generation, 2)
        self.assertEqual(takeover.events[-1].ref, TakeoverReason.LEASE_EXPIRED.value)

    def test_superseded_worker_is_fenced_from_late_heartbeat(self):
        state = self.active_state()
        state = self.register(state, "worker-b", now=11)
        takeover = takeover_worker(
            state,
            "worker-b",
            now=40,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertTrue(takeover.accepted)
        late = heartbeat_worker(
            takeover.state,
            "worker-a",
            generation=1,
            now=41,
            lease_seconds=LEASE,
            expected_revision=takeover.state.revision,
        )
        self.assertFalse(late.accepted)
        self.assertEqual(late.code, DecisionCode.FENCED_WORKER)
        self.assertEqual(late.state.current_worker_id, "worker-b")
        self.assertEqual(late.state.generation, 2)
        stale_completion = complete_worker(
            takeover.state,
            "worker-a",
            generation=1,
            now=42,
            expected_revision=takeover.state.revision,
        )
        self.assertFalse(stale_completion.accepted)
        self.assertEqual(stale_completion.code, DecisionCode.FENCED_WORKER)

    def test_current_worker_with_wrong_generation_is_fenced(self):
        state = self.active_state()
        wrong = heartbeat_worker(
            state,
            "worker-a",
            generation=0,
            now=20,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertFalse(wrong.accepted)
        self.assertEqual(wrong.code, DecisionCode.STALE_GENERATION)

    def test_abandoned_conversation_leaves_task_open_and_recoverable(self):
        state = self.active_state()
        abandoned = abandon_worker(
            state,
            "worker-a",
            generation=1,
            now=45,
            expected_revision=state.revision,
        )
        self.assertTrue(abandoned.accepted)
        self.assertEqual(abandoned.state.task_status, DurableTaskStatus.OPEN)
        self.assertIsNone(abandoned.state.current_worker_id)
        self.assertEqual(
            worker_by_id(abandoned.state, "worker-a").status,
            WorkerLeaseStatus.ABANDONED,
        )
        recovered = self.register(abandoned.state, "worker-b", now=46)
        recovered = self.claim(recovered, "worker-b", now=47)
        self.assertEqual(recovered.generation, 2)
        self.assertEqual(recovered.current_worker_id, "worker-b")

    def test_abandon_can_fence_an_expired_current_worker(self):
        state = self.active_state()
        decision = abandon_worker(
            state,
            "worker-a",
            generation=1,
            now=100,
            expected_revision=state.revision,
        )
        self.assertTrue(decision.accepted)
        self.assertIsNone(decision.state.current_worker_id)

    def test_worker_completion_does_not_complete_durable_task(self):
        state = self.active_state()
        completed = complete_worker(
            state,
            "worker-a",
            generation=1,
            now=20,
            expected_revision=state.revision,
        )
        self.assertTrue(completed.accepted)
        self.assertEqual(
            worker_by_id(completed.state, "worker-a").status,
            WorkerLeaseStatus.COMPLETED,
        )
        self.assertEqual(completed.state.task_status, DurableTaskStatus.OPEN)
        self.assertIsNone(completed.state.completed_at)
        replacement = self.register(completed.state, "worker-b", now=21)
        replacement = self.claim(replacement, "worker-b", now=22)
        self.assertEqual(replacement.generation, 2)

    def test_expired_worker_cannot_self_complete_or_request_handoff(self):
        state = self.active_state()
        state = self.register(state, "worker-b", now=11)
        completion = complete_worker(
            state,
            "worker-a",
            generation=1,
            now=40,
            expected_revision=state.revision,
        )
        self.assertFalse(completion.accepted)
        self.assertEqual(completion.code, DecisionCode.LEASE_EXPIRED)
        handoff = request_handoff(
            state,
            "worker-a",
            "worker-b",
            generation=1,
            now=40,
            expected_revision=state.revision,
        )
        self.assertFalse(handoff.accepted)
        self.assertEqual(handoff.code, DecisionCode.LEASE_EXPIRED)

    def test_durable_task_completion_is_explicit_and_irreversible(self):
        state = self.active_state()
        worker_done = complete_worker(
            state,
            "worker-a",
            generation=1,
            now=20,
            expected_revision=state.revision,
        )
        task_done = complete_task(
            worker_done.state,
            completion_ref="workspace:commit:abc",
            now=21,
            expected_revision=worker_done.state.revision,
        )
        self.assertTrue(task_done.accepted)
        self.assertEqual(task_done.state.task_status, DurableTaskStatus.COMPLETED)
        self.assertEqual(task_done.state.completion_ref, "workspace:commit:abc")
        rejected = register_worker(
            task_done.state,
            "worker-b",
            conversation_ref=None,
            now=22,
            expected_revision=task_done.state.revision,
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.code, DecisionCode.TASK_CLOSED)

    def test_task_completion_can_follow_abandonment_after_external_reconciliation(self):
        state = self.active_state()
        abandoned = abandon_worker(
            state,
            "worker-a",
            generation=1,
            now=40,
            expected_revision=state.revision,
        )
        task_done = complete_task(
            abandoned.state,
            completion_ref="reconcile:workspace-proves-done",
            now=41,
            expected_revision=abandoned.state.revision,
        )
        self.assertTrue(task_done.accepted)
        self.assertEqual(task_done.state.task_status, DurableTaskStatus.COMPLETED)

    def test_task_completion_requires_no_active_worker_and_evidence_reference(self):
        state = self.active_state()
        still_active = complete_task(
            state,
            completion_ref="proof",
            now=20,
            expected_revision=state.revision,
        )
        self.assertFalse(still_active.accepted)
        self.assertEqual(still_active.code, DecisionCode.ACTIVE_WORKER_MUST_END)
        abandoned = abandon_worker(
            state,
            "worker-a",
            generation=1,
            now=20,
            expected_revision=state.revision,
        )
        no_ref = complete_task(
            abandoned.state,
            completion_ref=" ",
            now=21,
            expected_revision=abandoned.state.revision,
        )
        self.assertFalse(no_ref.accepted)
        self.assertEqual(no_ref.code, DecisionCode.COMPLETION_REF_REQUIRED)

    def test_multiple_candidates_racing_to_claim_use_revision_cas(self):
        state = new_task_state("task-1")
        state = self.register(state, "worker-a", now=1)
        state = self.register(state, "worker-b", now=2)
        shared_revision = state.revision
        winner = claim_worker(
            state,
            "worker-a",
            now=3,
            lease_seconds=LEASE,
            expected_revision=shared_revision,
        )
        self.assertTrue(winner.accepted)
        loser = claim_worker(
            winner.state,
            "worker-b",
            now=3,
            lease_seconds=LEASE,
            expected_revision=shared_revision,
        )
        self.assertFalse(loser.accepted)
        self.assertFalse(loser.mutated)
        self.assertEqual(loser.code, DecisionCode.STALE_REVISION)
        self.assertEqual(loser.state.current_worker_id, "worker-a")

    def test_handoff_target_must_be_registered_candidate(self):
        state = self.active_state()
        missing = request_handoff(
            state,
            "worker-a",
            "worker-missing",
            generation=1,
            now=20,
            expected_revision=state.revision,
        )
        self.assertFalse(missing.accepted)
        self.assertEqual(missing.code, DecisionCode.INVALID_HANDOFF_TARGET)
        state = self.register(state, "worker-b", now=21)
        state = self.claim(
            abandon_worker(
                state,
                "worker-a",
                generation=1,
                now=22,
                expected_revision=state.revision,
            ).state,
            "worker-b",
            now=23,
        )
        not_candidate = request_handoff(
            state,
            "worker-b",
            "worker-a",
            generation=2,
            now=24,
            expected_revision=state.revision,
        )
        self.assertFalse(not_candidate.accepted)
        self.assertEqual(not_candidate.code, DecisionCode.INVALID_HANDOFF_TARGET)

    def test_takeover_without_active_worker_requires_normal_claim(self):
        state = new_task_state("task-1")
        state = self.register(state, "worker-a")
        eligibility = takeover_eligibility(state, "worker-a", now=1)
        self.assertFalse(eligibility.eligible)
        self.assertEqual(eligibility.reason, TakeoverReason.NO_ACTIVE_WORKER)
        takeover = takeover_worker(
            state,
            "worker-a",
            now=1,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertFalse(takeover.accepted)
        self.assertEqual(takeover.code, DecisionCode.NO_ACTIVE_WORKER)

    def test_takeover_events_share_one_atomic_revision(self):
        state = self.active_state()
        state = self.register(state, "worker-b", now=11)
        decision = takeover_worker(
            state,
            "worker-b",
            now=40,
            lease_seconds=LEASE,
            expected_revision=state.revision,
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(len(decision.events), 2)
        self.assertTrue(all(event.revision == decision.state.revision for event in decision.events))
        self.assertEqual(decision.expected_revision + 1, decision.state.revision)

    def test_duplicate_worker_id_cannot_resurrect_terminal_conversation(self):
        state = self.active_state()
        done = complete_worker(
            state,
            "worker-a",
            generation=1,
            now=20,
            expected_revision=state.revision,
        )
        duplicate = register_worker(
            done.state,
            "worker-a",
            conversation_ref="conversation:new",
            now=21,
            expected_revision=done.state.revision,
        )
        self.assertTrue(duplicate.accepted)
        self.assertEqual(duplicate.code, DecisionCode.ALREADY_REGISTERED)
        self.assertEqual(
            worker_by_id(duplicate.state, "worker-a").status,
            WorkerLeaseStatus.COMPLETED,
        )

    def test_corrupt_snapshot_fails_closed_via_invariant_error(self):
        state = self.active_state()
        corrupt = replace(state, current_worker_id=None)
        with self.assertRaises(ProtocolInvariantError):
            validate_state(corrupt)
        with self.assertRaises(ProtocolInvariantError):
            register_worker(
                corrupt,
                "worker-b",
                conversation_ref=None,
                now=20,
                expected_revision=corrupt.revision,
            )

    def test_non_positive_lease_is_rejected_as_programmer_error(self):
        state = new_task_state("task-1")
        state = self.register(state, "worker-a")
        with self.assertRaises(ValueError):
            claim_worker(
                state,
                "worker-a",
                now=1,
                lease_seconds=0,
                expected_revision=state.revision,
            )


if __name__ == "__main__":
    unittest.main()
