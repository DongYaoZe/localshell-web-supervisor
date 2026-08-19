from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit


class PageIsolationMode(StrEnum):
    """How the disposable ChatGPT experiment was isolated from user work."""

    DEDICATED_PROFILE = "dedicated_profile"
    EXISTING_PROFILE_DISPOSABLE_WINDOW = "existing_profile_disposable_window"
    COPIED_AUTH_PROFILE = "copied_auth_profile"


@dataclass(slots=True)
class PageCloseEvidence:
    experiment_id: str
    disposable_profile: bool
    normally_authenticated: bool
    auth_material_copied: bool
    pre_close_url: str
    reopened_url: str
    pre_close_generating: bool
    close_while_live_confirmed: bool
    background_progress_observed: bool
    completion_evidence_after_reopen: bool
    same_conversation_after_reopen: bool
    duplicate_turn_observed: bool
    auth_still_valid_after_reopen: bool
    pre_close_signature: str | None = None
    post_reopen_signature: str | None = None
    notes: list[str] = field(default_factory=list)
    # Backward-compatible default: 0.4 evidence meant a dedicated disposable profile.
    isolation_mode: str = PageIsolationMode.DEDICATED_PROFILE.value
    exact_window_binding_confirmed: bool = False
    current_user_conversation_excluded: bool = False
    # Tool-specific evidence is deliberately separate from ordinary model-generation parking.
    tool_execution_observed: bool = False
    tool_job_identity_confirmed: bool = False
    tool_running_at_close: bool = False
    tool_completed_after_close: bool = False
    tool_final_response_after_reopen: bool = False


@dataclass(slots=True)
class PageCloseEvaluation:
    experiment_id: str
    # Backward-compatible alias for the ordinary generation/page-continuity gate.
    parking_safe: bool
    generation_parking_safe: bool
    tool_execution_parking_safe: bool
    checks: dict[str, bool]
    blockers: list[str]
    tool_checks: dict[str, bool]
    tool_blockers: list[str]
    conclusion: str


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _isolation_checks(evidence: PageCloseEvidence) -> tuple[bool, bool, bool]:
    try:
        mode = PageIsolationMode(str(evidence.isolation_mode))
    except ValueError:
        return False, False, False

    supported = mode in {
        PageIsolationMode.DEDICATED_PROFILE,
        PageIsolationMode.EXISTING_PROFILE_DISPOSABLE_WINDOW,
    }
    dedicated_profile_ok = (
        mode == PageIsolationMode.DEDICATED_PROFILE and evidence.disposable_profile
    )
    disposable_window_ok = (
        mode == PageIsolationMode.EXISTING_PROFILE_DISPOSABLE_WINDOW
        and evidence.exact_window_binding_confirmed
        and evidence.current_user_conversation_excluded
    )
    return supported, dedicated_profile_ok, disposable_window_ok


def evaluate_page_close_evidence(evidence: PageCloseEvidence) -> PageCloseEvaluation:
    """Evaluate evidence for parking a ChatGPT worker by closing its page.

    This function never opens/closes a page. Evidence may come from either a normally
    authenticated dedicated profile or an exact-bound disposable window inside the user's
    existing authenticated Chrome profile. Copied-auth profiles, ambiguous windows,
    localhost fixtures, and anonymous experiments remain fail-closed.
    """
    supported_mode, dedicated_profile_ok, disposable_window_ok = _isolation_checks(evidence)
    checks = {
        "supported_isolation_mode": supported_mode,
        "isolated_execution_context": dedicated_profile_ok or disposable_window_ok,
        "normally_authenticated": evidence.normally_authenticated,
        "no_auth_material_copy": not evidence.auth_material_copied,
        "pre_close_conversation_url": "/c/" in _normalize_url(evidence.pre_close_url),
        "same_url_after_reopen": (
            _normalize_url(evidence.pre_close_url) == _normalize_url(evidence.reopened_url)
            and evidence.same_conversation_after_reopen
        ),
        "closed_while_live": evidence.pre_close_generating and evidence.close_while_live_confirmed,
        "background_progress_observed": evidence.background_progress_observed,
        "completion_after_reopen": evidence.completion_evidence_after_reopen,
        "signature_advanced": bool(
            evidence.pre_close_signature
            and evidence.post_reopen_signature
            and evidence.pre_close_signature != evidence.post_reopen_signature
        ),
        "no_duplicate_turn": not evidence.duplicate_turn_observed,
        "auth_survived_reopen": evidence.auth_still_valid_after_reopen,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    generation_parking_safe = not blockers

    tool_checks = {
        "generation_parking_safe": generation_parking_safe,
        "tool_execution_observed": evidence.tool_execution_observed,
        "tool_job_identity_confirmed": evidence.tool_job_identity_confirmed,
        "tool_running_at_close": evidence.tool_running_at_close,
        "tool_completed_after_close": evidence.tool_completed_after_close,
        "tool_final_response_after_reopen": evidence.tool_final_response_after_reopen,
    }
    tool_blockers = [name for name, passed in tool_checks.items() if not passed]
    tool_execution_parking_safe = not tool_blockers

    # Keep the 0.4 API stable: `parking_safe` continues to mean ordinary conversation/page
    # continuity. Callers that may park a worker during a live LSM tool chain must require
    # `tool_execution_parking_safe` explicitly.
    parking_safe = generation_parking_safe
    if tool_execution_parking_safe:
        conclusion = (
            "isolated authenticated evidence satisfies both generation and live-tool "
            "close/reopen parking gates"
        )
    elif generation_parking_safe:
        conclusion = (
            "generation/page parking is proven, but live-tool parking remains unproven for "
            "this evidence"
        )
    else:
        conclusion = (
            "page-close parking remains unproven for this evidence; keep live/ambiguous workers "
            "DO_NOT_CLOSE"
        )
    return PageCloseEvaluation(
        experiment_id=evidence.experiment_id,
        parking_safe=parking_safe,
        generation_parking_safe=generation_parking_safe,
        tool_execution_parking_safe=tool_execution_parking_safe,
        checks=checks,
        blockers=blockers,
        tool_checks=tool_checks,
        tool_blockers=tool_blockers,
        conclusion=conclusion,
    )
