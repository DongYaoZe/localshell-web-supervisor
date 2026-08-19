import sqlite3
import tempfile
import unittest
from pathlib import Path

from cws.db import SCHEMA_VERSION
from cws.models import WorkerStatus
from cws.page_runtime import tagged_probe_url
from cws.registry import Registry

URL = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class ProbeSlotRegistryTests(unittest.TestCase):
    def test_active_worker_cannot_be_bound_as_probe_slot(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "r.sqlite3")
            try:
                task = registry.register_task(
                    task_id="t",
                    project="p",
                    objective="o",
                    cwd=td,
                    conversation_url=URL,
                )
                worker = registry.get_worker(task.current_worker_id)
                actual = tagged_probe_url(
                    URL,
                    slot_id="probe:default",
                    owner_token="owner",
                )
                with self.assertRaisesRegex(ValueError, "parked worker"):
                    registry.bind_probe_window_slot(
                        "probe:default",
                        owner_token="owner",
                        target_worker_id=worker.worker_id,
                        target_conversation_url=URL,
                        actual_url=actual,
                        window_handle=1,
                        browser_pid=2,
                        chrome_executable="chrome.exe",
                    )
            finally:
                registry.close()

    def test_bind_read_clear_probe_slot(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "r.sqlite3")
            try:
                task = registry.register_task(
                    task_id="t",
                    project="p",
                    objective="o",
                    cwd=td,
                    conversation_url=URL,
                )
                worker = registry.get_worker(task.current_worker_id)
                registry.set_worker_status(worker.worker_id, WorkerStatus.PARKED)
                actual = tagged_probe_url(
                    URL,
                    slot_id="probe:default",
                    owner_token="owner",
                )
                binding = registry.bind_probe_window_slot(
                    "probe:default",
                    owner_token="owner",
                    target_worker_id=worker.worker_id,
                    target_conversation_url=URL,
                    actual_url=actual,
                    window_handle=1,
                    browser_pid=2,
                    chrome_executable="chrome.exe",
                    observed_at=100,
                    ttl_s=10,
                )
                self.assertTrue(binding.is_fresh(now=109.9))
                self.assertFalse(binding.is_fresh(now=110.0))
                self.assertEqual(
                    registry.get_probe_window_slot("probe:default").target_worker_id,
                    worker.worker_id,
                )
                self.assertTrue(registry.clear_probe_window_slot("probe:default"))
                self.assertIsNone(registry.get_probe_window_slot("probe:default"))
            finally:
                registry.close()

    def test_second_durable_probe_slot_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "r.sqlite3")
            try:
                first_task = registry.register_task(
                    task_id="t1", project="p", objective="o", cwd=td, conversation_url=URL
                )
                first = registry.get_worker(first_task.current_worker_id)
                registry.set_worker_status(first.worker_id, WorkerStatus.PARKED)
                registry.bind_probe_window_slot(
                    "probe:default",
                    owner_token="owner1",
                    target_worker_id=first.worker_id,
                    target_conversation_url=URL,
                    actual_url=tagged_probe_url(
                        URL, slot_id="probe:default", owner_token="owner1"
                    ),
                    window_handle=1,
                    browser_pid=2,
                    chrome_executable="chrome.exe",
                )
                second_url = "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444"
                second_task = registry.register_task(
                    task_id="t2", project="p", objective="o", cwd=td, conversation_url=second_url
                )
                second = registry.get_worker(second_task.current_worker_id)
                registry.set_worker_status(second.worker_id, WorkerStatus.PARKED)
                with self.assertRaisesRegex(ValueError, "only one durable probe slot"):
                    registry.bind_probe_window_slot(
                        "probe:other",
                        owner_token="owner2",
                        target_worker_id=second.worker_id,
                        target_conversation_url=second_url,
                        actual_url=tagged_probe_url(
                            second_url, slot_id="probe:other", owner_token="owner2"
                        ),
                        window_handle=3,
                        browser_pid=4,
                        chrome_executable="chrome.exe",
                    )
            finally:
                registry.close()

    def test_v3_migrates_additively_to_v4(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA user_version = 3")
            conn.executescript(
                """
                CREATE TABLE tasks(
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
                CREATE TABLE workers(
                    worker_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    conversation_url TEXT NOT NULL,
                    conversation_id TEXT,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    last_seen_at REAL,
                    ended_at REAL
                );
                INSERT INTO tasks VALUES(
                    't','p','o','.', 'QUEUED',NULL,'{}','w',0,3,1,1
                );
                INSERT INTO workers VALUES(
                    'w','t','https://chatgpt.com/c/a',NULL,'active',1,NULL,NULL
                );
                """
            )
            conn.commit()
            conn.close()

            registry = Registry(db)
            try:
                self.assertEqual(
                    registry._conn.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                for name in ("probe_window_slots", "page_capabilities"):
                    self.assertIsNotNone(
                        registry._conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                            (name,),
                        ).fetchone()
                    )
                self.assertEqual(registry.get_task("t").current_worker_id, "w")
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
