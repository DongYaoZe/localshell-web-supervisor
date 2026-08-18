import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cws.doctor import DoctorStatus, run_doctor
from cws.ram import BrowserMemoryObservation, SystemMemoryObservation
from cws.registry import Registry


NOW = 1000.0


def system_memory():
    return SystemMemoryObservation(
        observed_at=NOW,
        total_bytes=8 * 1024**3,
        available_bytes=3 * 1024**3,
        used_bytes=5 * 1024**3,
        used_fraction=5 / 8,
    )


def chrome_memory():
    return BrowserMemoryObservation(
        observed_at=NOW,
        process_name="chrome",
        process_count=10,
        total_working_set_bytes=1024**3,
        largest_working_set_bytes=256 * 1024**2,
        window_process_count=1,
        pids=[1, 2],
    )


class DoctorTests(unittest.TestCase):
    def test_missing_optional_runtime_conditions_are_warnings_not_failures(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            try:
                with (
                    patch("cws.doctor.detect_lsm_state_dir", return_value=None),
                    patch("cws.doctor.observe_system_memory", return_value=system_memory()),
                    patch("cws.doctor.observe_windows_process_group", return_value=chrome_memory()),
                    patch("cws.doctor.importlib.util.find_spec", return_value=None),
                ):
                    report = run_doctor(registry)
                self.assertEqual(report.overall, DoctorStatus.WARN)
                checks = {check.name: check for check in report.checks}
                self.assertEqual(checks["lsm.state"].status, DoctorStatus.WARN)
                self.assertEqual(checks["playwright.optional"].status, DoctorStatus.WARN)
                self.assertEqual(checks["watchdog.lease"].status, DoctorStatus.WARN)
                self.assertEqual(checks["recovery.transport"].status, DoctorStatus.PASS)
                self.assertIn("disabled", checks["recovery.transport"].detail)
            finally:
                registry.close()

    def test_unknown_requested_task_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            try:
                with (
                    patch("cws.doctor.detect_lsm_state_dir", return_value=None),
                    patch("cws.doctor.observe_system_memory", return_value=system_memory()),
                    patch("cws.doctor.observe_windows_process_group", return_value=chrome_memory()),
                ):
                    report = run_doctor(registry, task_id="missing")
                self.assertEqual(report.overall, DoctorStatus.FAIL)
                task_check = next(check for check in report.checks if check.name == "task")
                self.assertEqual(task_check.status, DoctorStatus.FAIL)
            finally:
                registry.close()

    def test_task_check_does_not_invoke_uia_unless_explicitly_requested(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            work = Path(td) / "work"
            work.mkdir()
            try:
                registry.register_task(
                    task_id="t1",
                    project="p",
                    objective="obj",
                    cwd=str(work),
                    conversation_url="https://chatgpt.com/c/fixture",
                )
                with (
                    patch("cws.doctor.detect_lsm_state_dir", return_value=None),
                    patch("cws.doctor.observe_system_memory", return_value=system_memory()),
                    patch("cws.doctor.observe_windows_process_group", return_value=chrome_memory()),
                    patch("cws.doctor.ChromeUiaProbe.observe") as observe,
                ):
                    report = run_doctor(registry, task_id="t1", probe_uia=False)
                observe.assert_not_called()
                self.assertNotEqual(report.overall, DoctorStatus.FAIL)
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
