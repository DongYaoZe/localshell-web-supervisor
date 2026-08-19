import sqlite3
import tempfile
import unittest
from pathlib import Path

from lws.db import SCHEMA_VERSION
from lws.models import ProbeMutationState, ProbeWindowSlotBinding, WorkerStatus
from lws.page_runtime import (
    plan_probe_slot,
    probe_close_operation,
    probe_operation_from_plan,
    tagged_probe_url,
)
from lws.probe_ops import (
    ProbeMutationObservation,
    ProbeReconcileOutcome,
    ProbeWindowMatch,
    decide_probe_reconciliation,
)
from lws.registry import Registry

URL1 = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL2 = "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SOURCE = "windows_uia_lws_probe"


def exact_match(slot: ProbeWindowSlotBinding) -> ProbeWindowMatch:
    return ProbeWindowMatch(
        window_handle=slot.window_handle,
        browser_pid=slot.browser_pid,
        chrome_executable=slot.chrome_executable,
        actual_url=slot.actual_url,
    )


def expected_new_match(operation, *, hwnd=999, pid=888) -> ProbeWindowMatch:
    return ProbeWindowMatch(
        window_handle=hwnd,
        browser_pid=pid,
        chrome_executable=operation.expected_chrome_executable,
        actual_url=operation.expected_actual_url,
    )


