from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .lifecycle import PageCloseEvidence, evaluate_page_close_evidence
from .models import PageCapabilityKind, PageCapabilityRecord


PAGE_CLOSE_EVALUATOR_VERSION = "page-close-v2"
PAGE_CAPABILITY_SURFACE = "normal_chrome_uia"
PAGE_CAPABILITY_PLATFORM = "windows"
PAGE_CAPABILITY_BROWSER = "chrome"
DEFAULT_CAPABILITY_TTL_S = 24 * 60 * 60
MAX_CAPABILITY_TTL_S = 7 * 24 * 60 * 60


class CapabilityProvenanceError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class CapabilityContext:
    scope_host: str
    browser_family: str
    browser_major: int
    platform: str
    surface: str


def _normalized_context(
    *,
    scope_host: str,
    browser_family: str,
    browser_major: int,
    platform: str,
    surface: str,
) -> CapabilityContext:
    host = scope_host.strip().lower()
    family = browser_family.strip().lower()
    platform_name = platform.strip().lower()
    if platform_name in {"win32", "win64", "nt"}:
        platform_name = "windows"
    surface_name = surface.strip().lower()
    major = int(browser_major)
    if not host or "." not in host:
        raise CapabilityProvenanceError("capability scope host is missing or invalid")
    if family != PAGE_CAPABILITY_BROWSER:
        raise CapabilityProvenanceError("page-close capability currently requires Chrome provenance")
    if major <= 0:
        raise CapabilityProvenanceError("page-close capability requires a positive Chrome major version")
    if platform_name != PAGE_CAPABILITY_PLATFORM:
        raise CapabilityProvenanceError("page-close capability currently requires Windows provenance")
    if surface_name != PAGE_CAPABILITY_SURFACE:
        raise CapabilityProvenanceError(
            "page-close capability requires the normal-Chrome UIA observation surface"
        )
    return CapabilityContext(host, family, major, platform_name, surface_name)


def context_from_evidence(evidence: PageCloseEvidence) -> CapabilityContext:
    pre_host = urlsplit(evidence.pre_close_url).netloc.lower()
    reopened_host = urlsplit(evidence.reopened_url).netloc.lower()
    if not pre_host or pre_host != reopened_host:
        raise CapabilityProvenanceError("page-close evidence hosts are missing or inconsistent")
    scope_host = (evidence.scope_host or pre_host).strip().lower()
    if scope_host != pre_host:
        raise CapabilityProvenanceError("declared capability scope does not match evidence host")
    if evidence.browser_family is None:
        raise CapabilityProvenanceError("evidence is missing browser_family provenance")
    if evidence.browser_major is None:
        raise CapabilityProvenanceError("evidence is missing browser_major provenance")
    if evidence.platform is None:
        raise CapabilityProvenanceError("evidence is missing platform provenance")
    if evidence.surface is None:
        raise CapabilityProvenanceError("evidence is missing observation surface provenance")
    return _normalized_context(
        scope_host=scope_host,
        browser_family=evidence.browser_family,
        browser_major=evidence.browser_major,
        platform=evidence.platform,
        surface=evidence.surface,
    )


