import unittest
from unittest.mock import patch

from lws.models import ProbeMutationState, ProbeWindowSlotBinding, WorkerRecord, WorkerStatus
from lws.page_runtime import (
    _CLOSE_SCRIPT,
    _FIND_SCRIPT,
    ChromeUiaProbeWindowTransport,
    DisabledProbeWindowTransport,
    ProbeSlotAction,
    ProbeWindowTransportDisabled,
    plan_probe_slot,
    probe_operation_from_plan,
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
        "windows_uia_lws_probe",
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
        self.assertTrue(actual.endswith("#lws-probe=probe:default:abc"))
        self.assertTrue(slot_owns_actual_url(slot(owner="abc")))

    def test_disabled_transport_never_mutates(self):
        plan = plan_probe_slot(worker(), None, now=150.0)
        with self.assertRaises(ProbeWindowTransportDisabled):
            DisabledProbeWindowTransport().execute(plan, existing=None)

    @patch("lws.page_runtime.os.name", "nt")
    def test_reuse_blocks_if_executable_identity_changed(self):
        existing = slot()
        plan = plan_probe_slot(worker(), existing, now=150.0)
        transport = ChromeUiaProbeWindowTransport(chrome_executable=CHROME, enabled=True)
        with patch.object(
            transport,
            "_find",
            return_value={
                "count": 1,
                "matches": [{
                    "window_handle": existing.window_handle,
                    "browser_pid": existing.browser_pid,
                    "chrome_executable": r"C:\Other\chrome.exe",
                    "actual_url": existing.actual_url,
                }],
            },
        ):
            out = transport.execute(plan, existing=existing)
        self.assertFalse(out.changed)
        self.assertFalse(out.side_effect_possible)
        self.assertIn("identity changed", out.detail)

    @patch("lws.page_runtime.os.name", "nt")
    @patch("lws.page_runtime.subprocess.Popen")
    def test_open_transport_binds_unique_tagged_window(self, popen):
        target = worker()
        plan = plan_probe_slot(target, None, now=150.0)
        operation = probe_operation_from_plan(
            target,
            plan,
            None,
            chrome_executable=CHROME,
            now=150.0,
        )
        operation.state = ProbeMutationState.OPEN_SUBMITTED
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
                "matches": [{
                    "window_handle": 99,
                    "browser_pid": 77,
                    "chrome_executable": CHROME,
                    "actual_url": operation.expected_actual_url,
                }],
            },
        ):
            out = transport.open_authorized(operation)
        self.assertTrue(out.changed)
        self.assertEqual(out.binding.target_worker_id, "w1")
        self.assertEqual(out.binding.window_handle, 99)
        popen.assert_called_once()

    @patch("lws.page_runtime.os.name", "nt")
    @patch("lws.page_runtime.subprocess.Popen")
    def test_rotate_refuses_to_open_if_exact_close_is_ambiguous(self, popen):
        existing = slot()
        target = worker(URL2, worker_id="w2")
        plan = plan_probe_slot(target, existing, now=150.0)
        operation = probe_operation_from_plan(
            target, plan, existing, chrome_executable=CHROME, now=150.0
        )
        operation.state = ProbeMutationState.CLOSE_SUBMITTED
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
            out = transport.close_authorized(operation)
        self.assertFalse(out.changed)
        self.assertFalse(out.side_effect_possible)
        popen.assert_not_called()

    @patch("lws.page_runtime.os.name", "nt")
    @patch("lws.page_runtime.subprocess.Popen")
    def test_rotate_does_not_open_without_ready_to_open_authority(self, popen):
        existing = slot()
        target = worker(URL2, worker_id="w2")
        plan = plan_probe_slot(target, existing, now=150.0)
        operation = probe_operation_from_plan(
            target, plan, existing, chrome_executable=CHROME, now=150.0
        )
        operation.state = ProbeMutationState.CLOSE_SUBMITTED
        transport = ChromeUiaProbeWindowTransport(
            chrome_executable=CHROME,
            enabled=True,
            open_timeout_s=0.01,
        )
        out = transport.open_authorized(operation)
        self.assertFalse(out.changed)
        self.assertFalse(out.side_effect_possible)
        self.assertIn("OPEN authority was not durably submitted", out.detail)
        popen.assert_not_called()

    def test_powershell_helpers_do_not_assign_read_only_pid_automatic_variable(self):
        for script in (_FIND_SCRIPT, _CLOSE_SCRIPT):
            self.assertNotRegex(script, r"(?im)^\s*\$pid\s*=")
        self.assertIn("$browserPid=", _FIND_SCRIPT)


if __name__ == "__main__":
    unittest.main()
