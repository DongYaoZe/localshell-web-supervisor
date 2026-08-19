import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lws.child_spawn import (
    ChildSpawnBlocked,
    ChildSpawnExecution,
    ChromeUiaChildSpawnTransport,
    arm_child_spawn,
    web_conversation_project_id,
    web_project_id,
    execute_child_spawn_open,
    execute_child_spawn_prompt,
    owned_project_root_matches,
    reconcile_child_spawn,
    tagged_project_url,
)
from lws.db import SCHEMA_V5, SCHEMA_V6, SCHEMA_V7, SCHEMA_V8
from lws.models import ChildSpawnAttemptState, WorkspaceObservation
from lws.registry import Registry


PROJECT_ID = "0123456789abcdef0123456789abcdef"
PROJECT = f"https://chatgpt.com/g/g-p-{PROJECT_ID}"
PROJECT_SLUG = f"https://chatgpt.com/g/g-p-{PROJECT_ID}-localshell-web-supervisor/project"
CONVERSATION = f"https://chatgpt.com/g/g-p-{PROJECT_ID}/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_CONVERSATION = (
    "https://chatgpt.com/g/g-p-11111111111111111111111111111111/"
    "c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class FakeSpawnTransport:
    def __init__(self, *, open_result=None, send_result=None, conversation_result=None):
        self.open_result = open_result
        self.send_result = send_result
        self.conversation_result = conversation_result
        self.open_calls = 0
        self.send_calls = 0

    def open_authorized(self, attempt):
        self.open_calls += 1
        return self.open_result or ChildSpawnExecution(
            True,
            True,
            "opened",
            window_handle=101,
            browser_pid=202,
            url=attempt.tagged_project_url,
        )

    def send_authorized(self, attempt, prompt):
        self.send_calls += 1
        return self.send_result or ChildSpawnExecution(
            True,
            True,
            "sent",
            window_handle=attempt.window_handle,
            browser_pid=attempt.browser_pid,
            url=attempt.tagged_project_url,
        )

    def wait_for_conversation(self, attempt):
        return self.conversation_result or ChildSpawnExecution(
            True,
            False,
            "conversation",
            window_handle=attempt.window_handle,
            browser_pid=attempt.browser_pid,
            url=CONVERSATION,
        )

    def observe_owned_project(self, attempt):
        return ChildSpawnExecution(
            False,
            False,
            "observed",
            window_handle=101,
            browser_pid=202,
            url=attempt.tagged_project_url,
        )

    def _observe_bound_url(self, attempt):
        return self.conversation_result or ChildSpawnExecution(
            False,
            False,
            "observed bound",
            window_handle=attempt.window_handle,
            browser_pid=attempt.browser_pid,
            url=CONVERSATION,
        )


