import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lws.actions import ActionAttemptState, TransportSubmission
from lws.cli import (
    AUTO_WATCH_TASK_PREFIX,
    _auto_continue_overrun_cycle,
    _canonical_overrun_tasks,
    _sync_visible_conversation_watchers,
)
from lws.dispatch_runtime import ActionReconcileResult
from lws.models import Assessment, BrowserObservation, SupervisorState
from lws.overrun_continuation import OverrunContinuationPolicy
from lws.registry import Registry


URL = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ALT_URL = "https://chatgpt.com/g/g-p-fixture/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HWND = 12345
PID = 54321


class FakeTransport:
    name = "fake-overrun"

    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.intents = []

    def submit(self, intent):
        self.calls += 1
        self.intents.append(intent)
        return self.result


class OverrunCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.tmp.name) / "registry.sqlite3")
        self.task = self.registry.register_task(
            task_id="cycle-task",
            project="lws",
            objective="cycle fixture",
            cwd=self.tmp.name,
            conversation_url=URL,
        )
        self.worker = self.registry.get_worker(self.task.current_worker_id)
        self.registry._conn.execute(
            "UPDATE workers SET started_at=0 WHERE worker_id=?",
            (self.worker.worker_id,),
        )
        self.registry._conn.commit()
        self.policy = OverrunContinuationPolicy(
            enabled=True,
            overrun_after_s=1520.0,
            sample_lead_s=60.0,
            max_browser_observation_age_s=60.0,
            retry_cooldown_s=120.0,
        )

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def _record_browser(self, observed_at):
        self.registry.record_browser_observation(
            BrowserObservation(
                worker_id=self.worker.worker_id,
                observed_at=float(observed_at),
                url=URL,
                generating=False,
                send_button_ready=True,
                last_dom_change_at=1400.0,
                message_signature="stable-work-turn",
                raw={"source": "windows_uia_chrome", "composer_present": True},
            )
        )
        self.registry.bind_worker_window(
            self.worker.worker_id,
            window_handle=HWND,
            browser_pid=PID,
            chrome_executable=CHROME,
            conversation_url=URL,
            observed_at=float(observed_at),
            ttl_s=300.0,
        )

    def _assessment(self, registry, task_id, *args, **kwargs):
        task = registry.get_task(task_id)
        browser = registry.latest_browser_observation(task.current_worker_id)
        return (
            task,
            browser,
            None,
            None,
            Assessment(
                SupervisorState.RUNNING,
                "synthetic active long turn",
                "high",
                ["fixture"],
            ),
        )

    def _run(self, now, transport, *, reconcile=None):
        reconcile = reconcile or (
            lambda registry, attempt_id, observer_factory, now=None: ActionReconcileResult(
                attempt_id=attempt_id,
                state=registry.get_action_attempt(attempt_id).state.value,
                acknowledged=False,
                detail="still unresolved",
            )
        )

        def record_current(registry, task, browser, lsm, workspace, assessment):
            record = __import__("lws.models", fromlist=["ReconciliationRecord"]).ReconciliationRecord(
                reconcile_id=f"rec-{float(now):.3f}",
                task_id=task.task_id,
                created_at=float(now),
                state=assessment.state.value,
                confidence=assessment.confidence,
                reason=assessment.reason,
                requires_reconcile=assessment.requires_reconcile,
                current_worker_id=task.current_worker_id,
                fence_token="stable-cycle-fence",
                fence_version=2,
                evidence=list(assessment.evidence),
                snapshot={},
            )
            registry.record_reconciliation(record)
            return record

        with (
            patch("lws.cli._refresh_uia", return_value=None),
            patch("lws.cli._assessment", side_effect=self._assessment),
            patch("lws.cli._record_current_reconciliation", side_effect=record_current),
            patch("lws.cli.ChromeUiaActionTransport.from_binding", return_value=transport),
            patch("lws.cli.reconcile_action_with_uia", side_effect=reconcile),
        ):
            return _auto_continue_overrun_cycle(
                self.registry,
                object(),
                object(),
                policy=self.policy,
                now=float(now),
            )

    def _prime_sample(self):
        self._record_browser(1465.0)
        transport = FakeTransport(
            TransportSubmission(True, True, "fake-overrun", "should not send")
        )
        result = self._run(1465.0, transport)
        self.assertEqual(transport.calls, 0)
        self.assertEqual(self.registry.action_attempts(self.task.task_id), [])
        self.assertIsNotNone(self.registry.latest_reconciliation(self.task.task_id))
        return result

    def test_prethreshold_cycle_only_primes_stable_reconciliation_then_due_cycle_sends_once(self):
        self._prime_sample()
        self._record_browser(1520.0)
        transport = FakeTransport(
            TransportSubmission(True, True, "fake-overrun", "submitted")
        )
        result = self._run(1520.0, transport)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["submitted"])
        attempt = self.registry.unresolved_action_attempt(self.task.task_id)
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.state, ActionAttemptState.SUBMITTED)
        self.assertEqual(attempt.metadata["trigger_kind"], "hard_overrun_25m20")
        self.assertIn("continue", transport.intents[0].prompt)

        self._record_browser(1550.0)
        self._run(1550.0, transport)
        self.assertEqual(transport.calls, 1, "unresolved action must fence duplicate send")

    def test_live_cycle_accepts_evidence_recorded_after_cycle_clock_sample(self):
        self._prime_sample()
        self._record_browser(1520.25)
        transport = FakeTransport(
            TransportSubmission(True, True, "fake-overrun", "submitted")
        )

        def record_current(registry, task, browser, lsm, workspace, assessment):
            record = __import__("lws.models", fromlist=["ReconciliationRecord"]).ReconciliationRecord(
                reconcile_id="rec-live-later-evidence",
                task_id=task.task_id,
                created_at=1520.5,
                state=assessment.state.value,
                confidence=assessment.confidence,
                reason=assessment.reason,
                requires_reconcile=assessment.requires_reconcile,
                current_worker_id=task.current_worker_id,
                fence_token="stable-cycle-fence",
                fence_version=2,
                evidence=list(assessment.evidence),
                snapshot={},
            )
            registry.record_reconciliation(record)
            return record

        with (
            patch("lws.cli._refresh_uia", return_value=None),
            patch("lws.cli._assessment", side_effect=self._assessment),
            patch("lws.cli._record_current_reconciliation", side_effect=record_current),
            patch("lws.cli.ChromeUiaActionTransport.from_binding", return_value=transport),
            patch(
                "lws.cli.reconcile_action_with_uia",
                side_effect=lambda registry, attempt_id, observer_factory, now=None: ActionReconcileResult(
                    attempt_id=attempt_id,
                    state=registry.get_action_attempt(attempt_id).state.value,
                    acknowledged=False,
                    detail="still unresolved",
                ),
            ),
        ):
            result = _auto_continue_overrun_cycle(
                self.registry,
                object(),
                object(),
                policy=self.policy,
                now=1520.0,
            )

        self.assertEqual(transport.calls, 1)
        dispatches = [row for row in result if row.get("kind") == "overrun_dispatch"]
        self.assertEqual(len(dispatches), 1)
        self.assertTrue(dispatches[0]["submitted"])

    def test_visible_discovery_creates_one_conversation_owner_and_dedupes_task_aliases(self):
        class DiscoveryProbe:
            chrome_executable = CHROME

            def discover_conversations(self):
                return [
                    {
                        "observed_at": 1510.0,
                        "browser_pid": PID,
                        "window_handle": HWND,
                        "url": ALT_URL,
                        "conversation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    }
                ]

        policy = OverrunContinuationPolicy(
            enabled=True,
            overrun_after_s=1520.0,
            auto_discover_visible_conversations=True,
        )
        first = _sync_visible_conversation_watchers(self.registry, DiscoveryProbe(), policy=policy)
        second = _sync_visible_conversation_watchers(self.registry, DiscoveryProbe(), policy=policy)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        owner_id = first[0]
        self.assertTrue(owner_id.startswith(AUTO_WATCH_TASK_PREFIX))
        owner = self.registry.get_task(owner_id)
        self.assertEqual(len(self.registry.workers_for_task(owner_id)), 1)
        binding = self.registry.get_worker_window_binding(owner.current_worker_id)
        self.assertEqual(binding.window_handle, HWND)
        self.assertEqual(binding.browser_pid, PID)

        selected = _canonical_overrun_tasks(self.registry)
        same_conversation = [
            task
            for task in selected
            if self.registry.get_worker(task.current_worker_id).conversation_id
            == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ]
        self.assertEqual([task.task_id for task in same_conversation], [owner_id])

    def test_ambiguous_send_is_durable_and_never_replayed_automatically(self):
        self._prime_sample()
        self._record_browser(1520.0)
        transport = FakeTransport(
            TransportSubmission(False, True, "fake-overrun", "click outcome ambiguous")
        )
        self._run(1520.0, transport)
        self.assertEqual(transport.calls, 1)
        attempt = self.registry.unresolved_action_attempt(self.task.task_id)
        self.assertEqual(attempt.state, ActionAttemptState.RECONCILE_REQUIRED)

        self.registry.close()
        self.registry = Registry(Path(self.tmp.name) / "registry.sqlite3")
        self._record_browser(1550.0)
        self._run(1550.0, transport)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(
            self.registry.unresolved_action_attempt(self.task.task_id).state,
            ActionAttemptState.RECONCILE_REQUIRED,
        )

    def test_proven_presend_rate_limit_failure_enters_retry_cooldown(self):
        self._prime_sample()
        self._record_browser(1520.0)
        transport = FakeTransport(
            TransportSubmission(
                False,
                False,
                "fake-overrun",
                "Too many requests modal dismissed; retry only after cooldown",
            )
        )
        first = self._run(1520.0, transport)
        self.assertEqual(transport.calls, 1)
        self.assertFalse(first[0]["submitted"])
        attempt = self.registry.action_attempts(self.task.task_id, limit=1)[0]
        self.assertEqual(attempt.state, ActionAttemptState.FAILED)
        self.assertIsNone(self.registry.unresolved_action_attempt(self.task.task_id))

        self._record_browser(1550.0)
        second = self._run(1550.0, transport)
        self.assertEqual(transport.calls, 1)
        dispatches = [row for row in second if row.get("kind") == "overrun_dispatch"]
        self.assertEqual(len(dispatches), 1)
        self.assertIn("cooldown", " ".join(dispatches[0].get("blockers") or []))


if __name__ == "__main__":
    unittest.main()
