import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lws.action_runtime import submit_armed_action
from lws.actions import (
    ActionAttempt,
    ActionAttemptState,
    ActionIntent,
    ActionTransportDisabled,
)
from lws.models import WorkerWindowBinding
from lws.registry import Registry
from lws.uia_actions import (
    ChromeUiaAckObserver,
    ChromeUiaActionTransport,
    UiaAckObservation,
    acknowledgement_from_uia_observation,
)


PROMPT = "harmless recovery prompt nonce-123"
URL = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def action_intent(worker_id="w1", prompt=PROMPT):
    return ActionIntent(
        attempt_id="act1",
        task_id="t1",
        worker_id=worker_id,
        action="CONTINUE_CURRENT_WORKER",
        prompt=prompt,
        prompt_hash="hash",
        nonce="nonce",
        fence_token="fence",
        fence_version=1,
    )


def action_attempt(worker_id="w1"):
    return ActionAttempt(
        attempt_id="act1",
        task_id="t1",
        worker_id=worker_id,
        action="CONTINUE_CURRENT_WORKER",
        fence_token="fence",
        fence_version=1,
        prompt_hash="hash",
        nonce="nonce",
        state=ActionAttemptState.SUBMITTED,
        created_at=100.0,
        updated_at=100.0,
    )


class ChromeUiaActionTransportTests(unittest.TestCase):
    def transport(self, **changes):
        payload = dict(
            expected_worker_id="w1",
            conversation_url=URL,
            expected_hwnd=1234,
            expected_browser_pid=4244,
            chrome_executable=CHROME,
            enabled=True,
        )
        payload.update(changes)
        return ChromeUiaActionTransport(**payload)

    def test_transport_is_disabled_by_default(self):
        transport = self.transport(enabled=False)
        with self.assertRaises(ActionTransportDisabled):
            transport.submit(action_intent())

    def binding(self, **changes):
        payload = dict(
            worker_id="w1",
            window_handle=1234,
            browser_pid=4244,
            chrome_executable=CHROME,
            conversation_url=URL,
            source="windows_uia_chrome",
            bound_at=100.0,
            observed_at=100.0,
            expires_at=200.0,
        )
        payload.update(changes)
        return WorkerWindowBinding(**payload)

    def test_transport_can_be_built_from_fresh_matching_window_binding(self):
        transport = ChromeUiaActionTransport.from_binding(
            self.binding(),
            expected_worker_id="w1",
            conversation_url=URL,
            now=150.0,
        )
        self.assertEqual(transport.expected_worker_id, "w1")
        self.assertEqual(transport.expected_hwnd, 1234)
        self.assertFalse(transport.enabled)

    def test_stale_or_mismatched_window_binding_fails_closed(self):
        with self.assertRaisesRegex(Exception, "stale"):
            ChromeUiaActionTransport.from_binding(
                self.binding(expires_at=100.0),
                expected_worker_id="w1",
                conversation_url=URL,
                now=150.0,
            )
        with self.assertRaisesRegex(Exception, "different worker"):
            ChromeUiaActionTransport.from_binding(
                self.binding(worker_id="other"),
                expected_worker_id="w1",
                conversation_url=URL,
                now=150.0,
            )
        with self.assertRaisesRegex(Exception, "URL"):
            ChromeUiaActionTransport.from_binding(
                self.binding(conversation_url="https://chatgpt.com/c/other"),
                expected_worker_id="w1",
                conversation_url=URL,
                now=150.0,
            )
        with self.assertRaisesRegex(Exception, "source"):
            ChromeUiaActionTransport.from_binding(
                self.binding(source="synthetic"),
                expected_worker_id="w1",
                conversation_url=URL,
                now=150.0,
            )

    def test_action_script_uses_bounded_post_draft_send_poll(self):
        from lws.uia_actions import _POWERSHELL_ACTION

        self.assertIn("for ($i = 0; $i -lt 40; $i++)", _POWERSHELL_ACTION)
        self.assertIn("Start-Sleep -Milliseconds 50", _POWERSHELL_ACTION)
        self.assertIn("bounded post-draft wait", _POWERSHELL_ACTION)

    def test_worker_mismatch_is_proven_no_side_effect(self):
        result = self.transport().submit(action_intent(worker_id="other"))
        self.assertFalse(result.submitted)
        self.assertFalse(result.side_effect_possible)
        self.assertIn("worker id", result.detail)

    def test_non_conversation_url_is_proven_no_side_effect(self):
        result = self.transport(conversation_url="https://chatgpt.com/").submit(action_intent())
        self.assertFalse(result.submitted)
        self.assertFalse(result.side_effect_possible)
        self.assertIn("/c/", result.detail)

    @patch("lws.uia_actions.os.name", "nt")
    @patch("lws.uia_actions._run_powershell_json")
    @patch("lws.uia_actions.time.time", return_value=200.0)
    def test_expired_binding_is_proven_no_side_effect(self, _now, run_helper):
        result = self.transport(binding_expires_at=199.0).submit(action_intent())
        self.assertFalse(result.submitted)
        self.assertFalse(result.side_effect_possible)
        self.assertIn("expired", result.detail)
        run_helper.assert_not_called()

    @patch("lws.uia_actions.os.name", "nt")
    @patch("lws.uia_actions._run_powershell_json")
    def test_exact_binding_and_prompt_are_passed_out_of_command_line(self, run_helper):
        run_helper.return_value = {
            "submitted": True,
            "side_effect_possible": True,
            "phase": "invoke_returned",
            "detail": "ok",
        }
        result = self.transport().submit(action_intent())
        self.assertTrue(result.submitted)
        kwargs = run_helper.call_args.kwargs
        self.assertEqual(kwargs["env"]["LWS_EXPECTED_URL"], URL)
        self.assertEqual(kwargs["env"]["LWS_EXPECTED_HWND"], "1234")
        self.assertEqual(kwargs["env"]["LWS_EXPECTED_BROWSER_PID"], "4244")
        self.assertEqual(kwargs["env"]["LWS_EXPECTED_CHROME_EXE"], CHROME)
        self.assertEqual(base64.b64decode(kwargs["env"]["LWS_PROMPT_B64"]).decode(), PROMPT)
        self.assertNotIn(PROMPT, run_helper.call_args.args[0])

    @patch("lws.uia_actions.os.name", "nt")
    @patch("lws.uia_actions._run_powershell_json")
    def test_non_ascii_prompt_round_trips_through_utf8_base64_transport(self, run_helper):
        prompt = "?????D:\\??\\?????????????"
        run_helper.return_value = {
            "submitted": True,
            "side_effect_possible": True,
            "phase": "invoke_returned",
            "detail": "ok",
        }
        result = self.transport().submit(action_intent(prompt=prompt))
        self.assertTrue(result.submitted)
        encoded = run_helper.call_args.kwargs["env"]["LWS_PROMPT_B64"]
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), prompt)
        self.assertNotIn(prompt, run_helper.call_args.args[0])

    @patch("lws.uia_actions.os.name", "nt")
    @patch("lws.uia_actions._run_powershell_json")
    def test_post_draft_failure_is_ambiguous_not_retryable(self, run_helper):
        run_helper.return_value = {
            "submitted": False,
            "side_effect_possible": True,
            "phase": "draft_set",
            "detail": "positive ready Send control did not appear after draft input",
        }
        result = self.transport().submit(action_intent())
        self.assertFalse(result.submitted)
        self.assertTrue(result.side_effect_possible)

    @patch("lws.uia_actions.os.name", "nt")
    @patch("lws.uia_actions._run_powershell_json")
    def test_runtime_persists_reconcile_required_for_ambiguous_uia_result(self, run_helper):
        run_helper.return_value = {
            "submitted": False,
            "side_effect_possible": True,
            "phase": "draft_set",
            "detail": "send missing after draft",
        }
        with tempfile.TemporaryDirectory() as td:
            reg = Registry(Path(td) / "r.sqlite3")
            try:
                task = reg.register_task(
                    task_id="t1",
                    project="p",
                    objective="o",
                    cwd=td,
                    conversation_url=URL,
                )
                worker = reg.get_worker(task.current_worker_id)
                from lws.actions import prompt_digest

                attempt = ActionAttempt(
                    attempt_id="act1",
                    task_id="t1",
                    worker_id=worker.worker_id,
                    action="CONTINUE_CURRENT_WORKER",
                    fence_token="fence",
                    fence_version=1,
                    prompt_hash=prompt_digest(PROMPT),
                    nonce="nonce",
                    state=ActionAttemptState.ARMED,
                    created_at=100.0,
                    updated_at=100.0,
                )
                reg.record_action_attempt(attempt)
                transport = self.transport(expected_worker_id=worker.worker_id)
                outcome = submit_armed_action(
                    reg,
                    attempt_id="act1",
                    prompt=PROMPT,
                    transport=transport,
                )
                self.assertEqual(outcome.state, ActionAttemptState.RECONCILE_REQUIRED.value)
                self.assertTrue(outcome.side_effect_possible)
            finally:
                reg.close()


