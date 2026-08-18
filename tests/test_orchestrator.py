import unittest

from cws.models import (
    BrowserObservation,
    LsmObservation,
    SupervisorState,
    TaskRecord,
    WorkerRecord,
    WorkerStatus,
)
from cws.orchestrator import (
    BrowserPoolPolicy,
    PageDisposition,
    classify_worker_for_pool,
    plan_browser_pool,
)
from cws.ram import BrowserMemoryObservation, SystemMemoryObservation


NOW = 1000.0


def task(state):
    return TaskRecord(
        task_id=f"t-{state.value.lower()}",
        project="p",
        objective="obj",
        cwd="C:/repo",
        state=state,
        current_worker_id="w1",
    )


def worker(task_id="t"):
    return WorkerRecord(
        worker_id="w1",
        task_id=task_id,
        conversation_url="https://chatgpt.com/c/x",
        conversation_id="x",
        status=WorkerStatus.ACTIVE,
        started_at=1,
    )


def lsm(**kw):
    base = dict(
        task_id="t",
        observed_at=NOW,
        session_id="s1",
        session_status="active",
        plan_status="active",
        in_flight_calls=0,
        active_jobs=0,
        continuation_pending=False,
    )
    base.update(kw)
    return LsmObservation(**base)


class BrowserPoolTests(unittest.TestCase):
    def test_live_lsm_work_is_never_close_candidate(self):
        t = task(SupervisorState.COMPLETED)
        item = classify_worker_for_pool(
            t,
            worker(t.task_id),
            browser=None,
            lsm=lsm(in_flight_calls=1),
            policy=BrowserPoolPolicy(),
        )
        self.assertEqual(item.disposition, PageDisposition.DO_NOT_CLOSE)
        self.assertFalse(item.close_allowed)

    def test_generating_browser_is_never_close_candidate(self):
        t = task(SupervisorState.BLOCKED)
        item = classify_worker_for_pool(
            t,
            worker(t.task_id),
            browser=BrowserObservation(worker_id="w1", observed_at=NOW, generating=True),
            lsm=lsm(plan_status="blocked"),
            policy=BrowserPoolPolicy(),
        )
        self.assertEqual(item.disposition, PageDisposition.DO_NOT_CLOSE)

    def test_completed_task_is_candidate_but_close_stays_disabled(self):
        t = task(SupervisorState.COMPLETED)
        item = classify_worker_for_pool(
            t,
            worker(t.task_id),
            browser=BrowserObservation(worker_id="w1", observed_at=NOW, generating=False),
            lsm=lsm(plan_status="completed"),
            policy=BrowserPoolPolicy(page_close_experiment_passed=False),
        )
        self.assertEqual(item.disposition, PageDisposition.PARK_CANDIDATE)
        self.assertFalse(item.close_allowed)

    def test_already_parked_worker_does_not_count_as_resident_page(self):
        t = task(SupervisorState.BLOCKED)
        parked = WorkerRecord(
            worker_id="w1",
            task_id=t.task_id,
            conversation_url="https://chatgpt.com/c/x",
            conversation_id="x",
            status=WorkerStatus.PARKED,
            started_at=1,
        )
        item = classify_worker_for_pool(
            t,
            parked,
            browser=None,
            lsm=lsm(plan_status="blocked"),
            policy=BrowserPoolPolicy(),
        )
        self.assertEqual(item.disposition, PageDisposition.NO_PAGE)
        plan = plan_browser_pool(
            [(t, parked, None, lsm(plan_status="blocked"))],
            system_memory=None,
            browser_memory=None,
            policy=BrowserPoolPolicy(),
            observed_at=NOW,
        )
        self.assertEqual(plan.active_worker_count, 0)

    def test_only_explicit_future_experiment_flag_can_allow_close(self):
        t = task(SupervisorState.COMPLETED)
        item = classify_worker_for_pool(
            t,
            worker(t.task_id),
            browser=None,
            lsm=lsm(plan_status="completed"),
            policy=BrowserPoolPolicy(page_close_experiment_passed=True),
        )
        self.assertTrue(item.close_allowed)

    def test_high_memory_pressure_prioritizes_park_candidates(self):
        completed = task(SupervisorState.COMPLETED)
        running = task(SupervisorState.RUNNING)
        rows = [
            (
                completed,
                worker(completed.task_id),
                None,
                lsm(plan_status="completed"),
            ),
            (
                running,
                worker(running.task_id),
                BrowserObservation(worker_id="w1", observed_at=NOW, generating=True),
                lsm(in_flight_calls=1),
            ),
        ]
        system = SystemMemoryObservation(
            observed_at=NOW,
            total_bytes=8 * 1024**3,
            available_bytes=512 * 1024**2,
            used_bytes=8 * 1024**3 - 512 * 1024**2,
            used_fraction=0.9375,
        )
        chrome = BrowserMemoryObservation(
            observed_at=NOW,
            process_name="chrome",
            process_count=20,
            total_working_set_bytes=2 * 1024**3,
            largest_working_set_bytes=500 * 1024**2,
            window_process_count=1,
        )
        plan = plan_browser_pool(
            rows,
            system_memory=system,
            browser_memory=chrome,
            policy=BrowserPoolPolicy(max_active_workers=1),
            observed_at=NOW,
        )
        self.assertEqual(plan.memory_pressure, "high")
        self.assertEqual(plan.park_candidate_count, 1)
        self.assertEqual(plan.pinned_worker_count, 1)
        self.assertEqual(plan.items[0].disposition, PageDisposition.PARK_CANDIDATE)
        self.assertTrue(any("memory pressure is high" in note for note in plan.recommendations))
        self.assertTrue(any("page-close" in note for note in plan.recommendations))

    def test_pinned_workers_over_limit_warn_but_are_not_closed(self):
        rows = []
        for index in range(3):
            t = TaskRecord(
                task_id=f"r{index}",
                project="p",
                objective="obj",
                cwd="C:/repo",
                state=SupervisorState.RUNNING,
                current_worker_id=f"w{index}",
            )
            w = WorkerRecord(
                worker_id=f"w{index}",
                task_id=t.task_id,
                conversation_url=f"https://chatgpt.com/c/{index}",
                conversation_id=str(index),
                status=WorkerStatus.ACTIVE,
                started_at=1,
            )
            rows.append((t, w, None, lsm(in_flight_calls=1)))
        plan = plan_browser_pool(
            rows,
            system_memory=None,
            browser_memory=None,
            policy=BrowserPoolPolicy(max_active_workers=2),
            observed_at=NOW,
        )
        self.assertEqual(plan.pinned_worker_count, 3)
        self.assertTrue(any("pinned" in warning for warning in plan.warnings))
        self.assertTrue(all(not item.close_allowed for item in plan.items))


if __name__ == "__main__":
    unittest.main()
