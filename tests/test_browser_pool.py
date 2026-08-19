import unittest

from cws.browser_pool import PagePool, PagePoolError, PageRole, ProbeResourcePolicy


class PagePoolTests(unittest.TestCase):
    def test_capacity_fails_closed_without_eviction(self):
        pool = PagePool(max_active_pages=1, max_probe_pages=1)
        pool.register_page("a", role=PageRole.ACTIVE, worker_id="w1", now=1)
        with self.assertRaises(PagePoolError):
            pool.register_page("b", role=PageRole.ACTIVE, worker_id="w2", now=2)
        self.assertEqual([lease.page_id for lease in pool.leases()], ["a"])

    def test_probe_capacity_fails_closed_without_eviction(self):
        pool = PagePool(max_active_pages=2, max_probe_pages=1)
        pool.register_page("probe1", role=PageRole.PROBE, worker_id=None, now=1)
        with self.assertRaises(PagePoolError):
            pool.register_page("probe2", role=PageRole.PROBE, worker_id=None, now=2)
        self.assertEqual([lease.page_id for lease in pool.leases()], ["probe1"])

    def test_failed_existing_role_change_rolls_back_entire_lease(self):
        pool = PagePool(max_active_pages=2, max_probe_pages=1)
        original = pool.register_page("active", role=PageRole.ACTIVE, worker_id="w1", now=1)
        pool.register_page("probe", role=PageRole.PROBE, worker_id=None, now=1)

        with self.assertRaises(PagePoolError):
            pool.register_page("active", role=PageRole.PROBE, worker_id=None, now=2)

        current = next(lease for lease in pool.leases() if lease.page_id == "active")
        self.assertIs(current, original)
        self.assertEqual(current.role, PageRole.ACTIVE)
        self.assertEqual(current.worker_id, "w1")
        self.assertEqual(current.last_used_at, 1)

    def test_release_only_forgets_already_closed_page(self):
        pool = PagePool()
        pool.register_page("a", role=PageRole.ACTIVE, worker_id="w1", now=1)
        released = pool.release_page("a")
        self.assertEqual(released.page_id, "a")
        self.assertEqual(pool.leases(), [])

    def test_probe_scheduler_prioritizes_priority_then_oldest(self):
        pool = PagePool()
        pool.register_probe_target("w1", "https://chatgpt.com/c/1", priority=0, last_probed_at=10)
        pool.register_probe_target("w2", "https://chatgpt.com/c/2", priority=1, last_probed_at=20)
        pool.register_probe_target("w3", "https://chatgpt.com/c/3", priority=1, last_probed_at=None)
        self.assertEqual(pool.next_probe_target(now=30).worker_id, "w3")
        pool.mark_probed("w3", now=30)
        self.assertEqual(pool.next_probe_target(now=31).worker_id, "w2")

    def test_probe_resource_policy_blocks_only_large_visual_types(self):
        policy = ProbeResourcePolicy()
        for resource_type in ("image", "media", "font"):
            self.assertTrue(policy.should_block(resource_type))
        for resource_type in ("document", "script", "stylesheet", "xhr", "fetch", "websocket"):
            self.assertFalse(policy.should_block(resource_type))


if __name__ == "__main__":
    unittest.main()