class ChildSpawnTests(unittest.TestCase):
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
        self.registry.create_child_dispatch(
            "parent",
            child_key="A",
            child_task_id="child",
            project="lws",
            objective="child",
            cwd="D:/repo-wt",
            prompt_text="Use Local Shell MCP goal mode. Implement A; test; commit.",
            web_project_url=PROJECT_SLUG,
            now=1,
        )

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def workspace(self, *, at=10):
        return WorkspaceObservation(
            task_id="child",
            observed_at=at,
            cwd="D:/repo-wt",
            cwd_exists=True,
            is_git_repo=False,
        )

    def arm(self):
        return arm_child_spawn(
            self.registry,
            child_task_id="child",
            chrome_executable=CHROME,
            workspace=self.workspace(),
            now=10,
        )

    def test_project_identity_tolerates_slug_canonicalization(self):
        self.assertEqual(web_project_id(PROJECT), PROJECT_ID)
        self.assertEqual(web_project_id(PROJECT_SLUG), PROJECT_ID)
        self.assertEqual(web_conversation_project_id(CONVERSATION), PROJECT_ID)
        self.assertIn("#lws-child=spawn_test:owner123", tagged_project_url(PROJECT, "spawn_test:owner123"))
        with self.assertRaises(ValueError):
            web_project_id(CONVERSATION)
        with self.assertRaises(ValueError):
            web_project_id("https://example.com/g/g-p-" + PROJECT_ID)

    def test_owned_project_root_accepts_only_same_project_and_owner_after_canonicalization(self):
        attempt = self.arm()
        canonical = (
            PROJECT_SLUG
            + "#lws-child="
            + attempt.owner_token
        )
        self.assertTrue(owned_project_root_matches(canonical, attempt))
        self.assertFalse(owned_project_root_matches(PROJECT_SLUG + "#lws-child=other-owner", attempt))
        self.assertFalse(
            owned_project_root_matches(
                "https://chatgpt.com/g/g-p-11111111111111111111111111111111/project#lws-child="
                + attempt.owner_token,
                attempt,
            )
        )

    @patch("lws.child_spawn.os.name", "nt")
    @patch("lws.child_spawn._run_powershell_json")
    def test_real_sender_uses_canonical_current_url_as_literal_exact_fence(self, run_ps):
        attempt = self.arm()
        bound = self.registry.update_child_spawn_attempt(
            attempt.attempt_id,
            state=ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
            now=11,
        )
        bound = self.registry.update_child_spawn_attempt(
            attempt.attempt_id,
            state=ChildSpawnAttemptState.WINDOW_BOUND,
            window_handle=101,
            browser_pid=202,
            now=12,
        )
        submitted = self.registry.update_child_spawn_attempt(
            attempt.attempt_id,
            state=ChildSpawnAttemptState.PROMPT_SUBMITTED,
            now=13,
        )
        canonical = PROJECT_SLUG + "#lws-child=" + submitted.owner_token
        transport = __import__(
            "lws.child_spawn", fromlist=["ChromeUiaChildSpawnTransport"]
        ).ChromeUiaChildSpawnTransport(chrome_executable=CHROME, enabled=True)
        with patch.object(
            transport,
            "_observe_bound_url",
            return_value=ChildSpawnExecution(
                False, False, "canonical", window_handle=101, browser_pid=202, url=canonical
            ),
        ):
            run_ps.return_value = {
                "submitted": True,
                "side_effect_possible": True,
                "detail": "sent",
            }
            result = transport.send_authorized(submitted, "hello")
        self.assertTrue(result.submitted)
        self.assertEqual(run_ps.call_args.kwargs["env"]["LWS_EXPECTED_URL"], canonical)

    def test_initial_child_spawn_open_send_adopts_exact_same_project_conversation(self):
        attempt = self.arm()
        self.assertEqual(attempt.state, ChildSpawnAttemptState.ARMED)
        transport = FakeSpawnTransport()
        bound = execute_child_spawn_open(
            self.registry,
            attempt_id=attempt.attempt_id,
            transport=transport,
            now=11,
        )
        self.assertEqual(bound.state, ChildSpawnAttemptState.WINDOW_BOUND)
        self.assertEqual(bound.window_handle, 101)
        completed = execute_child_spawn_prompt(
            self.registry,
            attempt_id=attempt.attempt_id,
            transport=transport,
            now=12,
        )
        self.assertEqual(completed.state, ChildSpawnAttemptState.COMPLETED)
        self.assertEqual(completed.conversation_url, CONVERSATION)
        protocol = self.registry.load_worker_protocol("child")
        self.assertEqual(protocol.generation, 1)
        self.assertEqual(protocol.current_worker_id, completed.worker_id)
        binding = self.registry.get_worker_window_binding(completed.worker_id, require_fresh=False)
        self.assertEqual(binding.window_handle, 101)
        self.assertEqual(binding.conversation_url, CONVERSATION)
        self.assertEqual(transport.open_calls, 1)
        self.assertEqual(transport.send_calls, 1)

    def test_ambiguous_open_is_write_ahead_and_never_replayed(self):
        attempt = self.arm()
        transport = FakeSpawnTransport(
            open_result=ChildSpawnExecution(False, True, "launch happened, no unique HWND")
        )
        unresolved = execute_child_spawn_open(
            self.registry,
            attempt_id=attempt.attempt_id,
            transport=transport,
            now=11,
        )
        self.assertEqual(unresolved.state, ChildSpawnAttemptState.RECONCILE_REQUIRED)
        self.assertEqual(transport.open_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "not ARMED"):
            execute_child_spawn_open(
                self.registry,
                attempt_id=attempt.attempt_id,
                transport=transport,
                now=12,
            )
        self.assertEqual(transport.open_calls, 1)

    def test_prompt_send_ambiguity_does_not_replay_and_reconcile_can_finish(self):
        attempt = self.arm()
        open_transport = FakeSpawnTransport()
        execute_child_spawn_open(
            self.registry, attempt_id=attempt.attempt_id, transport=open_transport, now=11
        )
        ambiguous = FakeSpawnTransport(
            send_result=ChildSpawnExecution(False, True, "draft/send outcome unknown"),
            conversation_result=ChildSpawnExecution(
                False,
                False,
                "later conversation observed",
                window_handle=101,
                browser_pid=202,
                url=CONVERSATION,
            ),
        )
        unresolved = execute_child_spawn_prompt(
            self.registry,
            attempt_id=attempt.attempt_id,
            transport=ambiguous,
            now=12,
        )
        self.assertEqual(unresolved.state, ChildSpawnAttemptState.RECONCILE_REQUIRED)
        self.assertEqual(ambiguous.send_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "not durably bound"):
            execute_child_spawn_prompt(
                self.registry,
                attempt_id=attempt.attempt_id,
                transport=ambiguous,
                now=13,
            )
        self.assertEqual(ambiguous.send_calls, 1)
        completed = reconcile_child_spawn(
            self.registry,
            attempt_id=attempt.attempt_id,
            transport=ambiguous,
            now=14,
        )
        self.assertEqual(completed.state, ChildSpawnAttemptState.COMPLETED)
        self.assertEqual(completed.conversation_url, CONVERSATION)

    def test_different_project_conversation_never_becomes_child_worker(self):
        attempt = self.arm()
        transport = FakeSpawnTransport(
            conversation_result=ChildSpawnExecution(
                True,
                False,
                "wrong project",
                window_handle=101,
                browser_pid=202,
                url=OTHER_CONVERSATION,
            )
        )
        execute_child_spawn_open(
            self.registry, attempt_id=attempt.attempt_id, transport=transport, now=11
        )
        unresolved = execute_child_spawn_prompt(
            self.registry, attempt_id=attempt.attempt_id, transport=transport, now=12
        )
        self.assertEqual(unresolved.state, ChildSpawnAttemptState.RECONCILE_REQUIRED)
        self.assertIsNone(self.registry.load_worker_protocol("child").current_worker_id)

    def test_wait_tolerates_transient_web_routes_but_accepts_only_same_project_conversation(self):
        attempt = self.arm()
        transport = ChromeUiaChildSpawnTransport(
            chrome_executable=CHROME,
            enabled=False,
            conversation_timeout_s=1,
        )
        transient = ChildSpawnExecution(
            False, False, "router", window_handle=101, browser_pid=202, url="https://chatgpt.com/"
        )
        project_root = ChildSpawnExecution(
            False,
            False,
            "project router",
            window_handle=101,
            browser_pid=202,
            url=PROJECT_SLUG,
        )
        final = ChildSpawnExecution(
            False,
            False,
            "conversation",
            window_handle=101,
            browser_pid=202,
            url=CONVERSATION,
        )
        with patch.object(
            transport, "_observe_bound_url", side_effect=[transient, project_root, final]
        ), patch("lws.child_spawn.time.sleep", return_value=None):
            observed = transport.wait_for_conversation(attempt)
        self.assertTrue(observed.changed)
        self.assertEqual(observed.url, CONVERSATION)
        self.assertIn("expected-project conversation", observed.detail)

    def test_initial_only_gate_blocks_task_with_any_worker_history(self):
        self.registry.adopt_child_worker(
            "child", CONVERSATION, worker_id="existing", lease_seconds=60, now=5
        )
        with self.assertRaises(ChildSpawnBlocked) as ctx:
            self.arm()
        self.assertTrue(any("initial-worker only" in blocker for blocker in ctx.exception.blockers))
        self.assertIsNone(self.registry.unresolved_child_spawn_attempt("child"))

    def test_schema_v7_registry_migrates_project_column_and_spawn_table(self):
        db = Path(self.tmp.name) / "legacy-v7.sqlite3"
        raw = sqlite3.connect(db)
        raw.execute("PRAGMA foreign_keys = ON")
        raw.executescript(SCHEMA_V5 + SCHEMA_V6 + SCHEMA_V7)
        raw.execute("PRAGMA user_version = 7")
        raw.execute(
            """INSERT INTO tasks
               (task_id, project, objective, cwd, state, checkpoint_json, created_at, updated_at)
               VALUES ('p','lws','parent','.', 'QUEUED','{}',1,1)"""
        )
        raw.execute(
            """INSERT INTO tasks
               (task_id, project, objective, cwd, state, checkpoint_json, created_at, updated_at)
               VALUES ('c','lws','child','.', 'QUEUED','{}',1,1)"""
        )
        raw.execute(
            """INSERT INTO child_dispatches
               (dispatch_id,parent_task_id,child_task_id,child_key,prompt_text,prompt_sha256,
                expected_branch,base_ref,created_at,updated_at,payload_json)
               VALUES ('d','p','c','A','x','sha',NULL,NULL,1,1,'{}')"""
        )
        raw.commit()
        raw.close()
        migrated = Registry(db)
        try:
            self.assertEqual(migrated._conn.execute("PRAGMA user_version").fetchone()[0], 9)
            columns = {
                row["name"] for row in migrated._conn.execute("PRAGMA table_info(child_dispatches)")
            }
            self.assertIn("web_project_url", columns)
            self.assertIsNone(migrated.get_child_dispatch("c").web_project_url)
            self.assertIsNotNone(
                migrated._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='child_spawn_attempts'"
                ).fetchone()
            )
        finally:
            migrated.close()

    def test_schema_v8_legacy_project_column_is_copied_to_provider_neutral_column(self):
        db = Path(self.tmp.name) / "legacy-v8.sqlite3"
        raw = sqlite3.connect(db)
        raw.execute("PRAGMA foreign_keys = ON")
        raw.executescript(SCHEMA_V5 + SCHEMA_V6 + SCHEMA_V7 + SCHEMA_V8)
        raw.execute("ALTER TABLE child_dispatches ADD COLUMN chatgpt_project_url TEXT")
        raw.execute("PRAGMA user_version = 8")
        raw.execute(
            """INSERT INTO tasks
               (task_id, project, objective, cwd, state, checkpoint_json, created_at, updated_at)
               VALUES ('p8','lws','parent','.', 'QUEUED','{}',1,1)"""
        )
        raw.execute(
            """INSERT INTO tasks
               (task_id, project, objective, cwd, state, checkpoint_json, created_at, updated_at)
               VALUES ('c8','lws','child','.', 'QUEUED','{}',1,1)"""
        )
        raw.execute(
            """INSERT INTO child_dispatches
               (dispatch_id,parent_task_id,child_task_id,child_key,prompt_text,prompt_sha256,
                expected_branch,base_ref,created_at,updated_at,payload_json,chatgpt_project_url)
               VALUES ('d8','p8','c8','A','x','sha',NULL,NULL,1,1,'{}',?)""",
            (PROJECT,),
        )
        raw.commit()
        raw.close()
        migrated = Registry(db)
        try:
            self.assertEqual(migrated._conn.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(migrated.get_child_dispatch("c8").web_project_url, PROJECT)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
