import os
import tempfile
import time
import unittest
from pathlib import Path

from cws.registry import Registry
from cws.watchdog_host import (
    build_watchdog_command,
    inspect_watchdog_host,
    launch_detached_watchdog,
    pid_exists,
)


class WatchdogHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "registry.sqlite3"
        self.registry = Registry(self.db)

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def test_current_pid_is_observable(self):
        self.assertTrue(pid_exists(os.getpid()))

    def test_command_is_repo_python_module_not_lsm_job(self):
        command = build_watchdog_command(
            python_executable="python",
            db_path="C:/repo/.cws/registry.sqlite3",
            interval_s=30,
            use_uia=True,
            lsm_state_dir="C:/lsm/state",
            git_bin="C:/git.exe",
        )
        self.assertEqual(command[:3], ["python", "-m", "cws"])
        self.assertIn("watch", command)
        self.assertIn("--uia", command)
        self.assertNotIn("job_start", " ".join(command))

    def test_timeout_autorecovery_command_is_explicit_and_forces_uia(self):
        command = build_watchdog_command(
            python_executable="python",
            db_path="C:/repo/.cws/registry.sqlite3",
            interval_s=30,
            use_uia=False,
            auto_recover_timeouts=True,
        )
        self.assertIn("--uia", command)
        self.assertIn("--auto-recover-timeouts", command)

    def test_cooperative_stop_fences_old_and_new_owner(self):
        now = time.time()
        acquired, lease = self.registry.acquire_watchdog_lease(
            name="default",
            owner_id="owner-a",
            pid=os.getpid(),
            host="host-a",
            ttl_s=60,
            now=now,
        )
        self.assertTrue(acquired)
        requested = self.registry.request_watchdog_stop(
            name="default", grace_s=60, now=now + 1
        )
        self.assertIsNotNone(requested)
        self.assertTrue(requested["owner_id"].startswith("stop:"))
        self.assertFalse(
            self.registry.heartbeat_watchdog_lease(
                name="default", owner_id="owner-a", ttl_s=60, now=now + 2
            )
        )
        second, holder = self.registry.acquire_watchdog_lease(
            name="default",
            owner_id="owner-b",
            pid=os.getpid(),
            host="host-b",
            ttl_s=60,
            now=now + 2,
        )
        self.assertFalse(second)
        self.assertTrue(holder["owner_id"].startswith("stop:"))
        self.assertTrue(
            self.registry.clear_watchdog_stop(
                name="default", stop_owner_id=requested["owner_id"]
            )
        )
        second, _ = self.registry.acquire_watchdog_lease(
            name="default",
            owner_id="owner-b",
            pid=os.getpid(),
            host="host-b",
            ttl_s=60,
            now=now + 3,
        )
        self.assertTrue(second)

    def test_status_distinguishes_stop_request(self):
        now = time.time()
        self.registry.acquire_watchdog_lease(
            name="default",
            owner_id="owner-a",
            pid=os.getpid(),
            host="host-a",
            ttl_s=60,
            now=now,
        )
        requested = self.registry.request_watchdog_stop(
            name="default", grace_s=60, now=now + 1
        )
        status = inspect_watchdog_host(self.registry, now=now + 2)
        self.assertTrue(status.stop_requested)
        self.assertTrue(status.fresh)
        self.assertEqual(status.pid, os.getpid())
        self.assertIn("stop requested", status.detail)
        self.registry.clear_watchdog_stop(
            name="default", stop_owner_id=requested["owner_id"]
        )

    def test_detached_start_refuses_fresh_existing_lease_before_spawn(self):
        now = time.time()
        self.registry.acquire_watchdog_lease(
            name="default",
            owner_id="owner-a",
            pid=os.getpid(),
            host="host-a",
            ttl_s=60,
            now=now,
        )
        with self.assertRaises(RuntimeError):
            launch_detached_watchdog(
                self.registry,
                repo_root=Path(self.tmp.name),
                db_path=self.db,
            )


if __name__ == "__main__":
    unittest.main()
