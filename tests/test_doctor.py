import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cws.doctor import DoctorStatus, run_doctor
from cws.models import ProbeMutationKind, ProbeMutationState
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
                self.assertIn("gated", checks["recovery.transport"].detail)
                self.assertEqual(checks["probe.mutation"].status, DoctorStatus.PASS)
                self.assertIn("no unresolved", checks["probe.mutation"].detail)
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

    def test_unresolved_probe_mutation_is_visible_as_warning(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            operation = SimpleNamespace(
                operation_id="probe-op-1",
                kind=ProbeMutationKind.ROTATE,
                state=ProbeMutationState.RECONCILE_REQUIRED,
            )
            try:
                with (
                    patch("cws.doctor.detect_lsm_state_dir", return_value=None),
                    patch("cws.doctor.observe_system_memory", return_value=system_memory()),
                    patch("cws.doctor.observe_windows_process_group", return_value=chrome_memory()),
                    patch.object(
                        registry,
                        "unresolved_probe_mutation_operation",
                        return_value=operation,
                    ),
                ):
                    report = run_doctor(registry)
                checks = {check.name: check for check in report.checks}
                self.assertEqual(checks["probe.mutation"].status, DoctorStatus.WARN)
                self.assertIn("RECONCILE_REQUIRED", checks["probe.mutation"].detail)
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
                checks = {check.name: check for check in report.checks}
                self.assertEqual(checks["task.window_binding"].status, DoctorStatus.PASS)
                self.assertIn("no exact-window lease", checks["task.window_binding"].detail)
            finally:
                registry.close()

    def test_stale_window_binding_is_a_warning(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            work = Path(td) / "work"
            work.mkdir()
            try:
                task = registry.register_task(
                    task_id="t1",
                    project="p",
                    objective="obj",
                    cwd=str(work),
                    conversation_url="https://chatgpt.com/c/fixture",
                )
                worker = registry.get_worker(task.current_worker_id)
                registry.bind_worker_window(
                    worker.worker_id,
                    window_handle=123,
                    browser_pid=456,
                    chrome_executable=r"C:\Chrome\chrome.exe",
                    conversation_url=worker.conversation_url,
                    observed_at=900.0,
                    ttl_s=10.0,
                )
                with (
                    patch("cws.doctor.detect_lsm_state_dir", return_value=None),
                    patch("cws.doctor.observe_system_memory", return_value=system_memory()),
                    patch("cws.doctor.observe_windows_process_group", return_value=chrome_memory()),
                    patch("cws.doctor.time.time", return_value=NOW),
                ):
                    report = run_doctor(registry, task_id="t1", probe_uia=False)
                checks = {check.name: check for check in report.checks}
                self.assertEqual(checks["task.window_binding"].status, DoctorStatus.WARN)
                self.assertIn("stale", checks["task.window_binding"].detail)
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
