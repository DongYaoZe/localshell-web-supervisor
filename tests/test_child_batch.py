import tempfile
import time
import unittest
from pathlib import Path

from lws.child_batch import advance_child_dispatch_batch
from lws.child_spawn import ChildSpawnDeliveryObservation, ChildSpawnExecution, arm_child_spawn, submit_child_spawn_open
from lws.models import ChildSpawnAttemptState, SupervisorState, WorkspaceObservation
from lws.registry import Registry


PROJECT_ID = "0123456789abcdef0123456789abcdef"
PROJECT = f"https://chatgpt.com/g/g-p-{PROJECT_ID}-fixture"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class FakeBatchTransport:
    def __init__(self):
        self.open_calls = 0
        self.reuse_calls = 0
        self.reuse_spawn_calls = 0
        self.send_calls = 0
        self.close_calls = 0
        self.next_hwnd = 100
        self.conversation_calls = 0
        self.ambiguous_child = None

    def open_authorized(self, attempt):
        self.open_calls += 1
        hwnd = self.next_hwnd
        self.next_hwnd += 1
        return ChildSpawnExecution(
            True,
            True,
            "opened",
            window_handle=hwnd,
            browser_pid=200,
            url=attempt.tagged_project_url,
        )

    def reuse_authorized(self, attempt, *, source_worker, source_binding):
        self.reuse_calls += 1
        return ChildSpawnExecution(
            True,
            True,
            "reused",
            window_handle=source_binding.window_handle,
            browser_pid=source_binding.browser_pid,
            url=attempt.tagged_project_url,
        )

    def reuse_completed_spawn_authorized(self, attempt, *, source_attempt):
        self.reuse_spawn_calls += 1
        return ChildSpawnExecution(
            True,
            True,
            "reused completed spawn",
            window_handle=source_attempt.window_handle,
            browser_pid=source_attempt.browser_pid,
            url=attempt.tagged_project_url,
        )

    def send_authorized(self, attempt, prompt):
        self.send_calls += 1
        return ChildSpawnExecution(
            True,
            True,
            "sent",
            window_handle=attempt.window_handle,
            browser_pid=attempt.browser_pid,
            url=attempt.tagged_project_url,
        )

    def wait_for_conversation(self, attempt):
        self.conversation_calls += 1
        if self.ambiguous_child == attempt.child_task_id:
            return ChildSpawnExecution(
                False,
                True,
                "conversation routing remained ambiguous",
                window_handle=attempt.window_handle,
                browser_pid=attempt.browser_pid,
            )
        suffix = f"{self.conversation_calls:012d}"
        url = (
            f"https://chatgpt.com/g/g-p-{attempt.project_id}/c/"
            f"aaaaaaaa-bbbb-cccc-dddd-{suffix}"
        )
        return ChildSpawnExecution(
            True,
            False,
            "conversation",
            window_handle=attempt.window_handle,
            browser_pid=attempt.browser_pid,
            url=url,
        )

    def observe_bound_delivery(self, attempt):
        suffix = f"{self.conversation_calls:012d}"
        url = (
            f"https://chatgpt.com/g/g-p-{attempt.project_id}/c/"
            f"aaaaaaaa-bbbb-cccc-dddd-{suffix}"
        )
        return ChildSpawnDeliveryObservation(url, True, "", False, False, "delivered")

    def wait_for_delivery(self, attempt, prompt):
        return self.observe_bound_delivery(attempt)

    def close_worker_binding_authorized(self, *, worker, binding):
        self.close_calls += 1
        return ChildSpawnExecution(
            True,
            True,
            "exact terminal child window close requested",
            window_handle=binding.window_handle,
            browser_pid=binding.browser_pid,
            url=binding.conversation_url,
        )

    def close_completed_spawn_authorized(self, *, source_attempt):
        self.close_calls += 1
        return ChildSpawnExecution(
            True,
            True,
            "exact terminal completed-spawn window close requested",
            window_handle=source_attempt.window_handle,
            browser_pid=source_attempt.browser_pid,
            url=source_attempt.conversation_url,
        )


class ChildBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "registry.sqlite3"
        self.registry = Registry(self.db)
        self.registry.register_task(
            task_id="parent",
            project="lws",
            objective="parent",
            cwd="D:/repo",
        )
        self.transport = FakeBatchTransport()
        self.child_created_at = 0

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def add_child(self, child_task_id, key):
        self.child_created_at += 1
        return self.registry.create_child_dispatch(
            "parent",
            child_key=key,
            child_task_id=child_task_id,
            project="lws",
            objective=child_task_id,
            cwd=f"D:/repo-{child_task_id}",
            prompt_text=f"work on {child_task_id}",
            web_project_url=PROJECT,
            now=float(self.child_created_at),
        )

    def workspace(self, child_task_id):
        task = self.registry.get_task(child_task_id)
        return WorkspaceObservation(
            task_id=child_task_id,
            observed_at=time.time(),
            cwd=task.cwd,
            cwd_exists=True,
            is_git_repo=False,
        )

    def factory(self, chrome_executable):
        self.assertEqual(chrome_executable, CHROME)
        return self.transport

    def test_pool_waits_for_lsm_binding_then_reuses_same_exact_window(self):
        self.add_child("child-a", "A")
        self.add_child("child-b", "B")

        first = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=1,
            chrome_executable=CHROME,
            workspace_loader=self.workspace,
            transport_factory=self.factory,
            close_terminal_pages=False,
        )

        task_a = self.registry.get_task("child-a")
        task_b = self.registry.get_task("child-b")
        self.assertIsNotNone(task_a.current_worker_id)
        self.assertIsNone(task_b.current_worker_id)
        binding_a = self.registry.get_worker_window_binding(task_a.current_worker_id)
        self.assertIsNotNone(binding_a)
        self.assertEqual(binding_a.window_handle, 100)
        self.assertEqual(self.transport.open_calls, 1)
        self.assertEqual(self.transport.reuse_calls, 0)
        self.assertIn("child-a", first.waiting_for_binding)
        self.assertTrue(any(event.action == "pool_wait" for event in first.events))
        attempt_b = self.registry.unresolved_child_spawn_attempt("child-b")
        self.assertEqual(attempt_b.state, ChildSpawnAttemptState.ARMED)

        self.registry.bind_child_lsm_session("child-a", "s_child_a")
        second = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=1,
            chrome_executable=CHROME,
            workspace_loader=self.workspace,
            transport_factory=self.factory,
            close_terminal_pages=False,
        )

        task_b = self.registry.get_task("child-b")
        self.assertIsNotNone(task_b.current_worker_id)
        binding_b = self.registry.get_worker_window_binding(task_b.current_worker_id)
        self.assertIsNotNone(binding_b)
        self.assertEqual(binding_b.window_handle, 100)
        self.assertIsNone(self.registry.get_worker_window_binding(task_a.current_worker_id))
        self.assertEqual(self.transport.open_calls, 1)
        self.assertEqual(self.transport.reuse_calls, 1)
        reused = [event for event in second.events if event.action == "reused_window"]
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0].source_child_task_id, "child-a")

    def test_ambiguous_preflight_stops_before_any_new_mutation(self):
        self.add_child("child-a", "A")
        self.add_child("child-b", "B")
        attempt = arm_child_spawn(
            self.registry,
            child_task_id="child-a",
            chrome_executable=CHROME,
            workspace=self.workspace("child-a"),
        )
        submitted = submit_child_spawn_open(self.registry, attempt.attempt_id)
        self.registry.update_child_spawn_attempt(
            submitted.attempt_id,
            state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
            last_error="lost open result",
        )

        report = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=2,
            chrome_executable=CHROME,
            workspace_loader=lambda _child: (_ for _ in ()).throw(AssertionError("workspace touched")),
            transport_factory=lambda _chrome: (_ for _ in ()).throw(AssertionError("transport touched")),
            close_terminal_pages=False,
        )

        self.assertTrue(report.stopped)
        self.assertIn("reconcile before batch dispatch", report.stop_reason)
        self.assertEqual(self.transport.open_calls, 0)
        self.assertEqual(self.transport.send_calls, 0)
        self.assertIsNone(self.registry.unresolved_child_spawn_attempt("child-b"))

    def test_ambiguous_send_stops_before_dispatching_later_child(self):
        self.add_child("child-a", "A")
        self.add_child("child-b", "B")
        self.transport.ambiguous_child = "child-a"

        report = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=2,
            chrome_executable=CHROME,
            workspace_loader=self.workspace,
            transport_factory=self.factory,
            close_terminal_pages=False,
        )

        self.assertTrue(report.stopped)
        attempt = self.registry.unresolved_child_spawn_attempt("child-a")
        self.assertEqual(attempt.state, ChildSpawnAttemptState.RECONCILE_REQUIRED)
        self.assertIsNone(self.registry.unresolved_child_spawn_attempt("child-b"))
        self.assertEqual(self.transport.open_calls, 1)
        self.assertEqual(self.transport.send_calls, 1)

    def test_terminal_completed_spawn_reuses_without_lsm_binding(self):
        self.add_child("child-a", "A")
        first = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=1,
            chrome_executable=CHROME,
            workspace_loader=self.workspace,
            transport_factory=self.factory,
            close_terminal_pages=False,
        )
        self.assertFalse(first.stopped)
        source_attempt = self.registry.child_spawn_attempts("child-a", limit=1)[0]
        source_hwnd = source_attempt.window_handle
        self.registry.complete_child_dispatch("child-a", completion_ref="fixture:done-a")
        self.assertIsNone(self.registry.get_task("child-a").lsm_session_id)

        self.add_child("child-b", "B")
        second = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=1,
            chrome_executable=CHROME,
            workspace_loader=self.workspace,
            transport_factory=self.factory,
            close_terminal_pages=False,
        )

        task_b = self.registry.get_task("child-b")
        binding_b = self.registry.get_worker_window_binding(task_b.current_worker_id)
        self.assertEqual(binding_b.window_handle, source_hwnd)
        self.assertEqual(self.transport.reuse_spawn_calls, 1)
        consumed = self.registry.get_child_spawn_attempt(source_attempt.attempt_id)
        self.assertEqual(consumed.metadata.get("window_recycled_to"), self.registry.child_spawn_attempts("child-b", limit=1)[0].attempt_id)

    def test_terminal_page_can_close_without_lsm_binding(self):
        self.add_child("child-a", "A")
        first = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=1,
            chrome_executable=CHROME,
            workspace_loader=self.workspace,
            transport_factory=self.factory,
            close_terminal_pages=False,
        )
        self.assertFalse(first.stopped)
        source_attempt = self.registry.child_spawn_attempts("child-a", limit=1)[0]
        self.registry.complete_child_dispatch("child-a", completion_ref="fixture:done")
        self.assertIsNone(self.registry.get_task("child-a").lsm_session_id)

        report = advance_child_dispatch_batch(
            self.registry,
            parent_task_id="parent",
            max_windows=1,
            chrome_executable=CHROME,
            workspace_loader=self.workspace,
            transport_factory=self.factory,
        )

        self.assertFalse(report.stopped)
        self.assertEqual(self.transport.close_calls, 1)
        closed = self.registry.get_child_spawn_attempt(source_attempt.attempt_id)
        self.assertIsNotNone(closed.metadata.get("window_closed_at"))
        self.assertTrue(any(event.action == "closed_terminal_window" for event in report.events))


if __name__ == "__main__":
    unittest.main()