class ProbeMutationRegistryTests(unittest.TestCase):
    def _registry(self, td: str) -> Registry:
        return Registry(Path(td) / "r.sqlite3")

    def _register_parked(self, registry: Registry, *, task_id: str, url: str):
        task = registry.register_task(
            task_id=task_id,
            project="lws",
            objective="probe test",
            cwd=".",
            conversation_url=url,
        )
        worker = registry.get_worker(task.current_worker_id)
        registry.set_worker_status(worker.worker_id, WorkerStatus.PARKED)
        return registry.get_worker(worker.worker_id)

    def _bind_old_slot(self, registry: Registry, worker) -> ProbeWindowSlotBinding:
        actual = tagged_probe_url(
            worker.conversation_url,
            slot_id="probe:default",
            owner_token="old-owner",
        )
        return registry.bind_probe_window_slot(
            "probe:default",
            owner_token="old-owner",
            target_worker_id=worker.worker_id,
            target_conversation_url=worker.conversation_url,
            actual_url=actual,
            window_handle=123,
            browser_pid=456,
            chrome_executable=CHROME,
            source=SOURCE,
            observed_at=100.0,
            ttl_s=1000.0,
        )

    def test_v4_migrates_additively_to_current_and_preserves_probe_slot(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy-v4.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA user_version = 4")
            conn.executescript(
                """
                CREATE TABLE tasks(
                    task_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lsm_session_id TEXT,
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    current_worker_id TEXT,
                    recovery_attempts INTEGER NOT NULL DEFAULT 0,
                    max_recovery_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE workers(
                    worker_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    conversation_url TEXT NOT NULL,
                    conversation_id TEXT,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    last_seen_at REAL,
                    ended_at REAL
                );
                CREATE TABLE probe_window_slots(
                    slot_id TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    target_worker_id TEXT NOT NULL,
                    target_conversation_url TEXT NOT NULL,
                    actual_url TEXT NOT NULL,
                    window_handle INTEGER NOT NULL,
                    browser_pid INTEGER NOT NULL,
                    chrome_executable TEXT NOT NULL,
                    source TEXT NOT NULL,
                    bound_at REAL NOT NULL,
                    observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                INSERT INTO tasks VALUES(
                    't','lws','o','.', 'QUEUED',NULL,'{}','w',0,3,1,1
                );
                INSERT INTO workers VALUES(
                    'w','t','https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                    NULL,'parked',1,NULL,NULL
                );
                INSERT INTO probe_window_slots VALUES(
                    'probe:default','owner','w',
                    'https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                    'https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee#lws-probe=probe:default:owner',
                    10,20,'chrome.exe','windows_uia_lws_probe',1,1,999
                );
                """
            )
            conn.commit()
            conn.close()

            registry = Registry(db)
            try:
                self.assertEqual(SCHEMA_VERSION, 9)
                self.assertEqual(
                    registry._conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
                )
                self.assertIsNotNone(
                    registry._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='probe_mutation_operations'"
                    ).fetchone()
                )
                self.assertEqual(
                    registry.get_probe_window_slot("probe:default").target_worker_id, "w"
                )
            finally:
                registry.close()

    def test_only_one_unresolved_probe_mutation_is_allowed_globally(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                first = self._register_parked(registry, task_id="t1", url=URL1)
                first_plan = plan_probe_slot(first, None, now=10.0)
                first_op = probe_operation_from_plan(
                    first,
                    first_plan,
                    None,
                    chrome_executable=CHROME,
                    now=10.0,
                    operation_id="op1",
                    nonce="nonce1",
                )
                registry.arm_probe_mutation_operation(first_op)

                second = self._register_parked(registry, task_id="t2", url=URL2)
                second_plan = plan_probe_slot(second, None, now=11.0)
                second_op = probe_operation_from_plan(
                    second,
                    second_plan,
                    None,
                    chrome_executable=CHROME,
                    now=11.0,
                    operation_id="op2",
                    nonce="nonce2",
                )
                with self.assertRaisesRegex(RuntimeError, "unresolved probe mutation"):
                    registry.arm_probe_mutation_operation(second_op)
                self.assertEqual(registry.unresolved_probe_mutation_operation().operation_id, "op1")
            finally:
                registry.close()

    def test_open_crash_after_launch_adopts_one_exact_owned_match_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                worker = self._register_parked(registry, task_id="t1", url=URL1)
                plan = plan_probe_slot(worker, None, now=10.0)
                operation = probe_operation_from_plan(
                    worker,
                    plan,
                    None,
                    chrome_executable=CHROME,
                    now=10.0,
                    operation_id="open-op",
                    nonce="open-nonce",
                )
                registry.arm_probe_mutation_operation(operation)

                # Crash before OPEN authority: absence is safe and remains resumable.
                reconciled = registry.reconcile_probe_mutation_operation(
                    "open-op", ProbeMutationObservation(observed_at=11.0)
                )
                self.assertEqual(reconciled.state, ProbeMutationState.ARMED)

                submitted = registry.authorize_probe_open("open-op", now=12.0)
                self.assertEqual(submitted.state, ProbeMutationState.OPEN_SUBMITTED)

                # Simulate browser launch succeeded, process died before durable slot update.
                match = expected_new_match(submitted)
                observation = ProbeMutationObservation(observed_at=13.0, new_matches=[match])
                completed = registry.reconcile_probe_mutation_operation("open-op", observation)
                self.assertEqual(completed.state, ProbeMutationState.COMPLETED)
                slot = registry.get_probe_window_slot("probe:default")
                self.assertEqual(slot.window_handle, match.window_handle)
                self.assertEqual(slot.target_worker_id, worker.worker_id)

                attempts = completed.reconcile_attempts
                again = registry.reconcile_probe_mutation_operation("open-op", observation)
                self.assertEqual(again.state, ProbeMutationState.COMPLETED)
                self.assertEqual(again.reconcile_attempts, attempts)
                self.assertEqual(registry.probe_window_slots(), [slot])
            finally:
                registry.close()

    def test_open_absent_after_submitted_blocks_replay(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                worker = self._register_parked(registry, task_id="t1", url=URL1)
                operation = probe_operation_from_plan(
                    worker,
                    plan_probe_slot(worker, None, now=10.0),
                    None,
                    chrome_executable=CHROME,
                    now=10.0,
                )
                operation = registry.arm_probe_mutation_operation(operation)
                registry.authorize_probe_open(operation.operation_id, now=11.0)
                blocked = registry.reconcile_probe_mutation_operation(
                    operation.operation_id,
                    ProbeMutationObservation(observed_at=12.0),
                )
                self.assertEqual(blocked.state, ProbeMutationState.RECONCILE_REQUIRED)
                self.assertEqual(blocked.resume_state, ProbeMutationState.OPEN_SUBMITTED.value)
                with self.assertRaisesRegex(RuntimeError, "not ready for OPEN"):
                    registry.authorize_probe_open(operation.operation_id, now=13.0)
                self.assertIsNone(registry.get_probe_window_slot("probe:default"))
            finally:
                registry.close()

    def test_delayed_pre_authority_observation_cannot_erase_open_authority(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                worker = self._register_parked(registry, task_id="t1", url=URL1)
                operation = probe_operation_from_plan(
                    worker,
                    plan_probe_slot(worker, None, now=10.0),
                    None,
                    chrome_executable=CHROME,
                    now=10.0,
                    operation_id="race-op",
                    nonce="race-nonce",
                )
                registry.arm_probe_mutation_operation(operation)
                stale_absent = ProbeMutationObservation(observed_at=10.5)
                registry.authorize_probe_open("race-op", now=11.0)

                reconciled = registry.reconcile_probe_mutation_operation(
                    "race-op", stale_absent
                )
                self.assertEqual(
                    reconciled.state, ProbeMutationState.RECONCILE_REQUIRED
                )
                self.assertEqual(
                    reconciled.resume_state, ProbeMutationState.OPEN_SUBMITTED.value
                )
                self.assertGreaterEqual(reconciled.updated_at, 11.0)
                with self.assertRaisesRegex(RuntimeError, "not ready for OPEN"):
                    registry.authorize_probe_open("race-op", now=12.0)
            finally:
                registry.close()

    def test_rotate_crash_after_close_before_open_resumes_without_inventing_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                old_worker = self._register_parked(registry, task_id="t1", url=URL1)
                old_slot = self._bind_old_slot(registry, old_worker)
                new_worker = self._register_parked(registry, task_id="t2", url=URL2)
                plan = plan_probe_slot(new_worker, old_slot, now=150.0)
                operation = probe_operation_from_plan(
                    new_worker,
                    plan,
                    old_slot,
                    chrome_executable=CHROME,
                    now=150.0,
                    operation_id="rotate-op",
                    nonce="rotate-nonce",
                )
                registry.arm_probe_mutation_operation(operation)

                with self.assertRaisesRegex(RuntimeError, "not ready for OPEN"):
                    registry.authorize_probe_open("rotate-op", now=151.0)
                registry.authorize_probe_close("rotate-op", now=152.0)
                with self.assertRaisesRegex(RuntimeError, "not ready for OPEN"):
                    registry.authorize_probe_open("rotate-op", now=153.0)

                # Exact old close happened, then process died before recording READY_TO_OPEN.
                ready = registry.reconcile_probe_mutation_operation(
                    "rotate-op", ProbeMutationObservation(observed_at=154.0)
                )
                self.assertEqual(ready.state, ProbeMutationState.READY_TO_OPEN)
                durable_old = registry.get_probe_window_slot("probe:default")
                self.assertEqual(durable_old.target_worker_id, old_worker.worker_id)

                submitted = registry.authorize_probe_open("rotate-op", now=155.0)
                self.assertEqual(submitted.state, ProbeMutationState.OPEN_SUBMITTED)
                new_match = expected_new_match(submitted, hwnd=321, pid=654)
                completed = registry.reconcile_probe_mutation_operation(
                    "rotate-op",
                    ProbeMutationObservation(observed_at=156.0, new_matches=[new_match]),
                )
                self.assertEqual(completed.state, ProbeMutationState.COMPLETED)
                replacement = registry.get_probe_window_slot("probe:default")
                self.assertEqual(replacement.target_worker_id, new_worker.worker_id)
                self.assertEqual(replacement.window_handle, 321)
            finally:
                registry.close()

    def test_close_crash_after_exact_close_clears_slot_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                worker = self._register_parked(registry, task_id="t1", url=URL1)
                old_slot = self._bind_old_slot(registry, worker)
                operation = probe_close_operation(
                    worker,
                    old_slot,
                    now=20.0,
                    operation_id="close-op",
                    nonce="close-nonce",
                )
                registry.arm_probe_mutation_operation(operation)
                registry.authorize_probe_close("close-op", now=21.0)
                completed = registry.reconcile_probe_mutation_operation(
                    "close-op", ProbeMutationObservation(observed_at=22.0)
                )
                self.assertEqual(completed.state, ProbeMutationState.COMPLETED)
                self.assertIsNone(registry.get_probe_window_slot("probe:default"))
                self.assertIsNone(registry.unresolved_probe_mutation_operation())
            finally:
                registry.close()

    def test_close_old_still_present_after_submitted_blocks_replay(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                worker = self._register_parked(registry, task_id="t1", url=URL1)
                old_slot = self._bind_old_slot(registry, worker)
                operation = probe_close_operation(worker, old_slot, now=20.0)
                operation = registry.arm_probe_mutation_operation(operation)
                registry.authorize_probe_close(operation.operation_id, now=21.0)
                blocked = registry.reconcile_probe_mutation_operation(
                    operation.operation_id,
                    ProbeMutationObservation(
                        observed_at=22.0, old_matches=[exact_match(old_slot)]
                    ),
                )
                self.assertEqual(blocked.state, ProbeMutationState.RECONCILE_REQUIRED)
                with self.assertRaisesRegex(RuntimeError, "not ARMED"):
                    registry.authorize_probe_close(operation.operation_id, now=23.0)
            finally:
                registry.close()

    def test_unresolved_operation_fences_legacy_slot_bind_and_clear(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                old_worker = self._register_parked(registry, task_id="t1", url=URL1)
                old_slot = self._bind_old_slot(registry, old_worker)
                new_worker = self._register_parked(registry, task_id="t2", url=URL2)
                operation = probe_operation_from_plan(
                    new_worker,
                    plan_probe_slot(new_worker, old_slot, now=150.0),
                    old_slot,
                    chrome_executable=CHROME,
                    now=150.0,
                    operation_id="fence-op",
                    nonce="fence-nonce",
                )
                registry.arm_probe_mutation_operation(operation)

                with self.assertRaisesRegex(RuntimeError, "fenced by unresolved mutation"):
                    registry.clear_probe_window_slot("probe:default")
                with self.assertRaisesRegex(RuntimeError, "fenced by unresolved mutation"):
                    registry.bind_probe_window_slot(
                        "probe:default",
                        owner_token=old_slot.owner_token,
                        target_worker_id=old_worker.worker_id,
                        target_conversation_url=old_worker.conversation_url,
                        actual_url=old_slot.actual_url,
                        window_handle=old_slot.window_handle,
                        browser_pid=old_slot.browser_pid,
                        chrome_executable=old_slot.chrome_executable,
                        source=old_slot.source,
                    )
            finally:
                registry.close()

    def test_direct_completion_cannot_bypass_open_authority(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            try:
                worker = self._register_parked(registry, task_id="t1", url=URL1)
                operation = probe_operation_from_plan(
                    worker,
                    plan_probe_slot(worker, None, now=10.0),
                    None,
                    chrome_executable=CHROME,
                    now=10.0,
                    operation_id="complete-op",
                    nonce="complete-nonce",
                )
                operation = registry.arm_probe_mutation_operation(operation)
                match = expected_new_match(operation)
                binding = ProbeWindowSlotBinding(
                    operation.slot_id,
                    operation.owner_token,
                    operation.target_worker_id,
                    operation.target_conversation_url,
                    operation.expected_actual_url,
                    match.window_handle,
                    match.browser_pid,
                    match.chrome_executable,
                    operation.source,
                    11.0,
                    11.0,
                    131.0,
                )
                with self.assertRaisesRegex(RuntimeError, "durable OPEN authority"):
                    registry.complete_probe_mutation_operation(
                        operation.operation_id,
                        binding=binding,
                        now=11.0,
                    )
                self.assertEqual(
                    registry.get_probe_mutation_operation(operation.operation_id).state,
                    ProbeMutationState.ARMED,
                )
                self.assertIsNone(registry.get_probe_window_slot("probe:default"))
            finally:
                registry.close()


class ProbeReconciliationDecisionTests(unittest.TestCase):
    def _rotate_operation(self):
        old_actual = tagged_probe_url(
            URL1, slot_id="probe:default", owner_token="old-owner"
        )
        old = ProbeWindowSlotBinding(
            "probe:default",
            "old-owner",
            "w1",
            URL1,
            old_actual,
            123,
            456,
            CHROME,
            SOURCE,
            100.0,
            100.0,
            999.0,
        )
        from lws.models import WorkerRecord

        new_worker = WorkerRecord("w2", "t2", URL2, None, WorkerStatus.PARKED, 1.0)
        plan = plan_probe_slot(new_worker, old, now=150.0)
        operation = probe_operation_from_plan(
            new_worker,
            plan,
            old,
            chrome_executable=CHROME,
            now=150.0,
            operation_id="op",
            nonce="nonce",
        )
        return old, operation

    def test_all_required_reconciliation_outcomes_are_deterministic(self):
        old, operation = self._rotate_operation()
        operation.state = ProbeMutationState.OPEN_SUBMITTED
        old_match = exact_match(old)
        new_match = expected_new_match(operation)

        cases = [
            (
                "old remains",
                ProbeMutationObservation(observed_at=1.0, old_matches=[old_match]),
                ProbeReconcileOutcome.OLD_TARGET_STILL_PRESENT,
            ),
            (
                "old absent replacement absent",
                ProbeMutationObservation(observed_at=1.0),
                ProbeReconcileOutcome.EXACT_TARGET_ABSENT,
            ),
            (
                "unique replacement",
                ProbeMutationObservation(observed_at=1.0, new_matches=[new_match]),
                ProbeReconcileOutcome.EXACT_UNIQUE_OWNED_TARGET_PRESENT,
            ),
            (
                "both old and new",
                ProbeMutationObservation(
                    observed_at=1.0, old_matches=[old_match], new_matches=[new_match]
                ),
                ProbeReconcileOutcome.BOTH_OLD_AND_NEW_PRESENT,
            ),
            (
                "multiple",
                ProbeMutationObservation(
                    observed_at=1.0, old_matches=[old_match, old_match]
                ),
                ProbeReconcileOutcome.MULTIPLE_MATCHES,
            ),
            (
                "stale identity",
                ProbeMutationObservation(
                    observed_at=1.0,
                    old_matches=[
                        ProbeWindowMatch(
                            old.window_handle,
                            old.browser_pid + 1,
                            old.chrome_executable,
                            old.actual_url,
                        )
                    ],
                ),
                ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
            ),
            (
                "unknown",
                ProbeMutationObservation(
                    observed_at=1.0, complete=False, error="probe failed"
                ),
                ProbeReconcileOutcome.UNKNOWN_OBSERVATION,
            ),
        ]

        for name, observation, expected in cases:
            with self.subTest(name=name):
                decision = decide_probe_reconciliation(operation, observation)
                self.assertEqual(decision.outcome, expected)
                if expected in {
                    ProbeReconcileOutcome.BOTH_OLD_AND_NEW_PRESENT,
                    ProbeReconcileOutcome.MULTIPLE_MATCHES,
                    ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
                    ProbeReconcileOutcome.UNKNOWN_OBSERVATION,
                }:
                    self.assertEqual(
                        decision.next_state, ProbeMutationState.RECONCILE_REQUIRED
                    )

    def test_exact_replacement_before_open_authority_is_inconsistent(self):
        _, rotate = self._rotate_operation()
        rotate_decision = decide_probe_reconciliation(
            rotate,
            ProbeMutationObservation(
                observed_at=2.0,
                new_matches=[expected_new_match(rotate)],
            ),
        )
        self.assertEqual(
            rotate_decision.outcome,
            ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
        )
        self.assertEqual(
            rotate_decision.next_state, ProbeMutationState.RECONCILE_REQUIRED
        )

        from lws.models import WorkerRecord

        worker = WorkerRecord("w1", "t1", URL1, None, WorkerStatus.PARKED, 1.0)
        opened = probe_operation_from_plan(
            worker,
            plan_probe_slot(worker, None, now=1.0),
            None,
            chrome_executable=CHROME,
            now=1.0,
        )
        open_decision = decide_probe_reconciliation(
            opened,
            ProbeMutationObservation(
                observed_at=2.0,
                new_matches=[expected_new_match(opened)],
            ),
        )
        self.assertEqual(
            open_decision.outcome,
            ProbeReconcileOutcome.STALE_OR_CHANGED_IDENTITY,
        )
        self.assertEqual(open_decision.next_state, ProbeMutationState.RECONCILE_REQUIRED)

    def test_open_submitted_absence_and_rotate_open_submitted_absence_fail_closed(self):
        old, rotate = self._rotate_operation()
        rotate.state = ProbeMutationState.OPEN_SUBMITTED
        rotate_decision = decide_probe_reconciliation(
            rotate, ProbeMutationObservation(observed_at=2.0)
        )
        self.assertEqual(rotate_decision.outcome, ProbeReconcileOutcome.EXACT_TARGET_ABSENT)
        self.assertEqual(rotate_decision.next_state, ProbeMutationState.RECONCILE_REQUIRED)

        from lws.models import WorkerRecord

        worker = WorkerRecord("w1", "t1", URL1, None, WorkerStatus.PARKED, 1.0)
        plan = plan_probe_slot(worker, None, now=1.0)
        opened = probe_operation_from_plan(
            worker, plan, None, chrome_executable=CHROME, now=1.0
        )
        opened.state = ProbeMutationState.OPEN_SUBMITTED
        open_decision = decide_probe_reconciliation(
            opened, ProbeMutationObservation(observed_at=2.0)
        )
        self.assertEqual(open_decision.next_state, ProbeMutationState.RECONCILE_REQUIRED)


if __name__ == "__main__":
    unittest.main()
