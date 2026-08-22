import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lws.registry import Registry
from lws.watchdog_host import (
    build_watchdog_command,
    inspect_watchdog_host,
    launch_detached_watchdog,
    pid_exists,
    stop_watchdog_host,
    watchdog_creationflags,
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
            db_path="C:/repo/.lws/registry.sqlite3",
            interval_s=30,
            use_uia=True,
            lsm_state_dir="C:/lsm/state",
            git_bin="C:/git.exe",
        )
        self.assertEqual(command[:3], ["python", "-m", "lws"])
        self.assertIn("watch", command)
        self.assertIn("--uia", command)
        self.assertNotIn("job_start", " ".join(command))

    def test_timeout_autorecovery_command_is_explicit_and_forces_uia(self):
        command = build_watchdog_command(
            python_executable="python",
            db_path="C:/repo/.lws/registry.sqlite3",
            interval_s=30,
            use_uia=False,
            auto_recover_timeouts=True,
        )
        self.assertIn("--uia", command)
        self.assertIn("--auto-recover-timeouts", command)

    def test_windows_watchdog_creationflags_include_no_console_window(self):
        flags = watchdog_creationflags("nt")
        self.assertTrue(flags & 0x00000008)  # DETACHED_PROCESS
        self.assertTrue(flags & 0x00000200)  # CREATE_NEW_PROCESS_GROUP
        self.assertTrue(flags & 0x08000000)  # CREATE_NO_WINDOW

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
        self.assertEqual(status.lease_state, "stop_fenced")
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

    def test_status_distinguishes_fresh_dead_stale_dead_and_stale_alive(self):
        now = time.time()
        acquired, _ = self.registry.acquire_watchdog_lease(
            name="default",
            owner_id="owner-a",
            pid=424242,
            host="host-a",
            ttl_s=60,
            now=now,
        )
        self.assertTrue(acquired)
        with patch("lws.watchdog_host.pid_exists", return_value=False):
            fresh_dead = inspect_watchdog_host(self.registry, now=now + 1)
            stale_dead = inspect_watchdog_host(self.registry, now=now + 61)
        self.assertEqual(fresh_dead.lease_state, "fresh_dead")
        self.assertTrue(fresh_dead.fresh)
        self.assertEqual(stale_dead.lease_state, "stale_dead")
        self.assertFalse(stale_dead.fresh)

        with patch("lws.watchdog_host.pid_exists", return_value=True):
            stale_alive = inspect_watchdog_host(self.registry, now=now + 61)
        self.assertEqual(stale_alive.lease_state, "stale_alive")
        self.assertIn("replacement will fence", stale_alive.detail)

    def test_stop_clears_only_exact_stop_fence_after_recorded_pid_is_dead(self):
        now = time.time()
        acquired, _ = self.registry.acquire_watchdog_lease(
            name="default",
            owner_id="owner-a",
            pid=424242,
            host="host-a",
            ttl_s=60,
            now=now,
        )
        self.assertTrue(acquired)
        with patch("lws.watchdog_host.pid_exists", return_value=False):
            result = stop_watchdog_host(self.registry, grace_s=60, wait_s=0)
        self.assertTrue(result.requested)
        self.assertTrue(result.stopped)
        self.assertTrue(result.stop_lease_cleared)
        self.assertIsNone(self.registry.watchdog_lease("default"))

    def test_detached_launch_redirects_handles_and_logs_beside_registry(self):
        fake_proc = SimpleNamespace(pid=321, poll=lambda: None, returncode=None)
        owned_lease = {
            "owner_id": "host:fixed",
            "pid": 321,
            "host": "fixture",
            "heartbeat_at": time.time(),
            "expires_at": time.time() + 60,
        }
        with (
            patch.object(self.registry, "watchdog_lease", side_effect=[None, owned_lease]),
            patch("lws.watchdog_host.uuid.uuid4", return_value=SimpleNamespace(hex="fixed")),
            patch("lws.watchdog_host.subprocess.Popen", return_value=fake_proc) as popen,
            patch("lws.watchdog_host.time.sleep", return_value=None),
        ):
            result = launch_detached_watchdog(
                self.registry,
                repo_root=Path(self.tmp.name),
                db_path=self.db,
                ready_timeout_s=1,
            )

        self.assertTrue(result.lease_ready)
        self.assertEqual(Path(result.log_path), self.db.parent / "watchdog.log")
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], __import__("subprocess").DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        if os.name == "nt":
            self.assertFalse(kwargs["start_new_session"])
            self.assertTrue(kwargs["creationflags"] & 0x00000008)
            self.assertTrue(kwargs["creationflags"] & 0x00000200)
            self.assertTrue(kwargs["creationflags"] & 0x08000000)
        else:
            self.assertTrue(kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
