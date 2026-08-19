import json
import re
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from cws.actions import (
    ActionAcknowledgement,
    ActionAttempt,
    ActionAttemptState,
    ActionBlocked,
)
from cws.browser_pool import PagePool, PagePoolError, PageRole
from cws.capabilities import CapabilityContext, capability_matches_context
from cws.lsm import FileLsmTelemetry
from cws.models import (
    Assessment,
    BrowserObservation,
    LsmObservation,
    PageCapabilityKind,
    PageCapabilityRecord,
    ProbeWindowSlotBinding,
    SupervisorState,
    TaskRecord,
    WorkerRecord,
    WorkerStatus,
    WorkspaceObservation,
)
from cws.page_runtime import (
    _FIND_SCRIPT,
    ChromeUiaProbeWindowTransport,
    ProbeSlotAction,
    plan_probe_slot,
    tagged_probe_url,
)
from cws.reconcile import build_reconciliation_record, fence_matches
from cws.registry import Registry
from cws.scheduler import attention_queue
from cws.uia_actions import UiaAckObservation, acknowledgement_from_uia_observation


URL1 = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL2 = "https://chatgpt.com/c/ffffffff-1111-2222-3333-444444444444"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
NOW = 1000.0


def action_attempt(
    *,
    attempt_id="act-1",
    task_id="t1",
    worker_id="w1",
    state=ActionAttemptState.ARMED,
    created_at=100.0,
):
    return ActionAttempt(
        attempt_id=attempt_id,
        task_id=task_id,
        worker_id=worker_id,
        action="CONTINUE_CURRENT_WORKER",
        fence_token="fence",
        fence_version=2,
        prompt_hash="prompt-hash",
        nonce=f"nonce-{attempt_id}",
        state=state,
        created_at=created_at,
        updated_at=created_at,
    )


def worker(url=URL1, *, worker_id="w1", status=WorkerStatus.PARKED):
    return WorkerRecord(worker_id, "t1", url, None, status, 1.0)


def probe_slot(*, url=URL1, worker_id="w1", owner="owner", expires_at=1200.0):
    return ProbeWindowSlotBinding(
        slot_id="probe:default",
        owner_token=owner,
        target_worker_id=worker_id,
        target_conversation_url=url,
        actual_url=tagged_probe_url(url, slot_id="probe:default", owner_token=owner),
        window_handle=111,
        browser_pid=222,
        chrome_executable=CHROME,
        source="windows_uia_cws_probe",
        bound_at=900.0,
        observed_at=900.0,
        expires_at=expires_at,
    )


def reconciliation_inputs(*, continuation_pending=False, active_run_id="r1", status_hash="clean"):
    task = TaskRecord(
        task_id="t1",
        project="p",
        objective="o",
        cwd="C:/repo",
        state=SupervisorState.RECONCILING,
        lsm_session_id="s1",
        current_worker_id="w1",
        recovery_attempts=0,
        max_recovery_attempts=3,
    )
    assessment = Assessment(
        state=SupervisorState.RECONCILING,
        reason="fixture",
        confidence="high",
        requires_reconcile=True,
    )
    browser = BrowserObservation(
        worker_id="w1",
        observed_at=NOW,
        url=URL1,
        generating=False,
        send_button_ready=True,
        last_dom_change_at=NOW - 20,
        message_signature="sig",
    )
    lsm = LsmObservation(
        task_id="t1",
        observed_at=NOW,
        session_id="s1",
        session_status="active",
        active_run_id=active_run_id,
        plan_status="active",
        continuation_due=False,
        continuation_pending=continuation_pending,
        in_flight_calls=0,
        active_jobs=0,
    )
    workspace = WorkspaceObservation(
        task_id="t1",
        observed_at=NOW,
        cwd="C:/repo",
        cwd_exists=True,
        is_git_repo=True,
        git_root="C:/repo",
        git_head="abc123",
        git_dirty=status_hash != "clean",
        git_status_hash=status_hash,
    )
    return task, assessment, browser, lsm, workspace


