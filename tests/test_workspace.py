import subprocess
import tempfile
import unittest
from pathlib import Path

from cws.models import LsmObservation, SupervisorState, TaskRecord, WorkspaceObservation
from cws.recovery import recommend
from cws.watcher import assess
from cws.workspace import WorkspaceProbe, detect_git_bin


class WorkspaceProbeTests(unittest.TestCase):
    def test_missing_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "gone"
            obs = WorkspaceProbe().observe(task_id="t1", cwd=str(missing))
            self.assertFalse(obs.cwd_exists)
            self.assertIn("does not exist", obs.error)

    @unittest.skipUnless(detect_git_bin(), "git is required for integration fixture")
    def test_git_head_and_dirty_status(self):
        git = detect_git_bin()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            subprocess.run([git, "init", str(root)], check=True, capture_output=True, text=True)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            subprocess.run([git, "-C", str(root), "add", "a.txt"], check=True)
            subprocess.run(
                [
                    git,
                    "-C",
                    str(root),
                    "-c",
                    "user.name=CWS Test",
                    "-c",
                    "user.email=cws-test@example.invalid",
                    "commit",
                    "-m",
                    "initial",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            probe = WorkspaceProbe(git_bin=git)
            clean = probe.observe(task_id="t1", cwd=str(root))
            self.assertTrue(clean.cwd_exists)
            self.assertTrue(clean.is_git_repo)
            self.assertEqual(len(clean.git_head), 40)
            self.assertFalse(clean.git_dirty)
            clean_hash = clean.git_status_hash

            (root / "a.txt").write_text("two\n", encoding="utf-8")
            dirty = probe.observe(task_id="t1", cwd=str(root))
            self.assertTrue(dirty.git_dirty)
            self.assertNotEqual(dirty.git_status_hash, clean_hash)
            self.assertTrue(any("a.txt" in line for line in dirty.git_status_entries))

    @unittest.skipUnless(detect_git_bin(), "git is required for integration fixture")
    def test_unborn_git_repo_is_still_a_repo(self):
        git = detect_git_bin()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            subprocess.run([git, "init", str(root)], check=True, capture_output=True, text=True)
            obs = WorkspaceProbe(git_bin=git).observe(task_id="t1", cwd=str(root))
            self.assertTrue(obs.is_git_repo)
            self.assertIsNone(obs.git_head)


class WorkspaceAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.task = TaskRecord(
            task_id="t1",
            project="p",
            objective="obj",
            cwd="C:/missing",
            state=SupervisorState.SUSPECT,
            lsm_session_id="s1",
            checkpoint={"git_head": "abc"},
        )
        self.missing = WorkspaceObservation(
            task_id="t1",
            observed_at=100,
            cwd="C:/missing",
            cwd_exists=False,
            error="working directory does not exist",
        )

    def test_missing_workspace_requires_human_without_live_lsm_work(self):
        result = assess(self.task, None, None, workspace=self.missing, now=100)
        self.assertEqual(result.state, SupervisorState.NEEDS_HUMAN)
        rec = recommend(self.task, result, None, self.missing)
        self.assertEqual(rec.action, "human_decision")
        self.assertFalse(rec.safe_to_dispatch)

    def test_live_lsm_work_still_wins_over_missing_workspace(self):
        lsm = LsmObservation(
            task_id="t1",
            observed_at=100,
            session_id="s1",
            session_status="active",
            plan_status="active",
            in_flight_calls=1,
        )
        result = assess(self.task, None, lsm, workspace=self.missing, now=100)
        self.assertEqual(result.state, SupervisorState.RUNNING)

    def test_abandoned_task_never_gets_continue_recommendation(self):
        abandoned = TaskRecord(
            task_id="t1",
            project="p",
            objective="obj",
            cwd="C:/repo",
            state=SupervisorState.ABANDONED,
            lsm_session_id="s1",
        )
        result = assess(abandoned, None, None, now=100)
        rec = recommend(abandoned, result, None)
        self.assertEqual(rec.action, "none")
        self.assertFalse(rec.safe_to_dispatch)


if __name__ == "__main__":
    unittest.main()
