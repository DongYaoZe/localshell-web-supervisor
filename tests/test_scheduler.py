import unittest

from cws.models import Assessment, SupervisorState, TaskRecord
from cws.scheduler import attention_queue


def task(task_id: str) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        project="p",
        objective="o",
        cwd="C:/repo",
        state=SupervisorState.SUSPECT,
    )


class AttentionQueueTests(unittest.TestCase):
    def test_duplicate_task_candidates_collapse(self):
        item = task("t1")
        assessment = Assessment(
            state=SupervisorState.SUSPECT,
            reason="same candidate twice",
            confidence="high",
        )

        queue = attention_queue([(item, assessment), (item, assessment)])

        self.assertEqual([entry.task_id for entry in queue], ["t1"])

    def test_duplicate_task_keeps_highest_priority_assessment(self):
        item = task("t1")
        suspect = Assessment(
            state=SupervisorState.SUSPECT,
            reason="suspect",
            confidence="medium",
        )
        blocked = Assessment(
            state=SupervisorState.BLOCKED,
            reason="blocked",
            confidence="high",
        )

        queue = attention_queue([(item, suspect), (item, blocked)])

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].state, SupervisorState.BLOCKED)
        self.assertEqual(queue[0].reason, "blocked")

    def test_equal_priority_duplicate_is_input_order_independent(self):
        item = task("t1")
        a = Assessment(state=SupervisorState.SUSPECT, reason="z reason", confidence="high")
        b = Assessment(state=SupervisorState.SUSPECT, reason="a reason", confidence="high")

        forward = attention_queue([(item, a), (item, b)])
        reverse = attention_queue([(item, b), (item, a)])

        self.assertEqual(forward, reverse)
        self.assertEqual(forward[0].reason, "a reason")


if __name__ == "__main__":
    unittest.main()
