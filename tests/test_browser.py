import unittest

from cws.browser import (
    dom_payload_from_lsm_snapshot,
    observation_from_dom_payload,
    observation_from_lsm_snapshot,
)


class BrowserObservationTests(unittest.TestCase):
    def test_same_tail_preserves_last_dom_change(self):
        first = observation_from_dom_payload(
            "w1",
            {"observed_at": 100.0, "url": "https://chatgpt.com/c/x", "text_tail": "hello"},
        )
        second = observation_from_dom_payload(
            "w1",
            {"observed_at": 150.0, "url": "https://chatgpt.com/c/x", "text_tail": "hello"},
            previous=first,
        )
        self.assertEqual(first.last_dom_change_at, 100.0)
        self.assertEqual(second.last_dom_change_at, 100.0)
        self.assertEqual(first.message_signature, second.message_signature)

    def test_changed_tail_resets_last_dom_change(self):
        first = observation_from_dom_payload(
            "w1", {"observed_at": 100.0, "text_tail": "one"}
        )
        second = observation_from_dom_payload(
            "w1",
            {"observed_at": 150.0, "text_tail": "two"},
            previous=first,
        )
        self.assertEqual(second.last_dom_change_at, 150.0)
        self.assertNotEqual(first.message_signature, second.message_signature)

    def test_lsm_snapshot_infers_error_and_send_button(self):
        snapshot = {
            "url": "https://chatgpt.com/c/x",
            "text": "Answer\nMessage delivery timed out. Please try again.",
            "text_truncated": False,
            "interactive_elements": [
                {"tag": "button", "role": None, "text": "Send prompt", "disabled": False}
            ],
            "errors": [],
            "network": [],
        }
        obs = observation_from_lsm_snapshot("w1", snapshot)
        self.assertTrue(obs.send_button_ready)
        self.assertIn("Message delivery timed out", obs.visible_error)
        self.assertIsNotNone(obs.message_signature)
        self.assertIsNotNone(obs.last_dom_change_at)

    def test_stop_button_is_positive_generation_evidence(self):
        snapshot = {
            "url": "https://chatgpt.com/c/x",
            "text": "working",
            "text_truncated": False,
            "interactive_elements": [
                {"tag": "button", "role": None, "text": "Stop generating", "disabled": False}
            ],
        }
        payload = dom_payload_from_lsm_snapshot(snapshot)
        self.assertTrue(payload["generating"])
        self.assertIsNone(payload["send_button_ready"])

    def test_truncated_lsm_body_prefix_is_not_a_message_signature(self):
        snapshot = {
            "url": "https://chatgpt.com/c/x",
            "text": "first 50000 chars never change after conversation grows",
            "text_truncated": True,
            "interactive_elements": [],
        }
        payload = dom_payload_from_lsm_snapshot(snapshot)
        self.assertFalse(payload["signature_reliable"])
        obs = observation_from_lsm_snapshot("w1", snapshot)
        self.assertIsNone(obs.message_signature)
        self.assertIsNone(obs.last_dom_change_at)


if __name__ == "__main__":
    unittest.main()
