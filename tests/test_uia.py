import unittest

from cws.models import BrowserObservation, WorkerRecord, WorkerStatus
from cws.uia import (
    UiaProbeUnavailable,
    conversation_id_from_url,
    normalize_url,
    payload_from_uia_result,
)


class UiaNormalizationTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(
            normalize_url("https://chatgpt.com/c/abc/"),
            "chatgpt.com/c/abc",
        )

    def test_conversation_id(self):
        url = "https://chatgpt.com/g/project/c/6a847d07-556c-83e8-8ed6-060b27f3b35c"
        self.assertEqual(
            conversation_id_from_url(url),
            "6a847d07-556c-83e8-8ed6-060b27f3b35c",
        )

    def test_payload_detects_generation_and_delivery_error(self):
        result = {
            "observed_at": 100.0,
            "address": "chatgpt.com/c/abc",
            "text_tail": "historical text\nhello\nMessage delivery timed out. Please try again.",
            "visible_text_tail": "Message delivery timed out. Please try again.",
            "buttons": [
                {"text": "Stop answering", "enabled": True, "offscreen": False},
            ],
            "browser_pid": 123,
            "window_title": "ChatGPT - Google Chrome",
            "selected_tab_label": "ChatGPT - High memory usage - 1.2 GB",
            "tool_status_labels": ["Inspecting project files"],
        }
        payload = payload_from_uia_result(result)
        self.assertTrue(payload["generating"])
        self.assertIsNone(payload["send_button_ready"])
        self.assertIn("Message delivery timed out", payload["visible_error"])
        self.assertEqual(payload["raw"]["source"], "windows_uia_chrome")
        self.assertEqual(payload["raw"]["browser_pid"], 123)
        self.assertNotIn("window_title", payload["raw"])
        self.assertNotIn("selected_tab_label", payload["raw"])
        self.assertNotIn("tool_status_labels", payload["raw"])
        self.assertNotIn("prompt_value", payload["raw"])
        self.assertEqual(payload["visible_error"], "Message delivery timed out")

    def test_payload_detects_ready_send_control(self):
        result = {
            "observed_at": 100.0,
            "address": "https://chatgpt.com/c/abc",
            "text_tail": "done",
            "visible_text_tail": "done",
            "buttons": [
                {"text": "Send prompt", "enabled": True, "offscreen": False},
            ],
        }
        payload = payload_from_uia_result(result)
        self.assertFalse(payload["generating"])
        self.assertTrue(payload["send_button_ready"])

    def test_missing_stop_and_send_controls_is_unknown_not_idle(self):
        result = {
            "observed_at": 100.0,
            "address": "https://chatgpt.com/c/abc",
            "text_tail": "accessibility tree may omit transient controls",
            "visible_text_tail": "accessibility tree may omit transient controls",
            "buttons": [],
        }
        payload = payload_from_uia_result(result)
        self.assertIsNone(payload["generating"])
        self.assertIsNone(payload["send_button_ready"])

    def test_error_result_is_not_normalized_as_browser_state(self):
        with self.assertRaises(UiaProbeUnavailable):
            payload_from_uia_result({"error": "no Chrome window matched"})


if __name__ == "__main__":
    unittest.main()