class ChromeUiaAckTests(unittest.TestCase):
    def observation(self, **changes):
        payload = dict(
            worker_id="w1",
            observed_at=200.0,
            url=URL,
            window_handle=1234,
            browser_pid=4244,
            generating=False,
            send_button_ready=None,
            composer_present=True,
            signed_in_likely=True,
            nonce_occurrences=1,
            text_element_count=1200,
            text_signature="abc123",
        )
        payload.update(changes)
        return UiaAckObservation(**payload)

    def test_positive_nonce_hash_observation_builds_ack(self):
        ack = acknowledgement_from_uia_observation(
            action_attempt(),
            self.observation(),
            min_nonce_occurrences=1,
            max_nonce_occurrences=1,
        )
        self.assertIsNotNone(ack)
        self.assertTrue(ack.accepted)
        self.assertEqual(ack.kind, "uia_nonce_hash")
        self.assertNotIn("nonce-123", ack.evidence_hash)

    def test_generating_or_unsigned_or_wrong_worker_does_not_ack(self):
        self.assertIsNone(
            acknowledgement_from_uia_observation(
                action_attempt(), self.observation(generating=True)
            )
        )
        self.assertIsNone(
            acknowledgement_from_uia_observation(
                action_attempt(), self.observation(signed_in_likely=False)
            )
        )
        self.assertIsNone(
            acknowledgement_from_uia_observation(
                action_attempt(), self.observation(worker_id="other")
            )
        )

    def test_duplicate_nonce_ceiling_blocks_ack(self):
        ack = acknowledgement_from_uia_observation(
            action_attempt(),
            self.observation(nonce_occurrences=2),
            max_nonce_occurrences=1,
        )
        self.assertIsNone(ack)

    @patch("lws.uia_actions.os.name", "nt")
    @patch("lws.uia_actions._run_powershell_json")
    def test_ack_observer_returns_only_bounded_metadata(self, run_helper):
        run_helper.return_value = {
            "observed_at": 300.0,
            "url": "chatgpt.com/c/11111111-2222-3333-4444-555555555555",
            "window_handle": 999,
            "browser_pid": 4244,
            "generating": False,
            "send_button_ready": None,
            "composer_present": True,
            "signed_in_likely": True,
            "nonce_occurrences": 1,
            "text_element_count": 1512,
            "text_signature": "deadbeef",
        }
        observer = ChromeUiaAckObserver(chrome_executable=CHROME)
        obs = observer.observe(
            worker_id="w1",
            conversation_url=URL,
            expected_nonce="known-nonce",
            expected_hwnd=999,
        )
        self.assertEqual(obs.nonce_occurrences, 1)
        self.assertEqual(obs.text_signature, "deadbeef")
        self.assertFalse(hasattr(obs, "text"))
        self.assertFalse(hasattr(obs, "text_tail"))


if __name__ == "__main__":
    unittest.main()
