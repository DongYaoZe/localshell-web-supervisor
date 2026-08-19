import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cws.capabilities import (
    CapabilityProvenanceError,
    PAGE_CLOSE_EVALUATOR_VERSION,
    build_page_close_capabilities,
    capability_matches_context,
    runtime_context,
)
from cws.lifecycle import PageCloseEvidence
from cws.models import PageCapabilityKind
from cws.registry import Registry

URL = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def evidence(*, tool=False, observed_at=100.0, browser_major=151):
    return PageCloseEvidence(
        experiment_id="exp-1",
        disposable_profile=False,
        normally_authenticated=True,
        auth_material_copied=False,
        pre_close_url=URL,
        reopened_url=URL,
        pre_close_generating=True,
        close_while_live_confirmed=True,
        background_progress_observed=True,
        completion_evidence_after_reopen=True,
        same_conversation_after_reopen=True,
        duplicate_turn_observed=False,
        auth_still_valid_after_reopen=True,
        pre_close_signature="SIG_BEFORE_PRIVATE_123",
        post_reopen_signature="SIG_AFTER_PRIVATE_456",
        isolation_mode="existing_profile_disposable_window",
        exact_window_binding_confirmed=True,
        current_user_conversation_excluded=True,
        tool_execution_observed=tool,
        tool_job_identity_confirmed=tool,
        tool_running_at_close=tool,
        tool_completed_after_close=tool,
        tool_final_response_after_reopen=tool,
        observed_at=observed_at,
        scope_host="chatgpt.com",
        browser_family="chrome",
        browser_major=browser_major,
        platform="windows",
        surface="normal_chrome_uia",
    )


class PageCapabilityTests(unittest.TestCase):
    def test_generation_evidence_creates_one_versioned_record(self):
        rows = build_page_close_capabilities(evidence(), ttl_s=100, recorded_at=150)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.kind, PageCapabilityKind.GENERATION)
        self.assertEqual(row.evaluator_version, PAGE_CLOSE_EVALUATOR_VERSION)
        self.assertEqual(row.observed_at, 100)
        self.assertEqual(row.expires_at, 200)
        self.assertEqual(row.browser_major, 151)
        self.assertNotIn("before", row.evidence_digest)

    def test_tool_evidence_creates_distinct_generation_and_tool_records(self):
        rows = build_page_close_capabilities(evidence(tool=True), ttl_s=100, recorded_at=150)
        self.assertEqual(
            {row.kind for row in rows},
            {PageCapabilityKind.GENERATION, PageCapabilityKind.TOOL_EXECUTION},
        )
        self.assertEqual(len({row.capability_id for row in rows}), 2)
        self.assertEqual(len({row.evidence_digest for row in rows}), 1)

    def test_same_evidence_has_deterministic_capability_id(self):
        first = build_page_close_capabilities(evidence(), ttl_s=100, recorded_at=150)[0]
        second = build_page_close_capabilities(evidence(), ttl_s=100, recorded_at=160)[0]
        self.assertEqual(first.capability_id, second.capability_id)
        self.assertEqual(first.evidence_digest, second.evidence_digest)

    def test_missing_or_future_provenance_fails_closed(self):
        with self.assertRaisesRegex(CapabilityProvenanceError, "observed_at"):
            build_page_close_capabilities(evidence(observed_at=None), recorded_at=150)
        with self.assertRaisesRegex(CapabilityProvenanceError, "browser_major"):
            build_page_close_capabilities(
                replace(evidence(), browser_major=None),
                recorded_at=150,
            )
        with self.assertRaisesRegex(CapabilityProvenanceError, "future"):
            build_page_close_capabilities(evidence(observed_at=500), recorded_at=150)

    def test_context_match_requires_same_major_evaluator_and_freshness(self):
        row = build_page_close_capabilities(evidence(), ttl_s=100, recorded_at=150)[0]
        ok, blockers = capability_matches_context(
            row,
            runtime_context(browser_major=151),
            expected_kind=PageCapabilityKind.GENERATION,
            now=199,
        )
        self.assertTrue(ok)
        self.assertEqual(blockers, [])

        ok, blockers = capability_matches_context(
            row,
            runtime_context(browser_major=152),
            expected_kind=PageCapabilityKind.GENERATION,
            now=199,
        )
        self.assertFalse(ok)
        self.assertIn("browser_major", blockers)

        stale = replace(row, evaluator_version="old-evaluator")
        ok, blockers = capability_matches_context(
            stale,
            runtime_context(browser_major=151),
            expected_kind=PageCapabilityKind.GENERATION,
            now=199,
        )
        self.assertFalse(ok)
        self.assertIn("evaluator_version", blockers)

        ok, blockers = capability_matches_context(
            row,
            runtime_context(browser_major=151),
            expected_kind=PageCapabilityKind.GENERATION,
            now=200,
        )
        self.assertFalse(ok)
        self.assertIn("fresh", blockers)

    def test_registry_persists_only_sanitized_capability_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            registry = Registry(Path(td) / "registry.sqlite3")
            try:
                row = build_page_close_capabilities(
                    evidence(tool=True), ttl_s=100, recorded_at=150
                )[0]
                saved = registry.record_page_capability(row)
                self.assertEqual(saved.capability_id, row.capability_id)
                loaded = registry.page_capabilities(kind=PageCapabilityKind.GENERATION)
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0].evidence_digest, row.evidence_digest)
                payload = registry._conn.execute(
                    "SELECT payload_json FROM page_capabilities WHERE capability_id=?",
                    (row.capability_id,),
                ).fetchone()[0]
                self.assertNotIn("SIG_BEFORE_PRIVATE_123", payload)
                self.assertNotIn("SIG_AFTER_PRIVATE_456", payload)
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
