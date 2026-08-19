from __future__ import annotations

import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import ProbeMutationOperation, ProbeMutationState
from .probe_ops import ProbeMutationObservation, ProbeWindowMatch, UNRESOLVED_PROBE_MUTATION_STATES

MAX_EVIDENCE_BYTES = 32 * 1024
MAX_MATCHES_PER_TARGET = 8
MAX_IDENTITY_TEXT = 2048
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "operation_id",
        "owner_token",
        "observed_at",
        "complete",
        "old_matches",
        "new_matches",
    }
)
_REQUIRED_TOP_LEVEL = _ALLOWED_TOP_LEVEL
_ALLOWED_MATCH_KEYS = frozenset(
    {"window_handle", "browser_pid", "chrome_executable", "actual_url"}
)


class ProbeOperatorEvidenceError(ValueError):
    pass


def classify_probe_operation(operation: ProbeMutationOperation | None) -> str:
    if operation is None:
        return "NONE"
    if operation.state == ProbeMutationState.COMPLETED:
        return "COMPLETED"
    if operation.state == ProbeMutationState.RECONCILE_REQUIRED:
        return "BLOCKED"
    if operation.state in UNRESOLVED_PROBE_MUTATION_STATES:
        return "UNRESOLVED"
    return "TERMINAL"


def probe_operation_payload(
    operation: ProbeMutationOperation | None,
    *,
    selection: str,
) -> dict[str, Any]:
    return {
        "selection": selection,
        "classification": classify_probe_operation(operation),
        "operation": asdict(operation) if operation is not None else None,
    }


def _bounded_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProbeOperatorEvidenceError(f"{name} must be a non-empty string")
    if len(value) > MAX_IDENTITY_TEXT:
        raise ProbeOperatorEvidenceError(f"{name} exceeds the bounded identity length")
    return value


def _positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbeOperatorEvidenceError(f"{name} must be a positive integer")
    return value


def _expected_old_url(operation: ProbeMutationOperation) -> str | None:
    prior = operation.prior_slot
    if not isinstance(prior, dict):
        return None
    value = prior.get("actual_url")
    return str(value) if isinstance(value, str) and value else None


def _parse_match(
    raw: Any,
    *,
    name: str,
    expected_url: str | None,
) -> ProbeWindowMatch:
    if not isinstance(raw, dict):
        raise ProbeOperatorEvidenceError(f"{name} entries must be JSON objects")
    keys = set(raw)
    if keys != _ALLOWED_MATCH_KEYS:
        missing = sorted(_ALLOWED_MATCH_KEYS - keys)
        extra = sorted(keys - _ALLOWED_MATCH_KEYS)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise ProbeOperatorEvidenceError(
            f"{name} match fields are invalid" + (" (" + "; ".join(detail) + ")" if detail else "")
        )
    actual_url = _bounded_text(raw["actual_url"], name=f"{name}.actual_url")
    if expected_url is None:
        raise ProbeOperatorEvidenceError(f"{name} evidence is not valid for this operation")
    if actual_url.rstrip("/") != expected_url.rstrip("/"):
        raise ProbeOperatorEvidenceError(f"{name}.actual_url does not match this operation")
    return ProbeWindowMatch(
        window_handle=_positive_int(raw["window_handle"], name=f"{name}.window_handle"),
        browser_pid=_positive_int(raw["browser_pid"], name=f"{name}.browser_pid"),
        chrome_executable=_bounded_text(
            raw["chrome_executable"], name=f"{name}.chrome_executable"
        ),
        actual_url=actual_url,
    )


def _parse_match_list(
    raw: Any,
    *,
    name: str,
    expected_url: str | None,
) -> list[ProbeWindowMatch]:
    if not isinstance(raw, list):
        raise ProbeOperatorEvidenceError(f"{name} must be a JSON array")
    if len(raw) > MAX_MATCHES_PER_TARGET:
        raise ProbeOperatorEvidenceError(
            f"{name} exceeds the maximum of {MAX_MATCHES_PER_TARGET} matches"
        )
    return [
        _parse_match(item, name=f"{name}[{index}]", expected_url=expected_url)
        for index, item in enumerate(raw)
    ]


def parse_probe_reconciliation_evidence(
    payload: Any,
    operation: ProbeMutationOperation,
) -> ProbeMutationObservation:
    if not isinstance(payload, dict):
        raise ProbeOperatorEvidenceError("probe reconciliation evidence must be a JSON object")
    keys = set(payload)
    if keys != _REQUIRED_TOP_LEVEL:
        missing = sorted(_REQUIRED_TOP_LEVEL - keys)
        extra = sorted(keys - _ALLOWED_TOP_LEVEL)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise ProbeOperatorEvidenceError(
            "probe reconciliation evidence fields are invalid"
            + (" (" + "; ".join(detail) + ")" if detail else "")
        )

    operation_id = _bounded_text(payload["operation_id"], name="operation_id")
    owner_token = _bounded_text(payload["owner_token"], name="owner_token")
    if operation_id != operation.operation_id:
        raise ProbeOperatorEvidenceError("evidence operation_id does not match the requested operation")
    if owner_token != operation.owner_token:
        raise ProbeOperatorEvidenceError("evidence owner_token does not match the durable operation")

    observed_at = payload["observed_at"]
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
        raise ProbeOperatorEvidenceError("observed_at must be a finite non-negative number")
    observed_at = float(observed_at)
    if not math.isfinite(observed_at) or observed_at < 0:
        raise ProbeOperatorEvidenceError("observed_at must be a finite non-negative number")
    if observed_at < float(operation.updated_at):
        raise ProbeOperatorEvidenceError(
            "observed_at predates the operation's current durable state"
        )
    if observed_at > time.time() + 300.0:
        raise ProbeOperatorEvidenceError("observed_at is implausibly far in the future")
    if not isinstance(payload["complete"], bool):
        raise ProbeOperatorEvidenceError("complete must be a boolean")

    old_matches = _parse_match_list(
        payload["old_matches"],
        name="old_matches",
        expected_url=_expected_old_url(operation),
    )
    new_matches = _parse_match_list(
        payload["new_matches"],
        name="new_matches",
        expected_url=operation.expected_actual_url or None,
    )
    return ProbeMutationObservation(
        observed_at=observed_at,
        old_matches=old_matches,
        new_matches=new_matches,
        complete=payload["complete"],
    )


def load_probe_reconciliation_evidence(
    path: str | Path,
    operation: ProbeMutationOperation,
) -> ProbeMutationObservation:
    evidence_path = Path(path)
    try:
        size = evidence_path.stat().st_size
    except OSError as exc:
        raise ProbeOperatorEvidenceError(f"cannot read probe reconciliation evidence: {exc}") from exc
    if size > MAX_EVIDENCE_BYTES:
        raise ProbeOperatorEvidenceError(
            f"probe reconciliation evidence exceeds {MAX_EVIDENCE_BYTES} bytes"
        )
    try:
        text = evidence_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ProbeOperatorEvidenceError(f"cannot read probe reconciliation evidence: {exc}") from exc
    payload = json.loads(text)
    return parse_probe_reconciliation_evidence(payload, operation)
