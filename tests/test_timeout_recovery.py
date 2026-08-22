import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lws.actions import ActionAcknowledgement, build_action_attempt, evidence_digest
from lws.dispatcher import DispatchAction, DispatchPlan
from lws.models import BrowserObservation, SupervisorState
from lws.registry import Registry
from lws.timeout_recovery import (
    TimeoutRecoveryPolicy,
    gate_timeout_dispatch_plan,
    is_recoverable_delivery_error,
)


NOW = 1000.0


class TimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(Path(self.tmp.name) / "registry.sqlite3")
        self.task = self.registry.register_task(
            task_id="timeout-task",
            project="lws",
            objective="recover delivery timeout",
            cwd="C:/repo",
            lsm_session_id="s1",
            conversation_url="https://web.example/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        self.worker = self.registry.get_worker(self.task.current_worker_id)

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def browser(
        self,
        *,
        error="Message delivery timed out",
        signature="timeout-signature",
        generating=False,
        observed_at=NOW,
    ):
        return BrowserObservation(
            worker_id=self.worker.worker_id,
            observed_at=observed_at,
            url=self.worker.conversation_url,
            generating=generating,
            send_button_ready=True,
            visible_error=error,
            last_dom_change_at=NOW - 30,
            message_signature=signature,
            raw={"source": "windows_uia_chrome"},
        )

    def plan(self):
        return DispatchPlan(
            task_id=self.task.task_id,
            created_at=NOW,
            action=DispatchAction.CONTINUE_CURRENT_WORKER,
            candidate_ready=True,
            transport_enabled=True,
            would_dispatch=True,
            reason="all ordinary fences passed",
            previous_reconcile_id="rec-a",
            current_reconcile_id="rec-b",
            fence_token="fence-v2",
        )

    def test_only_explicit_delivery_errors_are_eligible(self):
        self.assertTrue(is_recoverable_delivery_error(self.browser()))
        self.assertTrue(
            is_recoverable_delivery_error(
                self.browser(error="There was an error generating a response")
            )
        )
        self.assertTrue(
            is_recoverable_delivery_error(self.browser(error="Error in message stream"))
        )
        self.assertFalse(is_recoverable_delivery_error(self.browser(error=None)))
        self.assertFalse(is_recoverable_delivery_error(self.browser(error="network looks slow")))

    def test_autorecovery_requires_explicit_policy_opt_in(self):
        blocked = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=self.browser(),
            policy=TimeoutRecoveryPolicy(enabled=False),
        )
        self.assertFalse(blocked.candidate_ready)
        self.assertFalse(blocked.would_dispatch)
        self.assertFalse(blocked.checks["timeout_autorecovery_enabled"])

        allowed = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=self.browser(),
            policy=TimeoutRecoveryPolicy(enabled=True),
        )
        self.assertTrue(allowed.candidate_ready)
        self.assertTrue(allowed.would_dispatch)
        self.assertTrue(allowed.checks["explicit_recoverable_delivery_error"])

    def test_positive_ack_signature_suppresses_replay_of_old_error_banner(self):
        plan = self.plan()
        attempt = build_action_attempt(
            plan,
            self.task,
            self.worker,
            "Resume safely.",
            fence_version=2,
            pre_action_signature="before",
            now=NOW - 20,
        )
        self.registry.record_action_attempt(attempt)
        self.registry.mark_action_submitted(
            attempt.attempt_id,
            transport_name="fake",
            submitted_at=NOW - 19,
        )
        self.registry.acknowledge_action(
            ActionAcknowledgement(
                attempt_id=attempt.attempt_id,
                worker_id=self.worker.worker_id,
                observed_at=NOW - 10,
                accepted=True,
                kind="uia_nonce_hash",
                evidence_hash=evidence_digest("ack"),
                text_signature="nonce-observer-signature",
            )
        )
        self.registry.record_action_ack_browser_signature(
            attempt.attempt_id,
            message_signature="post-action-signature",
        )

        unchanged = gate_timeout_dispatch_plan(
            self.registry,
            plan,
            browser=self.browser(signature="post-action-signature"),
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
            now=NOW,
        )
        self.assertFalse(unchanged.candidate_ready)
        self.assertFalse(unchanged.would_dispatch)
        self.assertFalse(unchanged.checks["timeout_state_not_already_acknowledged"])

        changed = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=self.browser(signature="new-timeout-signature"),
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
            now=NOW,
        )
        self.assertTrue(changed.candidate_ready)
        self.assertTrue(changed.would_dispatch)

    def test_recent_recovery_enforces_cooldown_even_after_ack(self):
        plan = self.plan()
        attempt = build_action_attempt(
            plan,
            self.task,
            self.worker,
            "Resume safely.",
            fence_version=2,
            pre_action_signature="before",
            now=NOW - 20,
        )
        self.registry.record_action_attempt(attempt)
        self.registry.mark_action_submitted(
            attempt.attempt_id,
            transport_name="fake",
            submitted_at=NOW - 19,
        )
        self.registry.acknowledge_action(
            ActionAcknowledgement(
                attempt_id=attempt.attempt_id,
                worker_id=self.worker.worker_id,
                observed_at=NOW - 10,
                accepted=True,
                kind="uia_nonce_hash",
                evidence_hash=evidence_digest("ack"),
            )
        )
        blocked = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=self.browser(signature="new-timeout-signature"),
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=60),
            now=NOW,
        )
        self.assertFalse(blocked.candidate_ready)
        self.assertFalse(blocked.checks["timeout_recovery_cooldown_elapsed"])

        allowed = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=self.browser(signature="new-timeout-signature"),
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=60),
            now=NOW + 41,
        )
        self.assertTrue(allowed.checks["timeout_recovery_cooldown_elapsed"])

    def test_old_error_banner_shadowed_by_newer_generation_cannot_trigger_recovery(self):
        active = self.browser(
            signature="manual-turn-generating",
            generating=True,
            observed_at=NOW - 2,
        )
        idle_same_error = self.browser(
            signature="manual-turn-complete",
            generating=False,
            observed_at=NOW - 1,
        )
        self.registry.record_browser_observation(active)
        self.registry.record_browser_observation(idle_same_error)

        blocked = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=idle_same_error,
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
            now=NOW,
        )
        self.assertFalse(blocked.candidate_ready)
        self.assertFalse(
            blocked.checks["timeout_error_not_shadowed_by_newer_generation"]
        )

        cleared = self.browser(
            error=None,
            signature="banner-cleared",
            generating=False,
            observed_at=NOW + 1,
        )
        fresh_timeout = self.browser(
            signature="fresh-timeout",
            generating=False,
            observed_at=NOW + 2,
        )
        self.registry.record_browser_observation(cleared)
        self.registry.record_browser_observation(fresh_timeout)
        allowed = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=fresh_timeout,
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
            now=NOW + 2,
        )
        self.assertTrue(
            allowed.checks["timeout_error_not_shadowed_by_newer_generation"]
        )
        self.assertTrue(allowed.candidate_ready)

    def test_literal_error_banner_is_suppressed_after_newer_generation(self):
        active = self.browser(
            error="Error in message stream",
            signature="literal-error-generating",
            generating=True,
            observed_at=NOW - 2,
        )
        idle = self.browser(
            error="Error in message stream",
            signature="literal-error-idle",
            generating=False,
            observed_at=NOW - 1,
        )
        self.registry.record_browser_observation(active)
        self.registry.record_browser_observation(idle)

        blocked = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=idle,
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
            now=NOW,
        )
        self.assertFalse(blocked.candidate_ready)
        self.assertFalse(blocked.would_dispatch)
        self.assertTrue(blocked.checks["explicit_recoverable_delivery_error"])
        self.assertFalse(
            blocked.checks["timeout_error_not_shadowed_by_newer_generation"]
        )

    def test_stale_banner_shadow_does_not_expire_after_thirty_observations(self):
        active = self.browser(
            signature="normal-turn-generating",
            generating=True,
            observed_at=NOW - 100,
        )
        self.registry.record_browser_observation(active)
        latest = None
        for index in range(35):
            latest = self.browser(
                signature=f"stale-banner-idle-{index}",
                generating=False,
                observed_at=NOW - 99 + index,
            )
            self.registry.record_browser_observation(latest)

        blocked = gate_timeout_dispatch_plan(
            self.registry,
            self.plan(),
            browser=latest,
            policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
            now=NOW,
        )
        self.assertFalse(blocked.candidate_ready)
        self.assertFalse(blocked.would_dispatch)
        self.assertFalse(
            blocked.checks["timeout_error_not_shadowed_by_newer_generation"]
        )

    def test_unbounded_same_error_episode_fails_closed_when_history_is_saturated(self):
        latest = None
        for index in range(5):
            latest = self.browser(
                signature=f"same-error-{index}",
                generating=False,
                observed_at=NOW - 5 + index,
            )
            self.registry.record_browser_observation(latest)

        with patch("lws.timeout_recovery.OBSERVATION_RETENTION_PER_ENTITY", 5):
            blocked = gate_timeout_dispatch_plan(
                self.registry,
                self.plan(),
                browser=latest,
                policy=TimeoutRecoveryPolicy(enabled=True, cooldown_s=0),
                now=NOW,
            )
        self.assertFalse(blocked.candidate_ready)
        self.assertFalse(blocked.would_dispatch)
        self.assertFalse(
            blocked.checks["timeout_error_not_shadowed_by_newer_generation"]
        )


if __name__ == "__main__":
    unittest.main()
