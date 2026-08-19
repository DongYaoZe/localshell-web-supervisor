from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


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


@dataclass(slots=True)
class PageCloseEvaluation:
    experiment_id: str
    parking_safe: bool
    checks: dict[str, bool]
    blockers: list[str]
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


def evaluate_page_close_evidence(evidence: PageCloseEvidence) -> PageCloseEvaluation:
    """Evaluate evidence for parking a live ChatGPT worker by closing its page.

    This function never opens/closes a page. It deliberately requires authenticated,
    disposable, ChatGPT-specific evidence; localhost or anonymous experiments cannot pass.
    """
    checks = {
        "disposable_profile": evidence.disposable_profile,
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
    parking_safe = not blockers
    if parking_safe:
        conclusion = (
            "isolated authenticated ChatGPT evidence satisfies the minimum live-page "
            "close/reopen parking gate"
        )
    else:
        conclusion = (
            "page-close parking remains unproven; keep live/ambiguous workers DO_NOT_CLOSE"
        )
    return PageCloseEvaluation(
        experiment_id=evidence.experiment_id,
        parking_safe=parking_safe,
        checks=checks,
        blockers=blockers,
        conclusion=conclusion,
    )