class RegistryCrashRaceTests(unittest.TestCase):
    def test_arm_and_budget_increment_roll_back_together_on_insert_collision(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            try:
                task = registry.register_task(
                    task_id="t1",
                    project="p",
                    objective="o",
                    cwd=td,
                    conversation_url=URL1,
                )
                worker_id = task.current_worker_id
                registry.record_action_attempt(
                    action_attempt(
                        attempt_id="collision",
                        worker_id=worker_id,
                        state=ActionAttemptState.FAILED,
                    )
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    registry.record_recovery_action_attempt(
                        action_attempt(attempt_id="collision", worker_id=worker_id)
                    )

                self.assertEqual(registry.get_task("t1").recovery_attempts, 0)
                self.assertEqual(
                    registry.get_action_attempt("collision").state,
                    ActionAttemptState.FAILED,
                )
                self.assertIsNone(registry.unresolved_action_attempt("t1"))
            finally:
                registry.close()

    def test_two_registry_connections_cannot_consume_one_recovery_slot_twice(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "registry.sqlite3"
            setup = Registry(db)
            task = setup.register_task(
                task_id="t1",
                project="p",
                objective="o",
                cwd=td,
                conversation_url=URL1,
            )
            worker_id = task.current_worker_id
            setup._conn.execute(
                "UPDATE tasks SET max_recovery_attempts=1 WHERE task_id='t1'"
            )
            setup._conn.commit()
            setup.close()

            barrier = threading.Barrier(2)
            results = []
            lock = threading.Lock()

            def contender(index):
                registry = Registry(db)
                try:
                    barrier.wait(timeout=2)
                    try:
                        registry.record_recovery_action_attempt(
                            action_attempt(attempt_id=f"act-{index}", worker_id=worker_id)
                        )
                        result = "armed"
                    except Exception as exc:  # exact losing fence may be budget or unresolved action
                        result = f"blocked:{type(exc).__name__}"
                    with lock:
                        results.append(result)
                finally:
                    registry.close()

            threads = [threading.Thread(target=contender, args=(i,)) for i in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            self.assertEqual(len(results), 2)
            self.assertEqual(results.count("armed"), 1)
            check = Registry(db)
            try:
                self.assertEqual(check.get_task("t1").recovery_attempts, 1)
                self.assertEqual(len(check.action_attempts("t1")), 1)
                self.assertIsNotNone(check.unresolved_action_attempt("t1"))
            finally:
                check.close()


class ReconciliationFenceRaceTests(unittest.TestCase):
    def build(self, *, continuation_pending=False, active_run_id="r1", status_hash="clean", created=NOW):
        task, assessment, browser, lsm, workspace = reconciliation_inputs(
            continuation_pending=continuation_pending,
            active_run_id=active_run_id,
            status_hash=status_hash,
        )
        return build_reconciliation_record(
            task,
            assessment,
            browser=browser,
            network=None,
            lsm=lsm,
            workspace=workspace,
            created_at=created,
        )

    def test_same_head_but_dirty_tree_change_invalidates_dispatch_fence(self):
        clean = self.build(status_hash="clean", created=NOW)
        dirty = self.build(status_hash="M:tracked-file", created=NOW + 10)
        self.assertFalse(fence_matches(clean, dirty))
        self.assertNotEqual(clean.fence_token, dirty.fence_token)

    def test_takeover_run_id_and_continuation_pending_each_invalidate_fence(self):
        base = self.build(active_run_id="r1", continuation_pending=False, created=NOW)
        takeover = self.build(active_run_id="r2", continuation_pending=False, created=NOW + 10)
        pending = self.build(active_run_id="r1", continuation_pending=True, created=NOW + 20)
        self.assertFalse(fence_matches(base, takeover))
        self.assertFalse(fence_matches(base, pending))


class LsmDurableEvidenceRaceTests(unittest.TestCase):
    def test_takeover_does_not_hide_fresh_inflight_lease_from_prior_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            (root / "sessions" / "s1.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_id": "s1",
                        "status": "active",
                        "active_run_id": "r2",
                        "plan": {
                            "status": "active",
                            "last_agent_activity": 9000,
                            "execution_lease_s": 900,
                            "continuation_pending": False,
                            "steps": [],
                        },
                        "in_flight_calls": {
                            "old-call": {
                                "run_id": "r1",
                                "started_at": 9950,
                                "heartbeat_at": 9950,
                            }
                        },
                        "activity": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "jobs.json").write_text(
                json.dumps({"version": 2, "jobs": []}), encoding="utf-8"
            )
            with patch("cws.lsm.time.time", return_value=10_000):
                observation = FileLsmTelemetry(root).observe(
                    task_id="t1", session_id="s1", tracked_job_ids=[]
                )
            self.assertEqual(observation.active_run_id, "r2")
            self.assertEqual(observation.in_flight_calls, 1)
            self.assertEqual(observation.freshest_in_flight_heartbeat, 9950)

    def test_retrying_job_uses_terminal_pending_attempt_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sessions").mkdir()
            status_path = root / "retry.status.json"
            status_path.write_text(
                json.dumps({"exit_code": 7, "completed_at": 9999.0, "error": "failed"}),
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
                                "status": "retrying",
                                "pending_status_path": str(status_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("cws.lsm.time.time", return_value=10_000):
                observation = FileLsmTelemetry(root).observe(
                    task_id="t1", session_id="s1", tracked_job_ids=["j1"]
                )
            self.assertEqual(observation.active_jobs, 0)
            self.assertEqual(observation.failed_jobs, 1)
            self.assertEqual(
                observation.raw["tracked_jobs"][0]["status_source"],
                "attempt_status",
            )


class ActionAcknowledgementAdversarialTests(unittest.TestCase):
    def observation(self, attempt, *, count=1, worker_id=None, observed_at=110.0):
        return UiaAckObservation(
            worker_id=worker_id or attempt.worker_id,
            observed_at=observed_at,
            url=URL1,
            window_handle=123,
            browser_pid=456,
            generating=False,
            send_button_ready=True,
            composer_present=True,
            signed_in_likely=True,
            nonce_occurrences=count,
            text_element_count=20,
            text_signature="post-signature",
        )

    def test_nonce_must_appear_exactly_once_and_in_the_correct_worker(self):
        attempt = action_attempt(state=ActionAttemptState.SUBMITTED)
        self.assertIsNone(
            acknowledgement_from_uia_observation(
                attempt, self.observation(attempt, count=0), max_nonce_occurrences=1
            )
        )
        self.assertIsNotNone(
            acknowledgement_from_uia_observation(
                attempt, self.observation(attempt, count=1), max_nonce_occurrences=1
            )
        )
        self.assertIsNone(
            acknowledgement_from_uia_observation(
                attempt, self.observation(attempt, count=2), max_nonce_occurrences=1
            )
        )
        self.assertIsNone(
            acknowledgement_from_uia_observation(
                attempt,
                self.observation(attempt, count=1, worker_id="wrong-worker"),
                max_nonce_occurrences=1,
            )
        )

    def test_stale_positive_ack_cannot_resolve_newer_unresolved_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            try:
                task = registry.register_task(
                    task_id="t1",
                    project="p",
                    objective="o",
                    cwd=td,
                    conversation_url=URL1,
                )
                attempt = action_attempt(
                    worker_id=task.current_worker_id,
                    state=ActionAttemptState.SUBMITTED,
                    created_at=100.0,
                )
                registry.record_action_attempt(attempt)
                stale = ActionAcknowledgement(
                    attempt_id=attempt.attempt_id,
                    worker_id=attempt.worker_id,
                    observed_at=99.0,
                    accepted=True,
                    kind="uia_nonce_hash",
                    evidence_hash="hash",
                )
                with self.assertRaises(ActionBlocked):
                    registry.acknowledge_action(stale)
                self.assertEqual(
                    registry.get_action_attempt(attempt.attempt_id).state,
                    ActionAttemptState.SUBMITTED,
                )
            finally:
                registry.close()


class ProbeIdentityAdversarialTests(unittest.TestCase):
    @patch("cws.page_runtime.os.name", "nt")
    def test_reuse_blocks_when_hwnd_or_pid_identity_changed(self):
        existing = probe_slot()
        plan = plan_probe_slot(worker(), existing, now=NOW)
        self.assertEqual(plan.action, ProbeSlotAction.REUSE)
        transport = ChromeUiaProbeWindowTransport(chrome_executable=CHROME, enabled=True)
        with patch.object(
            transport,
            "_find",
            return_value={
                "count": 1,
                "matches": [{"window_handle": 999, "browser_pid": 222}],
            },
        ):
            result = transport.execute(plan, existing=existing)
        self.assertFalse(result.changed)
        self.assertFalse(result.side_effect_possible)
        self.assertIn("identity changed", result.detail)

    @patch("cws.page_runtime.os.name", "nt")
    @patch("cws.page_runtime.subprocess.Popen")
    def test_rotation_never_opens_new_target_if_old_owner_tag_is_multiply_bound(self, popen):
        existing = probe_slot()
        plan = plan_probe_slot(worker(URL2, worker_id="w2"), existing, now=NOW)
        transport = ChromeUiaProbeWindowTransport(
            chrome_executable=CHROME,
            enabled=True,
            open_timeout_s=0.05,
        )
        with (
            patch.object(
                transport,
                "_close",
                return_value={
                    "closed": True,
                    "absent": False,
                    "ambiguous": False,
                    "detail": "close requested",
                },
            ),
            patch.object(
                transport,
                "_find",
                return_value={
                    "count": 2,
                    "matches": [
                        {"window_handle": 111, "browser_pid": 222},
                        {"window_handle": 333, "browser_pid": 444},
                    ],
                },
            ),
        ):
            result = transport.execute(plan, existing=existing)
        self.assertFalse(result.changed)
        self.assertTrue(result.side_effect_possible)
        self.assertIn("multiply bound", result.detail)
        popen.assert_not_called()

    def test_real_find_script_pid_collision_is_recorded_for_child_a(self):
        # Real 0.6 evidence: PowerShell $PID is automatic/read-only. Assigning `$pid=...`
        # raises VariableNotWritable on a genuine find call. Production ownership is 0.7-A.
        collision = re.search(r"(?im)\$pid\s*=", _FIND_SCRIPT)
        if collision:
            pytest.xfail(
                "Known 0.6 _FIND_SCRIPT VariableNotWritable defect; production fix owned by 0.7-A"
            )
        self.assertIsNone(collision)


class CapabilityClockContextTests(unittest.TestCase):
    def capability(self):
        return PageCapabilityRecord(
            capability_id="cap-1",
            kind=PageCapabilityKind.GENERATION,
            scope_host="chatgpt.com",
            browser_family="chrome",
            browser_major=151,
            platform="windows",
            surface="normal_chrome_uia",
            isolation_mode="fixture",
            evaluator_version="page-close-v2",
            evidence_digest="digest",
            source_experiment_id="exp",
            observed_at=100.0,
            recorded_at=101.0,
            expires_at=200.0,
        )

    def context(self):
        return CapabilityContext(
            scope_host="chatgpt.com",
            browser_family="chrome",
            browser_major=151,
            platform="windows",
            surface="normal_chrome_uia",
        )

    def test_clock_rollback_before_observation_is_not_fresh(self):
        ok, blockers = capability_matches_context(
            self.capability(),
            self.context(),
            expected_kind=PageCapabilityKind.GENERATION,
            now=99.0,
        )
        self.assertFalse(ok)
        self.assertIn("fresh", blockers)

    def test_context_surface_and_kind_mismatches_fail_closed(self):
        bad_context = replace(self.context(), surface="other-surface")
        ok, blockers = capability_matches_context(
            self.capability(),
            bad_context,
            expected_kind=PageCapabilityKind.TOOL_EXECUTION,
            now=150.0,
        )
        self.assertFalse(ok)
        self.assertIn("surface", blockers)
        self.assertIn("kind", blockers)


class KnownCrossChildDefectEvidenceTests(unittest.TestCase):
    def test_page_pool_failed_existing_role_change_should_be_transactional(self):
        pool = PagePool(max_active_pages=2, max_probe_pages=1)
        pool.register_page("active", role=PageRole.ACTIVE, worker_id="w1", now=1)
        pool.register_page("probe", role=PageRole.PROBE, worker_id=None, now=1)
        before = [(x.page_id, x.role, x.worker_id, x.last_used_at) for x in pool.leases()]
        with self.assertRaises(PagePoolError):
            pool.register_page("active", role=PageRole.PROBE, worker_id=None, now=2)
        after = [(x.page_id, x.role, x.worker_id, x.last_used_at) for x in pool.leases()]
        if after != before:
            pytest.xfail(
                "Known 0.6 PagePool rollback defect in probe-operations ownership area; owner 0.7-A"
            )
        self.assertEqual(after, before)

    def test_scheduler_duplicate_candidates_should_collapse_per_task(self):
        task = TaskRecord(
            task_id="t1",
            project="p",
            objective="o",
            cwd="C:/repo",
            state=SupervisorState.SUSPECT,
        )
        assessment = Assessment(
            state=SupervisorState.SUSPECT,
            reason="same candidate twice",
            confidence="high",
        )
        queue = attention_queue([(task, assessment), (task, assessment)])
        if len(queue) != 1:
            pytest.xfail(
                "Known 0.6 duplicate scheduler-candidate defect; orchestration owner 0.7-B"
            )
        self.assertEqual([item.task_id for item in queue], ["t1"])


if __name__ == "__main__":
    unittest.main()
