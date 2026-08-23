import tempfile
import unittest
from pathlib import Path

from lws.actions import ActionAttemptState, TransportSubmission
from lws.dispatch_runtime import (
    DispatchExecutionPolicy,
    execute_current_worker_recovery,
    reconcile_action_with_uia,
)
from lws.dispatcher import DispatchAction, DispatchDisabled, DispatchPlan
from lws.models import (
    BrowserObservation,
    ReconciliationRecord,
    RecoveryRecommendation,
    SupervisorState,
)
from lws.registry import Registry
from lws.timeout_recovery import TimeoutRecoveryPolicy, gate_timeout_dispatch_plan
from lws.uia_actions import UiaAckObservation


URL = "https://web.example/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROMPT = "Resume safely from the durable checkpoint."


class FakeTransport:
    name = "fake"

    def __init__(self, result=None):
        self.result = result or TransportSubmission(True, True, self.name, "submitted")
        self.intent = None

    def submit(self, intent):
        self.intent = intent
        return self.result


class FakeObserver:
    def __init__(self, chrome_executable, *, generating=False, nonce_occurrences=1):
        self.chrome_executable = chrome_executable
        self.generating = generating
        self.nonce_occurrences = nonce_occurrences
        self.expected_nonce = None

    def observe(
        self,
        *,
        worker_id,
        conversation_url,
        expected_nonce,
        expected_hwnd=None,
        expected_browser_pid=None,
    ):
        self.expected_nonce = expected_nonce
        return UiaAckObservation(
            worker_id=worker_id,
            observed_at=160.0,
            url=conversation_url,
            window_handle=expected_hwnd or 0,
            browser_pid=expected_browser_pid or 0,
            generating=self.generating,
            send_button_ready=None,
            composer_present=True,
            signed_in_likely=True,
            nonce_occurrences=self.nonce_occurrences,
            text_element_count=50,
            text_signature="post-signature",
        )


class DispatchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.tmp.name) / "r.sqlite3")
        self.task = self.registry.register_task(
            task_id="t1",
            project="p",
            objective="o",
            cwd=self.tmp.name,
            conversation_url=URL,
        )
        self.worker = self.registry.get_worker(self.task.current_worker_id)
        self.registry.record_browser_observation(
            BrowserObservation(
                worker_id=self.worker.worker_id,
                observed_at=140.0,
                url=URL,
                generating=False,
                send_button_ready=True,
                last_dom_change_at=130.0,
                message_signature="pre-signature",
            )
        )
        self.registry.bind_worker_window(
            self.worker.worker_id,
            window_handle=123,
            browser_pid=456,
            chrome_executable=CHROME,
            conversation_url=URL,
            observed_at=140.0,
            ttl_s=30.0,
        )
        self.reconciliation = ReconciliationRecord(
            reconcile_id="rec2",
            task_id="t1",
            created_at=140.0,
            state="SUSPECT",
            confidence="high",
            reason="fixture",
            requires_reconcile=True,
            current_worker_id=self.worker.worker_id,
            fence_token="fixture",
            fence_version=3,
        )
        self.registry.record_reconciliation(self.reconciliation)

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def plan(self):
        return DispatchPlan(
            task_id="t1",
            created_at=145.0,
            action=DispatchAction.CONTINUE_CURRENT_WORKER,
            candidate_ready=True,
            transport_enabled=True,
            would_dispatch=True,
            reason="ready",
            previous_reconcile_id="rec1",
            current_reconcile_id="rec2",
            fence_token="fixture",
            checks={"all": True},
        )

    def recommendation(self):
        return RecoveryRecommendation(
            task_id="t1",
            action="reconcile_then_continue",
            safe_to_dispatch=False,
            reason="fixture",
            prompt=PROMPT,
        )

    def test_explicit_execution_arms_budget_before_submit_and_adds_nonce_marker(self):
        transport = FakeTransport()
        result = execute_current_worker_recovery(
            self.registry,
            plan=self.plan(),
            recommendation=self.recommendation(),
            policy=DispatchExecutionPolicy(enabled=True, confirmed_task_id="t1"),
            transport_factory=lambda binding: transport,
            now=150.0,
        )
        self.assertTrue(result.submitted)
        self.assertEqual(result.state, ActionAttemptState.SUBMITTED.value)
        attempt = self.registry.get_action_attempt(result.attempt_id)
        self.assertEqual(self.registry.get_task("t1").recovery_attempts, 1)
        self.assertIn(PROMPT, transport.intent.prompt)
        self.assertIn(f"LWS-ACTION-{attempt.nonce}", transport.intent.prompt)
        self.assertNotIn(PROMPT, str(attempt.metadata))

    def test_nonbudgeted_current_worker_action_preserves_recovery_budget_and_metadata(self):
        self.registry._conn.execute(
            "UPDATE tasks SET recovery_attempts=max_recovery_attempts WHERE task_id='t1'"
        )
        self.registry._conn.commit()
        transport = FakeTransport()
        result = execute_current_worker_recovery(
            self.registry,
            plan=self.plan(),
            recommendation=self.recommendation(),
            policy=DispatchExecutionPolicy(
                enabled=True,
                confirmed_task_id="t1",
                consume_recovery_budget=False,
                attempt_metadata={"trigger_kind": "hard_overrun_25m20"},
            ),
            transport_factory=lambda binding: transport,
            now=150.0,
        )
        self.assertTrue(result.submitted)
        task = self.registry.get_task("t1")
        self.assertEqual(task.recovery_attempts, task.max_recovery_attempts)
        attempt = self.registry.get_action_attempt(result.attempt_id)
        self.assertEqual(attempt.metadata["trigger_kind"], "hard_overrun_25m20")

    def test_disabled_or_wrong_confirmation_never_arms(self):
        for policy in (
            DispatchExecutionPolicy(enabled=False, confirmed_task_id="t1"),
            DispatchExecutionPolicy(enabled=True, confirmed_task_id="other"),
        ):
            with self.assertRaises(DispatchDisabled):
                execute_current_worker_recovery(
                    self.registry,
                    plan=self.plan(),
                    recommendation=self.recommendation(),
                    policy=policy,
                    transport_factory=lambda binding: FakeTransport(),
                    now=150.0,
                )
            self.assertIsNone(self.registry.unresolved_action_attempt("t1"))
            self.assertEqual(self.registry.get_task("t1").recovery_attempts, 0)

    def test_newer_reconciliation_or_stale_window_blocks_before_arming(self):
        newer = ReconciliationRecord(
            reconcile_id="rec3",
            task_id="t1",
            created_at=149.0,
            state="SUSPECT",
            confidence="high",
            reason="newer",
            requires_reconcile=True,
            current_worker_id=self.worker.worker_id,
            fence_token="fixture",
        )
        self.registry.record_reconciliation(newer)
        with self.assertRaisesRegex(Exception, "newer reconciliation"):
            execute_current_worker_recovery(
                self.registry,
                plan=self.plan(),
                recommendation=self.recommendation(),
                policy=DispatchExecutionPolicy(enabled=True, confirmed_task_id="t1"),
                transport_factory=lambda binding: FakeTransport(),
                now=150.0,
            )
        self.assertIsNone(self.registry.unresolved_action_attempt("t1"))

    def test_atomic_arm_refuses_terminal_task_without_transport_submission(self):
        self.registry.update_state("t1", SupervisorState.COMPLETED)
        transport = FakeTransport()
        with self.assertRaisesRegex(Exception, "terminal task"):
            execute_current_worker_recovery(
                self.registry,
                plan=self.plan(),
                recommendation=self.recommendation(),
                policy=DispatchExecutionPolicy(enabled=True, confirmed_task_id="t1"),
                transport_factory=lambda binding: transport,
                now=150.0,
            )
        self.assertIsNone(transport.intent)
        self.assertIsNone(self.registry.unresolved_action_attempt("t1"))
        self.assertEqual(self.registry.get_task("t1").recovery_attempts, 0)

    def test_atomic_arm_refuses_exhausted_budget_without_action_row(self):
        self.registry._conn.execute(
            "UPDATE tasks SET recovery_attempts=max_recovery_attempts WHERE task_id='t1'"
        )
        self.registry._conn.commit()
        with self.assertRaisesRegex(Exception, "budget"):
            execute_current_worker_recovery(
                self.registry,
                plan=self.plan(),
                recommendation=self.recommendation(),
                policy=DispatchExecutionPolicy(enabled=True, confirmed_task_id="t1"),
                transport_factory=lambda binding: FakeTransport(),
                now=150.0,
            )
        self.assertIsNone(self.registry.unresolved_action_attempt("t1"))
        self.assertEqual(self.registry.action_attempts("t1"), [])

    def _submitted_attempt(self):
        transport = FakeTransport()
        result = execute_current_worker_recovery(
            self.registry,
            plan=self.plan(),
            recommendation=self.recommendation(),
            policy=DispatchExecutionPolicy(enabled=True, confirmed_task_id="t1"),
            transport_factory=lambda binding: transport,
            now=150.0,
        )
        return result.attempt_id

    def test_literal_error_can_complete_synthetic_recovery_lifecycle_once(self):
        literal_error = BrowserObservation(
            worker_id=self.worker.worker_id,
            observed_at=149.0,
            url=URL,
            generating=False,
            send_button_ready=True,
            visible_error="Error in message stream",
            last_dom_change_at=130.0,
            message_signature="literal-error-signature",
            raw={"source": "windows_uia_chrome", "composer_present": True},
        )
        self.registry.record_browser_observation(literal_error)
        plan = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=literal_error,
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
            now=150.0,
        )
        self.assertTrue(plan.would_dispatch)
        self.assertTrue(plan.checks["explicit_recoverable_delivery_error"])

        transport = FakeTransport()
        execution = execute_current_worker_recovery(
            self.registry,
            plan=plan,
            recommendation=self.recommendation(),
            policy=DispatchExecutionPolicy(enabled=True, confirmed_task_id="t1"),
            transport_factory=lambda binding: transport,
            now=150.0,
        )
        self.assertTrue(execution.submitted)
        self.assertEqual(execution.state, ActionAttemptState.SUBMITTED.value)

        reconciled = reconcile_action_with_uia(
            self.registry,
            attempt_id=execution.attempt_id,
            observer_factory=lambda chrome: FakeObserver(chrome),
            now=155.0,
        )
        self.assertTrue(reconciled.acknowledged)
        self.assertEqual(reconciled.state, ActionAttemptState.ACKNOWLEDGED.value)
        self.assertIsNone(self.registry.unresolved_action_attempt("t1"))

    def test_positive_single_nonce_completion_acknowledges(self):
        attempt_id = self._submitted_attempt()
        observer = FakeObserver(CHROME)
        result = reconcile_action_with_uia(
            self.registry,
            attempt_id=attempt_id,
            observer_factory=lambda chrome: observer,
            now=155.0,
        )
        self.assertTrue(result.acknowledged)
        self.assertEqual(result.state, ActionAttemptState.ACKNOWLEDGED.value)
        attempt = self.registry.get_action_attempt(attempt_id)
        self.assertEqual(observer.expected_nonce, f"LWS-ACTION-{attempt.nonce}")
        self.assertIsNone(self.registry.unresolved_action_attempt("t1"))

    def test_ack_waits_while_generation_is_active_or_nonce_count_is_wrong(self):
        attempt_id = self._submitted_attempt()
        result = reconcile_action_with_uia(
            self.registry,
            attempt_id=attempt_id,
            observer_factory=lambda chrome: FakeObserver(chrome, generating=True),
            now=155.0,
        )
        self.assertFalse(result.acknowledged)
        self.assertEqual(self.registry.get_action_attempt(attempt_id).state, ActionAttemptState.SUBMITTED)


if __name__ == "__main__":
    unittest.main()
