import unittest

from cws.lifecycle import PageCloseEvidence, evaluate_page_close_evidence


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

    def test_strong_authenticated_disposable_evidence_can_pass(self):
        result = evaluate_page_close_evidence(self.valid())
        self.assertTrue(result.parking_safe)
        self.assertEqual(result.blockers, [])

    def test_anonymous_experiment_can_never_enable_parking(self):
        result = evaluate_page_close_evidence(
            self.valid(normally_authenticated=False, auth_still_valid_after_reopen=False)
        )
        self.assertFalse(result.parking_safe)
        self.assertIn("normally_authenticated", result.blockers)

    def test_copied_auth_material_blocks_even_if_behavior_passes(self):
        result = evaluate_page_close_evidence(self.valid(auth_material_copied=True))
        self.assertFalse(result.parking_safe)
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


if __name__ == "__main__":
    unittest.main()
