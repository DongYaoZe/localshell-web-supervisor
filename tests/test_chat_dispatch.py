import tempfile
import time
import unittest
from pathlib import Path

from lws.chat_dispatch import (
    BrowserAckResult,
    BrowserCloseResult,
    BrowserSendResult,
    ChatDispatchEngine,
    ChatDispatchState,
    ChatDispatchStore,
    ChatPageState,
    PageIdentity,
    job_payload,
)


PROJECT = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-project"
URL1 = PROJECT + "/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL2 = PROJECT + "/c/bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class FakeBrowser:
    chrome_executable = CHROME

    def __init__(self):
        self.visible = {}
        self.recovered_new = None
        self.open_existing_calls = 0
        self.open_new_calls = 0
        self.send_existing_calls = 0
        self.send_new_calls = 0
        self.close_calls = 0
        self.next_hwnd = 1000
        self.send_result = BrowserSendResult(True, True, "submitted")
        self.new_send_url = URL1
        self.ack_by_dispatch = {}

    def _identity(self, url, *, owned):
        self.next_hwnd += 1
        return PageIdentity(url, self.next_hwnd, 77, CHROME, owned)

    def find_existing(self, conversation_url):
        return self.visible.get(conversation_url)

    def open_existing(self, conversation_url):
        self.open_existing_calls += 1
        identity = self._identity(conversation_url, owned=True)
        self.visible[conversation_url] = identity
        return identity

    def open_new(self, project_url, owner_token, prompt_sha256):
        self.open_new_calls += 1
        return self._identity(project_url + "#lws-child=" + owner_token, owned=True)

    def recover_new(self, project_url, owner_token, prompt_sha256):
        return self.recovered_new

    def send_existing(self, job, page):
        self.send_existing_calls += 1
        result = self.send_result
        return BrowserSendResult(
            result.submitted,
            result.side_effect_possible,
            result.detail,
            conversation_url=page.conversation_url,
            rate_limited=result.rate_limited,
        )

    def send_new(self, job, page):
        self.send_new_calls += 1
        result = self.send_result
        return BrowserSendResult(
            result.submitted,
            result.side_effect_possible,
            result.detail,
            conversation_url=self.new_send_url if result.submitted else None,
            rate_limited=result.rate_limited,
        )

    def observe_ack(self, job, page):
        return self.ack_by_dispatch.get(
            job.dispatch_id,
            BrowserAckResult(True, 1, False, "ack", conversation_url=page.conversation_url),
        )

    def current_url(self, page):
        return page.conversation_url

    def close_page(self, page):
        self.close_calls += 1
        return BrowserCloseResult(True, False, False, "closed")


class ChatDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ChatDispatchStore(Path(self.tmp.name) / "queue.sqlite3")
        self.browser = FakeBrowser()
        self.engine = ChatDispatchEngine(
            self.store,
            self.browser,
            max_windows=2,
            idle_close_s=30,
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def enqueue_existing(self, *, key="child-1", url=URL1, prompt="work", idem=None):
        return self.store.enqueue(
            prompt=prompt,
            conversation_key=key,
            conversation_url=url,
            dispatch_key=idem,
            idle_close_s=30,
            max_windows=2,
        )

    def test_idempotency_key_returns_same_job_and_payload_hides_prompt(self):
        first = self.enqueue_existing(idem="request-1", prompt="secret prompt body")
        second = self.enqueue_existing(idem="request-1", prompt="secret prompt body")
        self.assertEqual(first.dispatch_id, second.dispatch_id)
        payload = job_payload(first)
        self.assertNotIn("prompt_text", payload)
        self.assertEqual(payload["prompt_bytes"], len("secret prompt body".encode()))
        with self.assertRaises(ValueError):
            self.enqueue_existing(idem="request-1", prompt="different")

    def test_borrowed_existing_page_is_sent_acknowledged_and_never_closed(self):
        borrowed = self.browser._identity(URL1, owned=False)
        self.browser.visible[URL1] = borrowed
        job = self.enqueue_existing()
        self.engine.run_once()
        self.assertEqual(self.store.get_job(job.dispatch_id).state, ChatDispatchState.SUBMITTED)
        self.assertEqual(self.browser.send_existing_calls, 1)
        self.engine.run_once()
        self.assertEqual(self.store.get_job(job.dispatch_id).state, ChatDispatchState.ACKNOWLEDGED)
        page = self.store.page_for_conversation("child-1")
        self.assertIsNotNone(page)
        self.assertFalse(page.owned)
        self.assertEqual(page.state, ChatPageState.IDLE)
        self.engine.run_once()
        self.assertEqual(self.browser.close_calls, 0)
        self.assertTrue(self.engine.should_exit())

    def test_owned_existing_page_closes_only_after_queue_drains_and_idle_grace(self):
        job = self.enqueue_existing()
        self.engine.run_once()
        self.assertEqual(self.browser.open_existing_calls, 1)
        self.engine.run_once()
        page = self.store.page_for_conversation("child-1")
        self.assertTrue(page.owned)
        self.assertEqual(page.state, ChatPageState.IDLE)
        self.assertIsNotNone(page.close_after_at)
        self.assertEqual(self.browser.close_calls, 0)
        self.store._conn.execute(
            "UPDATE chat_dispatch_pages SET close_after_at=0 WHERE page_id=?", (page.page_id,)
        )
        self.store._conn.commit()
        self.engine.run_once()
        self.assertEqual(self.browser.close_calls, 1)
        self.assertEqual(self.store.get_page(page.page_id).state, ChatPageState.CLOSED)
        self.assertTrue(self.engine.should_exit())

    def test_unproven_idle_close_becomes_terminally_ambiguous(self):
        job = self.enqueue_existing()
        self.engine.run_once()
        self.engine.run_once()
        page = self.store.page_for_conversation("child-1")
        self.assertTrue(page.owned)
        self.store._conn.execute(
            "UPDATE chat_dispatch_pages SET close_after_at=0 WHERE page_id=?", (page.page_id,)
        )
        self.store._conn.commit()
        self.browser.close_page = lambda _page: BrowserCloseResult(
            False, False, False, "close helper failed before observation"
        )

        self.engine.run_once()

        closed = self.store.get_page(page.page_id)
        self.assertEqual(closed.state, ChatPageState.AMBIGUOUS)
        self.assertIn("failed before observation", closed.last_error)
        self.assertTrue(self.engine.should_exit())

    def test_reusing_idle_owned_page_cancels_old_close_timer(self):
        first = self.enqueue_existing(prompt="one")
        self.engine.run_once()
        self.engine.run_once()
        page = self.store.page_for_conversation("child-1")
        self.assertEqual(page.state, ChatPageState.IDLE)
        old_close = page.close_after_at
        second = self.enqueue_existing(prompt="two")
        self.engine.run_once()
        page = self.store.page_for_conversation("child-1")
        self.assertEqual(page.state, ChatPageState.OPEN)
        self.assertIsNone(page.close_after_at)
        self.assertNotEqual(self.store.get_job(first.dispatch_id).dispatch_id, second.dispatch_id)
        self.assertEqual(self.browser.send_existing_calls, 2)
        self.assertIsNotNone(old_close)

    def test_same_conversation_serializes_prompts(self):
        first = self.enqueue_existing(prompt="one")
        second = self.enqueue_existing(prompt="two")
        self.browser.ack_by_dispatch[first.dispatch_id] = BrowserAckResult(
            True, 1, True, "still generating", conversation_url=URL1
        )
        self.engine.run_once()
        self.assertEqual(self.browser.send_existing_calls, 1)
        self.assertEqual(self.store.get_job(second.dispatch_id).state, ChatDispatchState.QUEUED)
        self.engine.run_once()
        self.assertEqual(self.browser.send_existing_calls, 1)
        self.browser.ack_by_dispatch[first.dispatch_id] = BrowserAckResult(
            True, 1, False, "done", conversation_url=URL1
        )
        self.engine.run_once()
        self.engine.run_once()
        self.assertEqual(self.browser.send_existing_calls, 2)

    def test_ambiguous_send_is_never_replayed(self):
        self.browser.send_result = BrowserSendResult(False, True, "ambiguous")
        job = self.enqueue_existing()
        self.engine.run_once()
        self.assertEqual(
            self.store.get_job(job.dispatch_id).state,
            ChatDispatchState.RECONCILE_REQUIRED,
        )
        self.assertEqual(self.browser.send_existing_calls, 1)
        self.browser.ack_by_dispatch[job.dispatch_id] = BrowserAckResult(
            True, 0, False, "nonce absent", conversation_url=URL1
        )
        for _ in range(3):
            self.engine.run_once()
        self.assertEqual(self.browser.send_existing_calls, 1)

    def test_submitting_state_reconciles_without_replaying_send(self):
        borrowed = self.browser._identity(URL1, owned=False)
        self.browser.visible[URL1] = borrowed
        job = self.enqueue_existing()
        page = self.store.upsert_page(conversation_key="child-1", identity=borrowed)
        self.store.update_job(
            job.dispatch_id,
            state=ChatDispatchState.SUBMITTING,
            window_handle=page.window_handle,
            browser_pid=page.browser_pid,
            chrome_executable=page.chrome_executable,
        )
        self.browser.ack_by_dispatch[job.dispatch_id] = BrowserAckResult(
            True, 1, False, "already delivered", conversation_url=URL1
        )
        self.engine.run_once()
        self.assertEqual(self.browser.send_existing_calls, 0)
        self.assertEqual(
            self.store.get_job(job.dispatch_id).state,
            ChatDispatchState.ACKNOWLEDGED,
        )

    def test_opening_state_reconciles_without_replaying_existing_window_open(self):
        job = self.enqueue_existing()
        self.store.update_job(job.dispatch_id, state=ChatDispatchState.OPENING)
        self.engine.run_once()
        self.assertEqual(self.browser.open_existing_calls, 0)
        self.assertEqual(
            self.store.get_job(job.dispatch_id).state,
            ChatDispatchState.RECONCILE_REQUIRED,
        )

    def test_opening_state_recovers_exact_tagged_new_window(self):
        job = self.store.enqueue(
            prompt="new child",
            conversation_key="new-child",
            project_url=PROJECT,
        )
        self.store.update_job(job.dispatch_id, state=ChatDispatchState.OPENING)
        self.browser.recovered_new = self.browser._identity(PROJECT + "#owned", owned=True)
        self.engine.run_once()
        self.assertEqual(self.browser.open_new_calls, 0)
        self.assertEqual(self.browser.send_new_calls, 1)
        self.assertEqual(self.store.get_job(job.dispatch_id).state, ChatDispatchState.SUBMITTED)

    def test_new_conversation_persists_url_mapping_for_followup_key(self):
        first = self.store.enqueue(
            prompt="new child",
            conversation_key="new-child",
            project_url=PROJECT,
        )
        self.engine.run_once()
        self.assertEqual(self.browser.open_new_calls, 1)
        saved = self.store.get_job(first.dispatch_id)
        self.assertEqual(saved.state, ChatDispatchState.SUBMITTED)
        self.assertEqual(saved.conversation_url, URL1)
        self.engine.run_once()
        self.assertEqual(self.store.get_job(first.dispatch_id).state, ChatDispatchState.ACKNOWLEDGED)
        second = self.store.enqueue(prompt="followup", conversation_key="new-child")
        self.assertEqual(second.conversation_url, URL1)

    def test_window_pool_bounds_different_conversations(self):
        engine = ChatDispatchEngine(self.store, self.browser, max_windows=1, idle_close_s=30)
        first = self.enqueue_existing(key="one", url=URL1)
        second = self.enqueue_existing(key="two", url=URL2)
        engine.run_once()
        self.assertEqual(self.store.get_job(first.dispatch_id).state, ChatDispatchState.SUBMITTED)
        self.assertEqual(self.store.get_job(second.dispatch_id).state, ChatDispatchState.QUEUED)
        self.assertEqual(self.browser.open_existing_calls, 1)

    def test_stale_reconcile_required_does_not_keep_worker_alive_forever(self):
        job = self.enqueue_existing()
        identity = self.browser._identity(URL1, owned=True)
        self.store.upsert_page(conversation_key="child-1", identity=identity)
        self.store.update_job(
            job.dispatch_id,
            state=ChatDispatchState.RECONCILE_REQUIRED,
            last_error="ambiguous",
        )
        self.store._conn.execute(
            "UPDATE chat_dispatch_jobs SET updated_at=? WHERE dispatch_id=?",
            (time.time() - 120, job.dispatch_id),
        )
        self.store._conn.commit()
        self.assertTrue(self.engine.should_exit())
        self.assertEqual(self.browser.close_calls, 0)


if __name__ == "__main__":
    unittest.main()
