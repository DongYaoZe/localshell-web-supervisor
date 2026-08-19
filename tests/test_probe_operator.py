import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cws.cli import main
from cws.models import ProbeMutationState, WorkerStatus
from cws.page_runtime import (
    PROBE_SLOT_SOURCE,
    plan_probe_slot,
    probe_close_operation,
    probe_operation_from_plan,
    tagged_probe_url,
)
from cws.registry import Registry

URL1 = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL2 = "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class ProbeOperatorCliTests(unittest.TestCase):
    def _register_parked(self, registry: Registry, *, task_id: str, url: str):
        task = registry.register_task(
            task_id=task_id,
            project="p",
            objective="o",
            cwd=".",
            conversation_url=url,
        )
        worker = registry.get_worker(task.current_worker_id)
        registry.set_worker_status(worker.worker_id, WorkerStatus.PARKED)
        return registry.get_worker(worker.worker_id)

    def _open_operation(self, registry: Registry, *, submitted: bool = False):
        worker = self._register_parked(registry, task_id="t-open", url=URL1)
        plan = plan_probe_slot(worker, None, now=10.0)
        operation = probe_operation_from_plan(
            worker,
            plan,
            None,
            chrome_executable=CHROME,
            now=10.0,
            operation_id="open-op",
            nonce="open-nonce",
        )
        operation = registry.arm_probe_mutation_operation(operation)
        if submitted:
            operation = registry.authorize_probe_open(operation.operation_id, now=11.0)
        return worker, operation

    def _bind_slot(self, registry: Registry, worker, *, owner: str = "old-owner"):
        actual = tagged_probe_url(
            worker.conversation_url,
            slot_id="probe:default",
            owner_token=owner,
        )
        return registry.bind_probe_window_slot(
            "probe:default",
            owner_token=owner,
            target_worker_id=worker.worker_id,
            target_conversation_url=worker.conversation_url,
            actual_url=actual,
            window_handle=123,
            browser_pid=456,
            chrome_executable=CHROME,
            source=PROBE_SLOT_SOURCE,
            observed_at=100.0,
            ttl_s=1000.0,
        )

    def _close_operation(self, registry: Registry, *, submitted: bool = True):
        worker = self._register_parked(registry, task_id="t-close", url=URL1)
        slot = self._bind_slot(registry, worker)
        operation = probe_close_operation(
            worker,
            slot,
            now=110.0,
            operation_id="close-op",
            nonce="close-nonce",
        )
        operation = registry.arm_probe_mutation_operation(operation)
        if submitted:
            operation = registry.authorize_probe_close(operation.operation_id, now=111.0)
        return worker, slot, operation

    def _rotate_operation(self, registry: Registry, *, open_submitted: bool = False):
        old_worker = self._register_parked(registry, task_id="t-old", url=URL1)
        old_slot = self._bind_slot(registry, old_worker)
        new_worker = self._register_parked(registry, task_id="t-new", url=URL2)
        plan = plan_probe_slot(new_worker, old_slot, now=150.0)
        operation = probe_operation_from_plan(
            new_worker,
            plan,
            old_slot,
            chrome_executable=CHROME,
            now=150.0,
            operation_id="rotate-op",
            nonce="rotate-nonce",
        )
        operation = registry.arm_probe_mutation_operation(operation)
        operation = registry.authorize_probe_close(operation.operation_id, now=151.0)
        if open_submitted:
            operation = registry.reconcile_probe_mutation_operation(
                operation.operation_id,
                self._observation_absent(152.0),
            )
            self.assertEqual(operation.state, ProbeMutationState.READY_TO_OPEN)
            operation = registry.authorize_probe_open(operation.operation_id, now=153.0)
        return old_slot, new_worker, operation

    @staticmethod
    def _observation_absent(observed_at: float):
        from cws.probe_ops import ProbeMutationObservation

        return ProbeMutationObservation(observed_at=observed_at)

    @staticmethod
    def _old_match(slot, *, hwnd=None, pid=None, executable=None):
        return {
            "window_handle": slot.window_handle if hwnd is None else hwnd,
            "browser_pid": slot.browser_pid if pid is None else pid,
            "chrome_executable": slot.chrome_executable if executable is None else executable,
            "actual_url": slot.actual_url,
        }

    @staticmethod
    def _new_match(operation, *, hwnd=900, pid=901, executable=CHROME, url=None):
        return {
            "window_handle": hwnd,
            "browser_pid": pid,
            "chrome_executable": executable,
            "actual_url": operation.expected_actual_url if url is None else url,
        }

    @staticmethod
    def _evidence(operation, *, observed_at=200.0, old_matches=None, new_matches=None):
        return {
            "operation_id": operation.operation_id,
            "owner_token": operation.owner_token,
            "observed_at": observed_at,
            "complete": True,
            "old_matches": list(old_matches or []),
            "new_matches": list(new_matches or []),
        }

    @staticmethod
    def _write(path: Path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _run(self, db: Path, argv):
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch("cws.cli.detect_lsm_state_dir", return_value=None),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(["--db", str(db), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_status_with_no_operation(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            code, out, err = self._run(db, ["probe-op-status", "--json"])
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["selection"], "unresolved")
            self.assertEqual(payload["classification"], "NONE")
            self.assertIsNone(payload["operation"])

    def test_status_unresolved_and_exact_id_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            registry = Registry(db)
            try:
                _worker, operation = self._open_operation(registry)
            finally:
                registry.close()

            code, out, err = self._run(db, ["probe-op-status", "--json"])
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["classification"], "UNRESOLVED")
            self.assertEqual(payload["operation"]["operation_id"], operation.operation_id)

            code, out, err = self._run(
                db, ["probe-op-status", operation.operation_id, "--json"]
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["selection"], "exact")
            self.assertEqual(payload["operation"]["state"], "ARMED")

            code, out, err = self._run(
                db, ["probe-op-status", operation.operation_id]
            )
            self.assertEqual(code, 0, err)
            self.assertIn(operation.operation_id, out)
            self.assertIn("state=ARMED", out)
            self.assertIn("classification=UNRESOLVED", out)

    def test_malformed_incomplete_and_forbidden_fields_are_rejected_without_state_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                _worker, operation = self._open_operation(registry, submitted=True)
            finally:
                registry.close()

            evidence_path = root / "evidence.json"
            cases = []
            missing = self._evidence(operation)
            missing.pop("new_matches")
            cases.append(missing)
            forbidden = self._evidence(operation)
            forbidden["cookies"] = {"session": "must-not-be-accepted"}
            cases.append(forbidden)
            malformed_match = self._evidence(
                operation,
                new_matches=[{"window_handle": 1, "browser_pid": 2}],
            )
            cases.append(malformed_match)
            stale = self._evidence(operation, observed_at=10.5)
            cases.append(stale)

            for payload in cases:
                with self.subTest(payload=sorted(payload)):
                    self._write(evidence_path, payload)
                    code, _out, err = self._run(
                        db,
                        [
                            "probe-op-reconcile",
                            operation.operation_id,
                            "--file",
                            str(evidence_path),
                            "--json",
                        ],
                    )
                    self.assertEqual(code, 2)
                    self.assertIn("invalid input", err)

            registry = Registry(db)
            try:
                unchanged = registry.get_probe_mutation_operation(operation.operation_id)
                self.assertEqual(unchanged.state, ProbeMutationState.OPEN_SUBMITTED)
                self.assertEqual(unchanged.reconcile_attempts, 0)
            finally:
                registry.close()

    def test_submitted_close_old_target_still_present_does_not_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                _worker, slot, operation = self._close_operation(registry)
            finally:
                registry.close()
            evidence_path = root / "evidence.json"
            self._write(
                evidence_path,
                self._evidence(operation, old_matches=[self._old_match(slot)]),
            )

            with (
                patch("cws.cli._refresh_uia") as refresh_uia,
                patch("cws.page_runtime.ChromeUiaProbeWindowTransport.close_authorized") as close,
            ):
                code, out, err = self._run(
                    db,
                    [
                        "probe-op-reconcile",
                        operation.operation_id,
                        "--file",
                        str(evidence_path),
                        "--json",
                    ],
                )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["classification"], "BLOCKED")
            self.assertEqual(payload["state"], "RECONCILE_REQUIRED")
            self.assertEqual(payload["last_outcome"], "OLD_TARGET_STILL_PRESENT")
            refresh_uia.assert_not_called()
            close.assert_not_called()

    def test_both_old_and_new_present_remains_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                old_slot, _worker, operation = self._rotate_operation(registry)
            finally:
                registry.close()
            evidence_path = root / "evidence.json"
            self._write(
                evidence_path,
                self._evidence(
                    operation,
                    old_matches=[self._old_match(old_slot)],
                    new_matches=[self._new_match(operation)],
                ),
            )
            code, out, err = self._run(
                db,
                [
                    "probe-op-reconcile",
                    operation.operation_id,
                    "--file",
                    str(evidence_path),
                    "--json",
                ],
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["classification"], "BLOCKED")
            self.assertEqual(payload["last_outcome"], "BOTH_OLD_AND_NEW_PRESENT")

    def test_multiple_exact_matches_remain_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                _worker, operation = self._open_operation(registry, submitted=True)
            finally:
                registry.close()
            evidence_path = root / "evidence.json"
            self._write(
                evidence_path,
                self._evidence(
                    operation,
                    new_matches=[
                        self._new_match(operation, hwnd=801, pid=901),
                        self._new_match(operation, hwnd=802, pid=902),
                    ],
                ),
            )
            code, out, err = self._run(
                db,
                [
                    "probe-op-reconcile",
                    operation.operation_id,
                    "--file",
                    str(evidence_path),
                    "--json",
                ],
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["classification"], "BLOCKED")
            self.assertEqual(payload["last_outcome"], "MULTIPLE_MATCHES")

    def test_unique_expected_target_after_submitted_open_adopts_and_completes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                worker, operation = self._open_operation(registry, submitted=True)
            finally:
                registry.close()
            evidence_path = root / "evidence.json"
            match = self._new_match(operation, hwnd=777, pid=888)
            self._write(
                evidence_path,
                self._evidence(operation, new_matches=[match]),
            )

            with (
                patch("cws.cli._refresh_uia") as refresh_uia,
                patch("cws.page_runtime.ChromeUiaProbeWindowTransport.open_authorized") as mutate,
            ):
                code, out, err = self._run(
                    db,
                    [
                        "probe-op-reconcile",
                        operation.operation_id,
                        "--file",
                        str(evidence_path),
                        "--json",
                    ],
                )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["classification"], "COMPLETED")
            self.assertTrue(payload["completed"])
            refresh_uia.assert_not_called()
            mutate.assert_not_called()

            registry = Registry(db)
            try:
                saved = registry.get_probe_mutation_operation(operation.operation_id)
                self.assertEqual(saved.state, ProbeMutationState.COMPLETED)
                slot = registry.get_probe_window_slot("probe:default")
                self.assertIsNotNone(slot)
                self.assertEqual(slot.target_worker_id, worker.worker_id)
                self.assertEqual(slot.window_handle, 777)
                self.assertEqual(slot.browser_pid, 888)
            finally:
                registry.close()

    def test_complete_false_is_valid_bounded_evidence_but_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                _worker, operation = self._open_operation(registry, submitted=True)
            finally:
                registry.close()
            evidence = self._evidence(operation)
            evidence["complete"] = False
            evidence_path = root / "evidence.json"
            self._write(evidence_path, evidence)

            code, out, err = self._run(
                db,
                [
                    "probe-op-reconcile",
                    operation.operation_id,
                    "--file",
                    str(evidence_path),
                    "--json",
                ],
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["classification"], "BLOCKED")
            self.assertEqual(payload["last_outcome"], "UNKNOWN_OBSERVATION")

    def test_wrong_operation_owner_or_url_is_rejected_before_reconciliation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                _worker, operation = self._open_operation(registry, submitted=True)
            finally:
                registry.close()
            evidence_path = root / "evidence.json"

            wrong_operation = self._evidence(operation)
            wrong_operation["operation_id"] = "other-op"
            wrong_owner = self._evidence(operation)
            wrong_owner["owner_token"] = "other-owner"
            wrong_url = self._evidence(
                operation,
                new_matches=[
                    self._new_match(
                        operation,
                        url="https://chatgpt.com/c/11111111-2222-3333-4444-555555555555#cws-probe=wrong",
                    )
                ],
            )
            for payload in (wrong_operation, wrong_owner, wrong_url):
                self._write(evidence_path, payload)
                code, _out, err = self._run(
                    db,
                    [
                        "probe-op-reconcile",
                        operation.operation_id,
                        "--file",
                        str(evidence_path),
                        "--json",
                    ],
                )
                self.assertEqual(code, 2)
                self.assertIn("invalid input", err)

            registry = Registry(db)
            try:
                unchanged = registry.get_probe_mutation_operation(operation.operation_id)
                self.assertEqual(unchanged.state, ProbeMutationState.OPEN_SUBMITTED)
                self.assertEqual(unchanged.reconcile_attempts, 0)
            finally:
                registry.close()

    def test_status_latest_can_show_completed_operation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "registry.sqlite3"
            registry = Registry(db)
            try:
                _worker, operation = self._open_operation(registry, submitted=True)
                from cws.probe_ops import ProbeMutationObservation, ProbeWindowMatch

                registry.reconcile_probe_mutation_operation(
                    operation.operation_id,
                    ProbeMutationObservation(
                        observed_at=12.0,
                        new_matches=[
                            ProbeWindowMatch(700, 701, CHROME, operation.expected_actual_url)
                        ],
                    ),
                )
            finally:
                registry.close()
            code, out, err = self._run(
                db, ["probe-op-status", "--latest", "--json"]
            )
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["selection"], "latest")
            self.assertEqual(payload["classification"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
