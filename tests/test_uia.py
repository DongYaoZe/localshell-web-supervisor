import subprocess
import unittest
from unittest.mock import patch

from lws.models import BrowserObservation, WorkerRecord, WorkerStatus
from lws.uia import (
    ChromeUiaProbe,
    UiaProbeUnavailable,
    _POWERSHELL_DISCOVER,
    _POWERSHELL_PROBE,
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
        url = "https://chatgpt.com/g/project/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.assertEqual(
            conversation_id_from_url(url),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
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
            "window_handle": 456,
            "window_title": "web chat - Google Chrome",
            "selected_tab_label": "web chat - High memory usage - 1.2 GB",
            "tool_status_labels": ["Inspecting project files"],
        }
        payload = payload_from_uia_result(result)
        self.assertTrue(payload["generating"])
        self.assertIsNone(payload["send_button_ready"])
        self.assertIn("Message delivery timed out", payload["visible_error"])
        self.assertEqual(payload["raw"]["source"], "windows_uia_chrome")
        self.assertEqual(payload["raw"]["browser_pid"], 123)
        self.assertEqual(payload["raw"]["window_handle"], 456)
        self.assertNotIn("window_title", payload["raw"])
        self.assertNotIn("selected_tab_label", payload["raw"])
        self.assertNotIn("tool_status_labels", payload["raw"])
        self.assertNotIn("prompt_value", payload["raw"])
        self.assertEqual(payload["visible_error"], "Message delivery timed out")

    def test_payload_detects_literal_error_in_message_stream(self):
        result = {
            "observed_at": 100.0,
            "address": "chatgpt.com/c/abc",
            "text_tail": "historical text\nError in message stream",
            "visible_text_tail": "Error in message stream",
            "buttons": [
                {"text": "Send prompt", "enabled": True, "offscreen": False},
            ],
            "composer_present": True,
        }
        payload = payload_from_uia_result(result)
        self.assertEqual(payload["visible_error"], "Error in message stream")
        self.assertFalse(payload["generating"])
        self.assertTrue(payload["send_button_ready"])

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

    def test_probe_enumerates_desktop_windows_and_fails_on_ambiguous_url(self):
        self.assertIn("AutomationElement]::RootElement", _POWERSHELL_PROBE)
        self.assertIn("Chrome_WidgetWin_1", _POWERSHELL_PROBE)
        self.assertIn("multiple Chrome windows matched", _POWERSHELL_PROBE)
        self.assertIn("'view_1012'", _POWERSHELL_PROBE)
        self.assertNotIn("Address and search bar", _POWERSHELL_PROBE)
        self.assertNotIn("MainWindowHandle", _POWERSHELL_PROBE)

    def test_discovery_probe_is_identity_only_and_uses_address_bar(self):
        self.assertIn("AutomationElement]::RootElement", _POWERSHELL_DISCOVER)
        self.assertIn("'view_1012'", _POWERSHELL_DISCOVER)
        self.assertIn("window_handle", _POWERSHELL_DISCOVER)
        self.assertIn("browser_pid", _POWERSHELL_DISCOVER)
        self.assertIn("address", _POWERSHELL_DISCOVER)
        self.assertNotIn("text_tail", _POWERSHELL_DISCOVER)
        self.assertNotIn("prompt-textarea", _POWERSHELL_DISCOVER)

    def test_probe_timeout_is_converted_to_uia_unavailable(self):
        probe = ChromeUiaProbe(timeout_s=1, chrome_executable=r"C:\\chrome.exe")
        with patch(
            "lws.uia.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["powershell.exe"], timeout=1),
        ):
            with self.assertRaisesRegex(UiaProbeUnavailable, "timed out"):
                probe.raw_probe("https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_error_result_is_not_normalized_as_browser_state(self):
        with self.assertRaises(UiaProbeUnavailable):
            payload_from_uia_result({"error": "no Chrome window matched"})


if __name__ == "__main__":
    unittest.main()
