import unittest
from unittest.mock import patch

from cws.models import ProbeWindowSlotBinding, WorkerRecord, WorkerStatus
from cws.page_runtime import (
    ChromeUiaProbeWindowTransport,
    DisabledProbeWindowTransport,
    ProbeSlotAction,
    ProbeWindowTransportDisabled,
    plan_probe_slot,
    slot_owns_actual_url,
    tagged_probe_url,
)

URL1 = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL2 = "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def worker(url=URL1, status=WorkerStatus.PARKED, worker_id="w1"):
    return WorkerRecord(worker_id, "t1", url, None, status, 1.0)


def slot(url=URL1, worker_id="w1", expires=200.0, owner="owner"):
    actual = tagged_probe_url(url, slot_id="probe:default", owner_token=owner)
    return ProbeWindowSlotBinding(
        "probe:default",
        owner,
        worker_id,
        url,
        actual,
        123,
        456,
        CHROME,
        "windows_uia_cws_probe",
        100.0,
        100.0,
        expires,
    )


class ProbeSlotPlannerTests(unittest.TestCase):
    def test_no_slot_opens_exactly_one_owned_probe(self):
        plan = plan_probe_slot(worker(), None, now=150.0)
        self.assertEqual(plan.action, ProbeSlotAction.OPEN)
        self.assertTrue(plan.mutation_required)
        self.assertTrue(plan.owner_token)

    def test_same_fresh_target_reuses(self):
        plan = plan_probe_slot(worker(), slot(), now=150.0)
        self.assertEqual(plan.action, ProbeSlotAction.REUSE)
        self.assertFalse(plan.mutation_required)

    def test_different_target_rotates_close_before_open(self):
        plan = plan_probe_slot(worker(URL2, worker_id="w2"), slot(), now=150.0)
        self.assertEqual(plan.action, ProbeSlotAction.ROTATE)
        self.assertTrue(plan.mutation_required)

    def test_stale_or_ambiguous_slot_blocks_instead_of_opening_another(self):
        plan = plan_probe_slot(worker(), slot(expires=149.0), now=150.0)
        self.assertEqual(plan.action, ProbeSlotAction.BLOCKED)
        self.assertFalse(plan.mutation_required)

        bad = slot()
        bad.actual_url = URL1
        plan = plan_probe_slot(worker(), bad, now=150.0)
        self.assertEqual(plan.action, ProbeSlotAction.BLOCKED)

    def test_active_worker_is_not_a_probe_target(self):
        plan = plan_probe_slot(worker(status=WorkerStatus.ACTIVE), None, now=150.0)
        self.assertEqual(plan.action, ProbeSlotAction.BLOCKED)

    def test_tag_is_exact_and_non_secret(self):
        actual = tagged_probe_url(URL1, slot_id="probe:default", owner_token="abc")
        self.assertTrue(actual.endswith("#cws-probe=probe:default:abc"))
        self.assertTrue(slot_owns_actual_url(slot(owner="abc")))

    def test_disabled_transport_never_mutates(self):
        plan = plan_probe_slot(worker(), None, now=150.0)
        with self.assertRaises(ProbeWindowTransportDisabled):
            DisabledProbeWindowTransport().execute(plan, existing=None)

    @patch("cws.page_runtime.os.name", "nt")
    @patch("cws.page_runtime.subprocess.Popen")
    def test_open_transport_binds_unique_tagged_window(self, popen):
        plan = plan_probe_slot(worker(), None, now=150.0)
        transport = ChromeUiaProbeWindowTransport(
            chrome_executable=CHROME,
            enabled=True,
            open_timeout_s=1,
        )
        with patch.object(
            transport,
            "_find",
            return_value={
                "count": 1,
                "matches": [{"window_handle": 99, "browser_pid": 77, "actual_url": "x"}],
            },
        ):
            out = transport.execute(plan, existing=None)
        self.assertTrue(out.changed)
        self.assertEqual(out.binding.target_worker_id, "w1")
        self.assertEqual(out.binding.window_handle, 99)
        popen.assert_called_once()

    @patch("cws.page_runtime.os.name", "nt")
    @patch("cws.page_runtime.subprocess.Popen")
    def test_rotate_refuses_to_open_if_exact_close_is_ambiguous(self, popen):
        existing = slot()
        plan = plan_probe_slot(worker(URL2, worker_id="w2"), existing, now=150.0)
        transport = ChromeUiaProbeWindowTransport(chrome_executable=CHROME, enabled=True)
        with patch.object(
            transport,
            "_close",
            return_value={
                "closed": False,
                "absent": False,
                "ambiguous": True,
                "detail": "mismatch",
            },
        ):
            out = transport.execute(plan, existing=existing)
        self.assertFalse(out.changed)
        self.assertFalse(out.side_effect_possible)
        popen.assert_not_called()

    @patch("cws.page_runtime.os.name", "nt")
    @patch("cws.page_runtime.subprocess.Popen")
    @patch("cws.page_runtime.time.sleep")
    def test_rotate_does_not_open_until_old_tagged_window_is_absent(self, _sleep, popen):
        existing = slot()
        plan = plan_probe_slot(worker(URL2, worker_id="w2"), existing, now=150.0)
        transport = ChromeUiaProbeWindowTransport(
            chrome_executable=CHROME,
            enabled=True,
            open_timeout_s=0.01,
        )
        with (
            patch.object(
                transport,
                "_close",
                return_value={
                    "closed": True,
                    "absent": False,
                    "ambiguous": False,
                    "detail": "close requested",
                },
            ),
            patch.object(
                transport,
                "_find",
                return_value={
                    "count": 1,
                    "matches": [{"window_handle": 123, "browser_pid": 456}],
                },
            ),
        ):
            out = transport.execute(plan, existing=existing)
        self.assertFalse(out.changed)
        self.assertTrue(out.side_effect_possible)
        self.assertIn("absence was not proven", out.detail)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
