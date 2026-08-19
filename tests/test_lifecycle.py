import unittest

from cws.lifecycle import (
    PageCloseEvidence,
    PageIsolationMode,
    evaluate_page_close_evidence,
)


class PageCloseEvidenceTests(unittest.TestCase):
    def valid(self, **changes):
        payload = dict(
            experiment_id="exp1",
            disposable_profile=True,
            normally_authenticated=True,
            auth_material_copied=False,
            pre_close_url="https://chatgpt.com/c/abc",
            reopened_url="https://chatgpt.com/c/abc",
            pre_close_generating=True,
            close_while_live_confirmed=True,
            background_progress_observed=True,
            completion_evidence_after_reopen=True,
            same_conversation_after_reopen=True,
            duplicate_turn_observed=False,
            auth_still_valid_after_reopen=True,
            pre_close_signature="before",
            post_reopen_signature="after",
        )
        payload.update(changes)
        return PageCloseEvidence(**payload)

    def test_strong_authenticated_disposable_profile_evidence_can_pass(self):
        result = evaluate_page_close_evidence(self.valid())
        self.assertTrue(result.parking_safe)
        self.assertEqual(result.blockers, [])
        self.assertTrue(result.checks["isolated_execution_context"])

    def test_existing_profile_exact_disposable_window_can_pass(self):
        result = evaluate_page_close_evidence(
            self.valid(
                disposable_profile=False,
                isolation_mode=PageIsolationMode.EXISTING_PROFILE_DISPOSABLE_WINDOW.value,
                exact_window_binding_confirmed=True,
                current_user_conversation_excluded=True,
            )
        )
        self.assertTrue(result.parking_safe)
        self.assertEqual(result.blockers, [])

    def test_existing_profile_window_must_be_exact_and_exclude_current_conversation(self):
        result = evaluate_page_close_evidence(
            self.valid(
                disposable_profile=False,
                isolation_mode=PageIsolationMode.EXISTING_PROFILE_DISPOSABLE_WINDOW.value,
                exact_window_binding_confirmed=False,
                current_user_conversation_excluded=False,
            )
        )
        self.assertFalse(result.parking_safe)
        self.assertIn("isolated_execution_context", result.blockers)

    def test_anonymous_experiment_can_never_enable_parking(self):
        result = evaluate_page_close_evidence(
            self.valid(normally_authenticated=False, auth_still_valid_after_reopen=False)
        )
        self.assertFalse(result.parking_safe)
        self.assertIn("normally_authenticated", result.blockers)

    def test_copied_auth_material_blocks_even_if_behavior_passes(self):
        result = evaluate_page_close_evidence(
            self.valid(
                auth_material_copied=True,
                isolation_mode=PageIsolationMode.COPIED_AUTH_PROFILE.value,
            )
        )
        self.assertFalse(result.parking_safe)
        self.assertIn("supported_isolation_mode", result.blockers)
        self.assertIn("no_auth_material_copy", result.blockers)

    def test_localhost_or_non_conversation_url_cannot_pass(self):
        result = evaluate_page_close_evidence(
            self.valid(
                pre_close_url="http://127.0.0.1:8000/test",
                reopened_url="http://127.0.0.1:8000/test",
            )
        )
        self.assertFalse(result.parking_safe)
        self.assertIn("pre_close_conversation_url", result.blockers)

    def test_duplicate_turn_or_missing_background_progress_blocks(self):
        result = evaluate_page_close_evidence(
            self.valid(duplicate_turn_observed=True, background_progress_observed=False)
        )
        self.assertFalse(result.parking_safe)
        self.assertIn("no_duplicate_turn", result.blockers)
        self.assertIn("background_progress_observed", result.blockers)

    def test_same_url_without_signature_advance_is_not_enough(self):
        result = evaluate_page_close_evidence(
            self.valid(post_reopen_signature="before")
        )
        self.assertFalse(result.parking_safe)
        self.assertIn("signature_advanced", result.blockers)

    def test_generation_parking_does_not_imply_tool_parking(self):
        result = evaluate_page_close_evidence(
            self.valid(
                disposable_profile=False,
                isolation_mode=PageIsolationMode.EXISTING_PROFILE_DISPOSABLE_WINDOW.value,
                exact_window_binding_confirmed=True,
                current_user_conversation_excluded=True,
            )
        )
        self.assertTrue(result.parking_safe)
        self.assertTrue(result.generation_parking_safe)
        self.assertFalse(result.tool_execution_parking_safe)
        self.assertIn("tool_execution_observed", result.tool_blockers)

    def test_strong_live_tool_close_reopen_evidence_can_pass_tool_gate(self):
        result = evaluate_page_close_evidence(
            self.valid(
                disposable_profile=False,
                isolation_mode=PageIsolationMode.EXISTING_PROFILE_DISPOSABLE_WINDOW.value,
                exact_window_binding_confirmed=True,
                current_user_conversation_excluded=True,
                tool_execution_observed=True,
                tool_job_identity_confirmed=True,
                tool_running_at_close=True,
                tool_completed_after_close=True,
                tool_final_response_after_reopen=True,
            )
        )
        self.assertTrue(result.generation_parking_safe)
        self.assertTrue(result.tool_execution_parking_safe)
        self.assertEqual(result.tool_blockers, [])
        self.assertIn("both generation and live-tool", result.conclusion)

    def test_tool_gate_fails_if_job_was_not_running_at_close(self):
        result = evaluate_page_close_evidence(
            self.valid(
                tool_execution_observed=True,
                tool_job_identity_confirmed=True,
                tool_running_at_close=False,
                tool_completed_after_close=True,
                tool_final_response_after_reopen=True,
            )
        )
        self.assertTrue(result.generation_parking_safe)
        self.assertFalse(result.tool_execution_parking_safe)
        self.assertIn("tool_running_at_close", result.tool_blockers)

    def test_unknown_isolation_mode_fails_closed(self):
        result = evaluate_page_close_evidence(self.valid(isolation_mode="mystery"))
        self.assertFalse(result.parking_safe)
        self.assertIn("supported_isolation_mode", result.blockers)


if __name__ == "__main__":
    unittest.main()
