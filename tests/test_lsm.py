import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lws.lsm import FileLsmTelemetry


class LsmTelemetryTests(unittest.TestCase):
    def test_reads_session_plan_and_tracked_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            session = {
                "version": 1,
                "session_id": "s1",
                "status": "active",
                "active_run_id": "r1",
                "plan": {
                    "plan_id": "p1",
                    "status": "active",
                    "last_agent_activity": 9000,
                    "execution_lease_s": 900,
                    "continuation_pending": False,
                    "steps": [
                        {"id": "a", "status": "completed"},
                        {"id": "b", "status": "active"},
                    ],
                },
                "in_flight_calls": {
                    "c1": {"started_at": 9950, "heartbeat_at": 9950, "run_id": "r1"}
                },
                "activity": [{"ts": 9950, "type": "tool.started"}],
            }
            (root / "sessions" / "s1.json").write_text(json.dumps(session), encoding="utf-8")
            (root / "jobs.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "jobs": [
                            {"job_id": "j1", "status": "running"},
                            {"job_id": "j2", "status": "failed"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("lws.lsm.time.time", return_value=10_000):
                obs = FileLsmTelemetry(root).observe(
                    task_id="t1", session_id="s1", tracked_job_ids=["j1", "j2"]
                )
            self.assertEqual(obs.in_flight_calls, 1)
            self.assertEqual(obs.active_jobs, 1)
            self.assertEqual(obs.failed_jobs, 1)
            self.assertEqual(obs.completed_steps, 1)
            self.assertEqual(obs.total_steps, 2)
            self.assertTrue(obs.continuation_due)

    def test_rejects_unknown_session_schema(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            (root / "sessions" / "s1.json").write_text(
                json.dumps({"version": 999, "session_id": "s1"}), encoding="utf-8"
            )
            with self.assertRaises(RuntimeError):
                FileLsmTelemetry(root).session_payload("s1")

    def test_terminal_attempt_status_overrides_stale_running_store(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            status_path = root / "job.status.json"
            status_path.write_text(
                json.dumps({"exit_code": 0, "completed_at": 9999.0, "error": None}),
                encoding="utf-8",
            )
            (root / "sessions" / "s1.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_id": "s1",
                        "status": "active",
                        "active_run_id": "r1",
                        "plan": {"status": "active", "last_agent_activity": 9900, "steps": []},
                        "in_flight_calls": {},
                        "activity": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "jobs.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "jobs": [
                            {
                                "job_id": "j1",
                                "status": "running",
                                "status_path": str(status_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("lws.lsm.time.time", return_value=10_000):
                obs = FileLsmTelemetry(root).observe(
                    task_id="t1", session_id="s1", tracked_job_ids=["j1"]
                )
            self.assertEqual(obs.active_jobs, 0)
            self.assertEqual(obs.succeeded_jobs, 1)
            self.assertEqual(obs.raw["tracked_jobs"][0]["status_source"], "attempt_status")


if __name__ == "__main__":
    unittest.main()
