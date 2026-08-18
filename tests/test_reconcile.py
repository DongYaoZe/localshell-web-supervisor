import tempfile
import unittest
from pathlib import Path

from cws.models import (
    Assessment,
    BrowserObservation,
    LsmObservation,
    NetworkObservation,
    SupervisorState,
    TaskRecord,
    WorkspaceObservation,
)
from cws.reconcile import build_reconciliation_record, fence_matches
from cws.registry import Registry


NOW = 1000.0


def make_task(checkpoint=None):
    return TaskRecord(
        task_id="t1",
        project="p",
        objective="obj",
        cwd="C:/repo",
        state=SupervisorState.RECONCILING,
        lsm_session_id="s1",
        checkpoint=checkpoint or {"step": "after commit"},
        current_worker_id="w1",
        recovery_attempts=0,
        max_recovery_attempts=3,
        created_at=1,
        updated_at=1,
    )


def make_assessment():
    return Assessment(
        SupervisorState.RECONCILING,
        "conflicting delivery evidence",
        "high",
        evidence=["browser.error=delivery timeout", "lsm.in_flight=0"],
        requires_reconcile=True,
    )


def make_browser(signature="sig1"):
    return BrowserObservation(
        worker_id="w1",
        observed_at=NOW,
        url="https://chatgpt.com/c/x",
        generating=False,
        send_button_ready=True,
        pending_tool_calls=1,
        visible_error="Message delivery timed out",
        last_dom_change_at=NOW - 20,
        message_signature=signature,
        raw={"text_tail": "must-not-be-copied-into-reconcile"},
    )


def make_network(quiet_since=NOW - 30):
    return NetworkObservation(
        worker_id="w1",
        observed_at=NOW,
        source="cdp",
        sample_started_at=NOW - 2,
        sample_ended_at=NOW,
        page_url="https://chatgpt.com/c/x",
        event_count=0,
        last_activity_at=NOW - 30,
        quiet_since_at=quiet_since,
        raw={"headers": "must-not-be-copied-into-reconcile"},
    )


def make_lsm(run_id="r1"):
    return LsmObservation(
        task_id="t1",
        observed_at=NOW,
        session_id="s1",
        session_status="active",
        active_run_id=run_id,
        plan_status="active",
        continuation_due=False,
        continuation_pending=False,
        in_flight_calls=0,
        active_jobs=0,
        recent_event_type="tool.completed",
        recent_event_at=NOW - 30,
        raw={"activity": "must-not-be-copied-into-reconcile"},
    )


def make_workspace(head="abc"):
    return WorkspaceObservation(
        task_id="t1",
        observed_at=NOW,
        cwd="C:/repo",
        cwd_exists=True,
        is_git_repo=True,
        git_root="C:/repo",
        git_head=head,
        git_dirty=False,
        git_status_hash="status-hash",
        git_status_entries=["sensitive/path.txt"],
    )


class ReconciliationTests(unittest.TestCase):
    def test_same_world_state_has_same_fence(self):
        a = build_reconciliation_record(
            make_task(),
            make_assessment(),
            browser=make_browser(),
            network=make_network(),
            lsm=make_lsm(),
            workspace=make_workspace(),
            created_at=NOW,
        )
        b = build_reconciliation_record(
            make_task(),
            make_assessment(),
            browser=make_browser(),
            network=make_network(),
            lsm=make_lsm(),
            workspace=make_workspace(),
            created_at=NOW + 100,
        )
        self.assertNotEqual(a.reconcile_id, b.reconcile_id)
        self.assertEqual(a.fence_token, b.fence_token)
        self.assertTrue(fence_matches(a, b))

    def test_git_or_worker_evidence_change_invalidates_fence(self):
        base = build_reconciliation_record(
            make_task(),
            make_assessment(),
            browser=make_browser(),
            network=make_network(),
            lsm=make_lsm(),
            workspace=make_workspace("abc"),
            created_at=NOW,
        )
        git_changed = build_reconciliation_record(
            make_task(),
            make_assessment(),
            browser=make_browser(),
            network=make_network(),
            lsm=make_lsm(),
            workspace=make_workspace("def"),
            created_at=NOW + 1,
        )
        browser_changed = build_reconciliation_record(
            make_task(),
            make_assessment(),
            browser=make_browser("sig2"),
            network=make_network(),
            lsm=make_lsm(),
            workspace=make_workspace("abc"),
            created_at=NOW + 2,
        )
        self.assertNotEqual(base.fence_token, git_changed.fence_token)
        self.assertNotEqual(base.fence_token, browser_changed.fence_token)

    def test_snapshot_excludes_raw_sensitive_payloads(self):
        record = build_reconciliation_record(
            make_task(),
            make_assessment(),
            browser=make_browser(),
            network=make_network(),
            lsm=make_lsm(),
            workspace=make_workspace(),
            created_at=NOW,
        )
        rendered = repr(record.snapshot)
        self.assertNotIn("must-not-be-copied", rendered)
        self.assertNotIn("sensitive/path.txt", rendered)
        self.assertNotIn("headers", rendered)
        self.assertNotIn("must-not-be-copied-into-reconcile", rendered)

    def test_registry_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "cws.sqlite3")
            try:
                registry.register_task(
                    task_id="t1",
                    project="p",
                    objective="obj",
                    cwd="C:/repo",
                    lsm_session_id="s1",
                    conversation_url="https://chatgpt.com/c/x",
                )
                task = registry.get_task("t1")
                record = build_reconciliation_record(
                    task,
                    make_assessment(),
                    browser=None,
                    network=None,
                    lsm=make_lsm(),
                    workspace=make_workspace(),
                    created_at=NOW,
                )
                registry.record_reconciliation(record)
                loaded = registry.latest_reconciliation("t1")
                self.assertEqual(loaded.reconcile_id, record.reconcile_id)
                self.assertEqual(loaded.fence_token, record.fence_token)
                history = registry.reconciliation_history("t1", 5)
                self.assertEqual([row.reconcile_id for row in history], [record.reconcile_id])
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