def canonical_evidence_digest(evidence: PageCloseEvidence) -> str:
    payload = json.dumps(
        asdict(evidence),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _capability_id(
    kind: PageCapabilityKind,
    *,
    evidence_digest: str,
    context: CapabilityContext,
    isolation_mode: str,
) -> str:
    material = "|".join(
        [
            kind.value,
            evidence_digest,
            context.scope_host,
            context.browser_family,
            str(context.browser_major),
            context.platform,
            context.surface,
            isolation_mode,
            PAGE_CLOSE_EVALUATOR_VERSION,
        ]
    )
    return "cap_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def build_page_close_capabilities(
    evidence: PageCloseEvidence,
    *,
    ttl_s: float = DEFAULT_CAPABILITY_TTL_S,
    recorded_at: float | None = None,
) -> list[PageCapabilityRecord]:
    evaluation = evaluate_page_close_evidence(evidence)
    if not evaluation.generation_parking_safe:
        raise CapabilityProvenanceError(
            "generation page-close gate failed: " + ", ".join(evaluation.blockers)
        )
    if evidence.observed_at is None or float(evidence.observed_at) <= 0:
        raise CapabilityProvenanceError(
            "durable capability import requires the experiment's actual observed_at timestamp"
        )
    context = context_from_evidence(evidence)
    recorded_at = time.time() if recorded_at is None else float(recorded_at)
    observed_at = float(evidence.observed_at)
    if observed_at > recorded_at + 30.0:
        raise CapabilityProvenanceError("capability evidence timestamp is implausibly in the future")
    ttl_s = min(MAX_CAPABILITY_TTL_S, max(60.0, float(ttl_s)))
    expires_at = observed_at + ttl_s
    digest = canonical_evidence_digest(evidence)

    kinds = [PageCapabilityKind.GENERATION]
    if evaluation.tool_execution_parking_safe:
        kinds.append(PageCapabilityKind.TOOL_EXECUTION)

    records: list[PageCapabilityRecord] = []
    for kind in kinds:
        records.append(
            PageCapabilityRecord(
                capability_id=_capability_id(
                    kind,
                    evidence_digest=digest,
                    context=context,
                    isolation_mode=evidence.isolation_mode,
                ),
                kind=kind,
                scope_host=context.scope_host,
                browser_family=context.browser_family,
                browser_major=context.browser_major,
                platform=context.platform,
                surface=context.surface,
                isolation_mode=evidence.isolation_mode,
                evaluator_version=PAGE_CLOSE_EVALUATOR_VERSION,
                evidence_digest=digest,
                source_experiment_id=evidence.experiment_id,
                observed_at=observed_at,
                recorded_at=recorded_at,
                expires_at=expires_at,
                metadata={
                    "generation_checks": dict(evaluation.checks),
                    "tool_checks": dict(evaluation.tool_checks),
                    "generation_safe": evaluation.generation_parking_safe,
                    "tool_execution_safe": evaluation.tool_execution_parking_safe,
                },
            )
        )
    return records


def capability_matches_context(
    capability: PageCapabilityRecord,
    context: CapabilityContext,
    *,
    expected_kind: PageCapabilityKind,
    now: float | None = None,
) -> tuple[bool, list[str]]:
    now = time.time() if now is None else float(now)
    blockers: list[str] = []
    checks = {
        "kind": capability.kind == expected_kind,
        "evaluator_version": capability.evaluator_version == PAGE_CLOSE_EVALUATOR_VERSION,
        "scope_host": capability.scope_host.lower() == context.scope_host.lower(),
        "browser_family": capability.browser_family.lower() == context.browser_family.lower(),
        "browser_major": int(capability.browser_major) == int(context.browser_major),
        "platform": capability.platform.lower() == context.platform.lower(),
        "surface": capability.surface.lower() == context.surface.lower(),
        "fresh": capability.is_fresh(now=now),
    }
    for name, passed in checks.items():
        if not passed:
            blockers.append(name)
    return not blockers, blockers


def runtime_context(*, browser_major: int, scope_host: str = "chatgpt.com") -> CapabilityContext:
    return _normalized_context(
        scope_host=scope_host,
        browser_family=PAGE_CAPABILITY_BROWSER,
        browser_major=browser_major,
        platform=PAGE_CAPABILITY_PLATFORM,
        surface=PAGE_CAPABILITY_SURFACE,
    )


def detect_chrome_major(
    chrome_executable: str,
    *,
    powershell: str = "powershell.exe",
    timeout_s: float = 5.0,
) -> int:
    """Read the local chrome.exe file version; this does not start or attach to Chrome."""
    if not chrome_executable or not os.path.isfile(chrome_executable):
        raise CapabilityProvenanceError("Chrome executable is unavailable for version provenance")
    env = os.environ.copy()
    env["CWS_CHROME_EXE"] = chrome_executable
    script = (
        "$ErrorActionPreference='Stop'; "
        "$v=(Get-Item -LiteralPath $env:CWS_CHROME_EXE).VersionInfo.ProductVersion; "
        "[Console]::Out.Write([string]$v)"
    )
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=max(1.0, float(timeout_s)),
        env=env,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise CapabilityProvenanceError("could not read local Chrome file version")
    match = re.match(r"\s*(\d+)", completed.stdout or "")
    if not match:
        raise CapabilityProvenanceError("local Chrome file version is not parseable")
    return int(match.group(1))
