import tempfile
import unittest
from pathlib import Path

from lws.models import BrowserObservation, WorkerStatus
from lws.registry import Registry


class RegistryTests(unittest.TestCase):
    def test_task_worker_job_and_observation_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "lws.sqlite3")
            try:
                task = registry.register_task(
                    task_id="t1",
                    project="demo",
                    objective="objective",
                    cwd="C:/repo",
                    lsm_session_id="s1",
                    conversation_url="https://web.example/c/abc",
                )
                self.assertEqual(task.current_worker_id is not None, True)
                parked = registry.set_worker_status(task.current_worker_id, WorkerStatus.PARKED)
                self.assertEqual(parked.status, WorkerStatus.PARKED)
                self.assertIsNone(parked.ended_at)
                active = registry.set_worker_status(task.current_worker_id, WorkerStatus.ACTIVE)
                self.assertEqual(active.status, WorkerStatus.ACTIVE)
                registry.track_job("t1", "job_1")
                self.assertEqual(registry.tracked_jobs("t1"), ["job_1"])
                obs = BrowserObservation(
                    worker_id=task.current_worker_id,
                    observed_at=123.0,
                    send_button_ready=True,
                )
                registry.record_browser_observation(obs)
                loaded = registry.latest_browser_observation(task.current_worker_id)
                self.assertEqual(loaded.observed_at, 123.0)
                self.assertTrue(loaded.send_button_ready)
                registry.set_checkpoint("t1", {"git_head": "abc123", "step": "after commit"})
                self.assertEqual(registry.get_task("t1").checkpoint["git_head"], "abc123")
                registry.record_recovery_event(
                    "t1",
                    action="observe",
                    safe_to_dispatch=False,
                    reason="still active",
                    payload={"evidence": ["job running"]},
                )
                history = registry.recovery_history("t1")
                self.assertEqual(history[0]["action"], "observe")

                acquired, first = registry.acquire_watchdog_lease(
                    name="default",
                    owner_id="one",
                    pid=111,
                    host="host-a",
                    ttl_s=30,
                    now=100,
                )
                self.assertTrue(acquired)
                self.assertEqual(first["pid"], 111)
                acquired, holder = registry.acquire_watchdog_lease(
                    name="default",
                    owner_id="two",
                    pid=222,
                    host="host-b",
                    ttl_s=30,
                    now=110,
                )
                self.assertFalse(acquired)
                self.assertEqual(holder["owner_id"], "one")
                self.assertTrue(
                    registry.heartbeat_watchdog_lease(
                        name="default", owner_id="one", ttl_s=30, now=115
                    )
                )
                acquired, _ = registry.acquire_watchdog_lease(
                    name="default",
                    owner_id="two",
                    pid=222,
                    host="host-b",
                    ttl_s=30,
                    now=146,
                )
                self.assertTrue(acquired)
                self.assertFalse(
                    registry.release_watchdog_lease(name="default", owner_id="one")
                )
                self.assertTrue(
                    registry.release_watchdog_lease(name="default", owner_id="two")
                )
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
