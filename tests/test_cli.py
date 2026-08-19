import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cws.cli import _read_json_file, main
from cws.ram import MemoryProbeUnavailable


class CliInputTests(unittest.TestCase):
    def test_json_file_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "payload.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": True}).encode("utf-8"))
            self.assertEqual(_read_json_file(path), {"ok": True})

    def test_json_file_accepts_plain_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "payload.json"
            path.write_text('{"name":"cws"}', encoding="utf-8")
            self.assertEqual(_read_json_file(path), {"name": "cws"})

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
                patch("cws.cli.detect_lsm_state_dir", return_value=None),
                patch("cws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "cws.cli.observe_windows_process_group",
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
                patch("cws.cli.detect_lsm_state_dir", return_value=None),
                patch("cws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "cws.cli.observe_windows_process_group",
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
                patch("cws.cli.detect_lsm_state_dir", return_value=None),
                patch("cws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "cws.cli.observe_windows_process_group",
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
                patch("cws.cli.detect_lsm_state_dir", return_value=None),
                patch("cws.cli.observe_system_memory", side_effect=MemoryProbeUnavailable("fixture")),
                patch(
                    "cws.cli.observe_windows_process_group",
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
            from cws.registry import Registry
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
                with patch("cws.cli._assessment") as assessment, redirect_stderr(err):
                    code = main(argv)
                self.assertEqual(code, 12)
                assessment.assert_not_called()
                self.assertIn("dispatch blocked", err.getvalue())


if __name__ == "__main__":
    unittest.main()
