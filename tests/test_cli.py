import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lws.cli import _assessment, _read_json_file, main
from lws.models import SupervisorState
from lws.registry import Registry
from lws.ram import MemoryProbeUnavailable
from lws.watchdog_host import WatchdogLaunchResult, WatchdogStopResult


class CliInputTests(unittest.TestCase):
    def test_watchdog_restart_never_launches_until_old_host_is_proven_stopped(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            stop = WatchdogStopResult(
                requested=True,
                pid=123,
                stopped=False,
                stop_lease_cleared=False,
                detail="still stopping",
            )
            out = io.StringIO()
            with (
                patch("lws.cli.stop_watchdog_host", return_value=stop),
                patch("lws.cli.launch_detached_watchdog") as launch,
                redirect_stdout(out),
            ):
                code = main([
                    "--db", str(db), "watchdog-restart", "--wait", "0", "--json"
                ])
            self.assertEqual(code, 11)
            launch.assert_not_called()
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["stop"]["stopped"])
            self.assertIsNone(payload["launch"])

    def test_watchdog_restart_launches_only_after_successful_stop(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            stop = WatchdogStopResult(
                requested=True,
                pid=123,
                stopped=True,
                stop_lease_cleared=True,
                detail="watchdog exited cooperatively",
            )
            launched = WatchdogLaunchResult(
                spawn_pid=456,
                lease_pid=456,
                lease_owner="host:fixture",
                command=["python", "-m", "lws"],
                log_path=str(Path(td) / "watchdog.log"),
                lease_ready=True,
                detail="ready",
            )
            out = io.StringIO()
            with (
                patch("lws.cli.stop_watchdog_host", return_value=stop),
                patch("lws.cli.launch_detached_watchdog", return_value=launched) as launch,
                redirect_stdout(out),
            ):
                code = main([
                    "--db", str(db), "watchdog-restart", "--wait", "0", "--json"
                ])
            self.assertEqual(code, 0)
            launch.assert_called_once()
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["stop"]["stopped"])
            self.assertTrue(payload["launch"]["lease_ready"])

    def test_watchdog_assessment_accepts_new_task_before_protocol_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            registry = Registry(db)
            try:
                registry.register_task(
                    task_id="fresh",
                    project="lws",
                    objective="fresh fixture",
                    cwd=td,
                )
                self.assertFalse(registry.worker_protocol_exists("fresh"))
                with (
                    patch("lws.cli._refresh_lsm", return_value=None),
                    patch(
                        "lws.cli.assess",
                        return_value=__import__("lws.models", fromlist=["Assessment"]).Assessment(
                            SupervisorState.QUEUED,
                            "fresh legacy task",
                            "low",
                            [],
                        ),
                    ),
                ):
                    task, _browser, _lsm, _workspace, result = _assessment(
                        registry,
                        "fresh",
                        object(),
                        object(),
                    )
                self.assertEqual(task.task_id, "fresh")
                self.assertEqual(result.state, SupervisorState.QUEUED)
            finally:
                registry.close()

    def test_watchdog_assessment_preserves_completed_worker_protocol_task(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            registry = Registry(db)
            try:
                registry.register_task(
                    task_id="terminal",
                    project="lws",
                    objective="terminal fixture",
                    cwd=td,
                )
                state = registry.bootstrap_worker_protocol("terminal")
                completed = registry.protocol_complete_task(
                    "terminal",
                    completion_ref="fixture:done",
                    expected_revision=state.revision,
                    now=10,
                )
                self.assertTrue(completed.accepted)
                registry.update_state("terminal", SupervisorState.COMPLETED)

                task, browser, lsm, workspace, result = _assessment(
                    registry,
                    "terminal",
                    object(),
                    object(),
                )
                self.assertEqual(task.state, SupervisorState.COMPLETED)
                self.assertEqual(result.state, SupervisorState.COMPLETED)
                self.assertIsNone(lsm)
                self.assertIsNone(workspace)
                self.assertIn("worker-protocol", result.reason)
            finally:
                registry.close()

    def test_watchdog_assessment_does_not_write_legacy_state_for_open_protocol_task(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            registry = Registry(db)
            try:
                registry.register_task(
                    task_id="protocol-open",
                    project="lws",
                    objective="protocol fixture",
                    cwd=td,
                )
                registry.bootstrap_worker_protocol("protocol-open")
                assessment = __import__("lws.models", fromlist=["Assessment"]).Assessment(
                    SupervisorState.RUNNING,
                    "protocol task assessed as running",
                    "high",
                    ["fixture"],
                )
                with (
                    patch("lws.cli._refresh_lsm", return_value=None),
                    patch("lws.cli.assess", return_value=assessment),
                ):
                    task, _browser, _lsm, _workspace, result = _assessment(
                        registry,
                        "protocol-open",
                        object(),
                        object(),
                    )
                self.assertEqual(result.state, SupervisorState.RUNNING)
                self.assertEqual(task.state, SupervisorState.QUEUED)
                self.assertEqual(
                    registry.get_task("protocol-open").state,
                    SupervisorState.QUEUED,
                )
            finally:
                registry.close()

    def test_json_file_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "payload.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": True}).encode("utf-8"))
            self.assertEqual(_read_json_file(path), {"ok": True})

    def test_json_file_accepts_plain_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "payload.json"
            path.write_text('{"name":"lws"}', encoding="utf-8")
            self.assertEqual(_read_json_file(path), {"name": "lws"})

    def test_page_close_cli_requires_stronger_tool_gate_only_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence_path = root / "evidence.json"
            evidence = {
                "experiment_id": "fixture",
                "disposable_profile": False,
                "normally_authenticated": True,
                "auth_material_copied": False,
                "pre_close_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "reopened_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "pre_close_generating": True,
                "close_while_live_confirmed": True,
                "background_progress_observed": True,
                "completion_evidence_after_reopen": True,
                "same_conversation_after_reopen": True,
                "duplicate_turn_observed": False,
                "auth_still_valid_after_reopen": True,
                "pre_close_signature": "before",
                "post_reopen_signature": "after",
                "isolation_mode": "existing_profile_disposable_window",
                "exact_window_binding_confirmed": True,
                "current_user_conversation_excluded": True,
            }
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                code = main([
                    "--db", str(root / "registry.sqlite3"),
                    "evaluate-page-close", "--file", str(evidence_path), "--json",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["generation_parking_safe"])
            self.assertFalse(payload["tool_execution_parking_safe"])
            self.assertEqual(payload["required_gate"], "generation")

            out = io.StringIO()
            with redirect_stdout(out):
                code = main([
                    "--db", str(root / "registry.sqlite3"),
                    "evaluate-page-close", "--file", str(evidence_path),
                    "--require-tool", "--json",
                ])
            self.assertEqual(code, 8)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["required_gate"], "tool_execution")
            self.assertFalse(payload["required_gate_safe"])

            evidence.update({
                "tool_execution_observed": True,
                "tool_job_identity_confirmed": True,
                "tool_running_at_close": True,
                "tool_completed_after_close": True,
                "tool_final_response_after_reopen": True,
            })
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([
                    "--db", str(root / "registry.sqlite3"),
                    "evaluate-page-close", "--file", str(evidence_path),
                    "--require-tool", "--json",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["required_gate_safe"])

    def test_pool_plan_cli_requires_explicit_passing_page_close_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = io.StringIO()
            with (
                patch("lws.cli.detect_lsm_state_dir", return_value=None),
                patch("lws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "lws.cli.observe_windows_process_group",
                    side_effect=MemoryProbeUnavailable("fixture"),
                ),
                redirect_stdout(out),
            ):
                code = main(["--db", str(root / "registry.sqlite3"), "pool-plan", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["policy"]["page_close_experiment_passed"])

            evidence = {
                "experiment_id": "pool-fixture",
                "disposable_profile": False,
                "normally_authenticated": True,
                "auth_material_copied": False,
                "pre_close_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "reopened_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "pre_close_generating": True,
                "close_while_live_confirmed": True,
                "background_progress_observed": True,
                "completion_evidence_after_reopen": True,
                "same_conversation_after_reopen": True,
                "duplicate_turn_observed": False,
                "auth_still_valid_after_reopen": True,
                "pre_close_signature": "before",
                "post_reopen_signature": "after",
                "isolation_mode": "existing_profile_disposable_window",
                "exact_window_binding_confirmed": True,
                "current_user_conversation_excluded": True,
            }
            evidence_path = root / "page-close.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            out = io.StringIO()
            with (
                patch("lws.cli.detect_lsm_state_dir", return_value=None),
                patch("lws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "lws.cli.observe_windows_process_group",
                    side_effect=MemoryProbeUnavailable("fixture"),
                ),
                redirect_stdout(out),
            ):
                code = main([
                    "--db", str(root / "registry.sqlite3"),
                    "pool-plan",
                    "--page-close-evidence", str(evidence_path),
                    "--json",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["policy"]["page_close_experiment_passed"])

    def test_durable_page_close_capability_is_explicit_and_context_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            evidence_path = root / "capability-evidence.json"
            observed_at = time.time() - 10.0
            evidence = {
                "experiment_id": "durable-capability-fixture",
                "disposable_profile": False,
                "normally_authenticated": True,
                "auth_material_copied": False,
                "pre_close_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "reopened_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "pre_close_generating": True,
                "close_while_live_confirmed": True,
                "background_progress_observed": True,
                "completion_evidence_after_reopen": True,
                "same_conversation_after_reopen": True,
                "duplicate_turn_observed": False,
                "auth_still_valid_after_reopen": True,
                "pre_close_signature": "PRIVATE_BEFORE_SIGNATURE",
                "post_reopen_signature": "PRIVATE_AFTER_SIGNATURE",
                "isolation_mode": "existing_profile_disposable_window",
                "exact_window_binding_confirmed": True,
                "current_user_conversation_excluded": True,
                "observed_at": observed_at,
                "scope_host": "chatgpt.com",
                "browser_family": "chrome",
                "browser_major": 151,
                "platform": "windows",
                "surface": "normal_chrome_uia",
            }
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                code = main([
                    "--db", str(db),
                    "capability-import",
                    "--file", str(evidence_path),
                    "--browser-major", "151",
                    "--ttl-hours", "1",
                    "--json",
                ])
            self.assertEqual(code, 0)
            imported = json.loads(out.getvalue())
            self.assertEqual(len(imported), 1)
            capability_id = imported[0]["capability_id"]
            self.assertEqual(imported[0]["browser_major"], 151)
            self.assertNotIn("PRIVATE_BEFORE_SIGNATURE", json.dumps(imported))
            self.assertNotIn("PRIVATE_AFTER_SIGNATURE", json.dumps(imported))

            out = io.StringIO()
            with redirect_stdout(out):
                code = main([
                    "--db", str(db),
                    "capability-status",
                    "--browser-major", "151",
                    "--json",
                ])
            self.assertEqual(code, 0)
            status = json.loads(out.getvalue())
            self.assertEqual(status[0]["capability_id"], capability_id)
            self.assertTrue(status[0]["usable_now"])

            out = io.StringIO()
            with (
                patch("lws.cli.detect_lsm_state_dir", return_value=None),
                patch("lws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "lws.cli.observe_windows_process_group",
                    side_effect=MemoryProbeUnavailable("fixture"),
                ),
                redirect_stdout(out),
            ):
                code = main([
                    "--db", str(db),
                    "pool-plan",
                    "--page-close-capability", "latest",
                    "--browser-major", "151",
                    "--json",
                ])
            self.assertEqual(code, 0)
            pool = json.loads(out.getvalue())
            self.assertTrue(pool["policy"]["page_close_experiment_passed"])
            self.assertEqual(pool["page_close_capability_id"], capability_id)
            self.assertFalse(pool["legacy_page_close_evidence_used"])

            err = io.StringIO()
            with (
                patch("lws.cli.detect_lsm_state_dir", return_value=None),
                patch("lws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "lws.cli.observe_windows_process_group",
                    side_effect=MemoryProbeUnavailable("fixture"),
                ),
                redirect_stderr(err),
            ):
                code = main([
                    "--db", str(db),
                    "pool-plan",
                    "--page-close-capability", "latest",
                    "--browser-major", "152",
                    "--json",
                ])
            self.assertEqual(code, 2)
            self.assertIn("browser_major", err.getvalue())

    def test_capability_import_does_not_infer_missing_historical_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence = {
                "experiment_id": "missing-provenance-fixture",
                "disposable_profile": False,
                "normally_authenticated": True,
                "auth_material_copied": False,
                "pre_close_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "reopened_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "pre_close_generating": True,
                "close_while_live_confirmed": True,
                "background_progress_observed": True,
                "completion_evidence_after_reopen": True,
                "same_conversation_after_reopen": True,
                "duplicate_turn_observed": False,
                "auth_still_valid_after_reopen": True,
                "pre_close_signature": "fixture-before",
                "post_reopen_signature": "fixture-after",
                "isolation_mode": "existing_profile_disposable_window",
                "exact_window_binding_confirmed": True,
                "current_user_conversation_excluded": True,
                "observed_at": time.time() - 10.0,
            }
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                code = main([
                    "--db", str(root / "registry.sqlite3"),
                    "capability-import", "--file", str(evidence_path), "--json",
                ])
            self.assertEqual(code, 2)
            self.assertIn("provenance", err.getvalue())
            from lws.registry import Registry
            registry = Registry(root / "registry.sqlite3")
            try:
                self.assertEqual(registry.page_capabilities(), [])
            finally:
                registry.close()

    def test_dispatch_execute_requires_opt_in_before_any_observation(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "registry.sqlite3")
            for argv in (
                ["--db", db, "dispatch-execute", "t1", "--confirm-task", "t1"],
                [
                    "--db", db, "dispatch-execute", "t1",
                    "--confirm-task", "other", "--enable-experimental-uia",
                ],
            ):
                err = io.StringIO()
                with patch("lws.cli._assessment") as assessment, redirect_stderr(err):
                    code = main(argv)
                self.assertEqual(code, 12)
                assessment.assert_not_called()
                self.assertIn("dispatch blocked", err.getvalue())

    def test_child_dispatch_batch_requires_explicit_mutation_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "registry.sqlite3")
            err = io.StringIO()
            with redirect_stderr(err):
                code = main([
                    "--db", db,
                    "child-dispatch-batch", "parent",
                    "--confirm-parent", "parent",
                ])
            self.assertEqual(code, 14)
            self.assertIn("explicit opt-in", err.getvalue())



if __name__ == "__main__":
    unittest.main()
