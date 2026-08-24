from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lws import cli
from lws.lsm import UnsupportedLsmState
from lws.models import Assessment, SupervisorState, TaskRecord
from lws.overrun_continuation import OverrunContinuationPolicy
from lws.watcher import WatchPolicy


class _ScanRegistry:
    def __init__(self) -> None:
        self.tasks = [
            TaskRecord("bad", "p", "bad", ".", SupervisorState.RUNNING),
            TaskRecord("good", "p", "good", ".", SupervisorState.RUNNING),
        ]
        self.events: list[tuple[str, str, str]] = []

    def list_tasks(self):
        return list(self.tasks)

    def record_recovery_event(self, task_id, *, action, safe_to_dispatch, reason, payload):
        self.events.append((task_id, action, reason))


class _OverrunRegistry:
    def __init__(self, task: TaskRecord) -> None:
        self.task = task
        self.events: list[tuple[str, str, str]] = []

    def unresolved_action_attempt(self, task_id):
        return None

    def latest_reconciliation(self, task_id):
        return None

    def record_recovery_event(self, task_id, *, action, safe_to_dispatch, reason, payload):
        self.events.append((task_id, action, reason))


class WatchdogResilienceTests(unittest.TestCase):
    def test_scan_isolates_one_task_runtime_error_and_keeps_scanning(self):
        registry = _ScanRegistry()
        keepalives: list[int] = []

        def fake_assessment(_registry, task_id, *_args, **_kwargs):
            if task_id == "bad":
                raise ValueError("window binding requires an active worker")
            task = next(item for item in registry.tasks if item.task_id == task_id)
            return (
                task,
                None,
                None,
                None,
                Assessment(SupervisorState.SUSPECT, "good task still assessed", "high"),
            )

        with patch.object(cli, "_assessment", side_effect=fake_assessment):
            queue = cli._scan_attention(
                registry,
                adapter=object(),
                workspace_probe=object(),
                policy=WatchPolicy(),
                keepalive=lambda: keepalives.append(1),
            )

        self.assertEqual([item.task_id for item in queue], ["bad", "good"])
        self.assertEqual(queue[0].state, SupervisorState.NEEDS_HUMAN)
        self.assertIn("ValueError", queue[0].reason)
        self.assertEqual(queue[1].state, SupervisorState.SUSPECT)
        self.assertGreaterEqual(len(keepalives), 4)
        self.assertTrue(any(action == "watchdog_scan_error" for _, action, _ in registry.events))

    def test_scan_still_fails_closed_on_unknown_lsm_schema(self):
        registry = _ScanRegistry()
        with patch.object(cli, "_assessment", side_effect=UnsupportedLsmState("future schema")):
            with self.assertRaises(UnsupportedLsmState):
                cli._scan_attention(
                    registry,
                    adapter=object(),
                    workspace_probe=object(),
                    policy=WatchPolicy(),
                )

    def test_overrun_reconcile_error_is_task_local(self):
        task = TaskRecord(
            "bad-overrun",
            "p",
            "o",
            ".",
            SupervisorState.RUNNING,
            current_worker_id="worker-bad",
            created_at=1.0,
            updated_at=1.0,
        )
        registry = _OverrunRegistry(task)
        clock = SimpleNamespace(
            sample_due=True,
            due=True,
            elapsed_s=2000.0,
            anchor_at=1.0,
            due_at=1521.0,
        )
        keepalives: list[int] = []

        with (
            patch.object(cli, "ChromeUiaProbe", return_value=object()),
            patch.object(cli, "_canonical_overrun_tasks", return_value=[task]),
            patch.object(cli, "overrun_clock", return_value=clock),
            patch.object(cli, "_refresh_uia", return_value=None),
            patch.object(cli, "_assessment", side_effect=ValueError("inactive binding")),
        ):
            results = cli._auto_continue_overrun_cycle(
                registry,
                adapter=object(),
                workspace_probe=object(),
                policy=OverrunContinuationPolicy(enabled=True, overrun_after_s=1520),
                now=2001.0,
                keepalive=lambda: keepalives.append(1),
            )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["submitted"])
        self.assertIn("ValueError", results[0]["detail"])
        self.assertTrue(any(action == "watchdog_overrun_error" for _, action, _ in registry.events))
        self.assertGreaterEqual(len(keepalives), 2)


if __name__ == "__main__":
    unittest.main()
