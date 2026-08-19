import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cws.cli import _refresh_uia
from cws.db import SCHEMA_VERSION
from cws.models import BrowserObservation, WorkerStatus
from cws.registry import Registry


class DummyProbe:
    chrome_executable = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    timeout_s = 8.0

    def observe(self, worker, *, previous=None, expected_hwnd=None):
        return BrowserObservation(
            worker_id=worker.worker_id,
            observed_at=100.0,
            url=worker.conversation_url,
            generating=False,
            raw={
                "source": "windows_uia_chrome",
                "browser_pid": 4244,
                "window_handle": 123456,
            },
        )


class WindowBindingTests(unittest.TestCase):
    def make_registry(self, td):
        reg = Registry(Path(td) / "registry.sqlite3")
        task = reg.register_task(
            task_id="t1",
            project="p",
            objective="o",
            cwd=td,
            conversation_url="https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        return reg, task

    def test_bind_fresh_and_expired(self):
        with tempfile.TemporaryDirectory() as td:
            reg, task = self.make_registry(td)
            try:
                worker = reg.get_worker(task.current_worker_id)
                binding = reg.bind_worker_window(
                    worker.worker_id,
                    window_handle=123,
                    browser_pid=456,
                    chrome_executable=r"C:\Chrome\chrome.exe",
                    conversation_url=worker.conversation_url,
                    observed_at=100.0,
                    ttl_s=10.0,
                )
                self.assertTrue(binding.is_fresh(now=109.9))
                self.assertFalse(binding.is_fresh(now=110.0))
                self.assertIsNotNone(
                    reg.get_worker_window_binding(worker.worker_id, now=109.9, require_fresh=True)
                )
                self.assertIsNone(
                    reg.get_worker_window_binding(worker.worker_id, now=110.0, require_fresh=True)
                )
            finally:
                reg.close()

    def test_supersede_and_park_clear_binding(self):
        with tempfile.TemporaryDirectory() as td:
            reg, task = self.make_registry(td)
            try:
                first = reg.get_worker(task.current_worker_id)
                reg.bind_worker_window(
                    first.worker_id,
                    window_handle=123,
                    browser_pid=456,
                    chrome_executable=r"C:\Chrome\chrome.exe",
                    conversation_url=first.conversation_url,
                )
                second = reg.add_worker(
                    "t1",
                    "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444",
                )
                self.assertIsNone(reg.get_worker_window_binding(first.worker_id))
                reg.bind_worker_window(
                    second.worker_id,
                    window_handle=789,
                    browser_pid=456,
                    chrome_executable=r"C:\Chrome\chrome.exe",
                    conversation_url=second.conversation_url,
                )
                reg.set_worker_status(second.worker_id, WorkerStatus.PARKED)
                self.assertIsNone(reg.get_worker_window_binding(second.worker_id))
            finally:
                reg.close()

    def test_refresh_uia_records_short_lived_binding(self):
        with tempfile.TemporaryDirectory() as td:
            reg, task = self.make_registry(td)
            try:
                obs = _refresh_uia(reg, task.task_id, DummyProbe())
                binding = reg.get_worker_window_binding(task.current_worker_id)
                self.assertIsNotNone(binding)
                self.assertEqual(binding.window_handle, 123456)
                self.assertEqual(binding.browser_pid, 4244)
                self.assertEqual(binding.observed_at, obs.observed_at)
                self.assertEqual(binding.expires_at, 132.0)
            finally:
                reg.close()

    def test_refresh_uia_reuses_fresh_exact_hwnd(self):
        with tempfile.TemporaryDirectory() as td:
            reg, task = self.make_registry(td)
            try:
                worker = reg.get_worker(task.current_worker_id)
                reg.bind_worker_window(
                    worker.worker_id,
                    window_handle=654321,
                    browser_pid=4244,
                    chrome_executable=DummyProbe.chrome_executable,
                    conversation_url=worker.conversation_url,
                    observed_at=100.0,
                    ttl_s=30.0,
                )

                class CapturingProbe(DummyProbe):
                    def __init__(self):
                        self.expected_hwnd = None

                    def observe(self, worker, *, previous=None, expected_hwnd=None):
                        self.expected_hwnd = expected_hwnd
                        obs = super().observe(
                            worker, previous=previous, expected_hwnd=expected_hwnd
                        )
                        obs.raw["window_handle"] = expected_hwnd
                        obs.observed_at = 110.0
                        return obs

                probe = CapturingProbe()
                with patch("cws.registry.time.time", return_value=110.0):
                    _refresh_uia(reg, task.task_id, probe)
                self.assertEqual(probe.expected_hwnd, 654321)
                refreshed = reg.get_worker_window_binding(worker.worker_id)
                self.assertEqual(refreshed.window_handle, 654321)
            finally:
                reg.close()

    def test_v2_registry_migrates_additively_to_v3(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.sqlite3"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                PRAGMA user_version = 2;
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lsm_session_id TEXT,
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    current_worker_id TEXT,
                    recovery_attempts INTEGER NOT NULL DEFAULT 0,
                    max_recovery_attempts INTEGER NOT NULL DEFAULT 3,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE workers (
                    worker_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    conversation_url TEXT NOT NULL,
                    conversation_id TEXT,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    last_seen_at REAL,
                    ended_at REAL
                );
                INSERT INTO tasks
                  (task_id, project, objective, cwd, state, checkpoint_json,
                   current_worker_id, created_at, updated_at)
                VALUES ('legacy', 'p', 'o', '.', 'QUEUED', '{}', 'wlegacy', 1, 1);
                INSERT INTO workers
                  (worker_id, task_id, conversation_url, conversation_id, status, started_at)
                VALUES ('wlegacy', 'legacy', 'https://chatgpt.com/c/legacy', 'legacy', 'active', 1);
                """
            )
            conn.commit()
            conn.close()

            reg = Registry(db)
            try:
                self.assertEqual(reg.get_task("legacy").current_worker_id, "wlegacy")
                self.assertEqual(reg.get_worker("wlegacy").status, WorkerStatus.ACTIVE)
                version = reg._conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, SCHEMA_VERSION)
                table = reg._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='worker_window_bindings'"
                ).fetchone()
                self.assertIsNotNone(table)
            finally:
                reg.close()


if __name__ == "__main__":
    unittest.main()
