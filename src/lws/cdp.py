from __future__ import annotations

import time
import ipaddress
from collections import Counter
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .models import NetworkObservation, WorkerRecord
from .uia import normalize_url


class CdpProbeUnavailable(RuntimeError):
    """Raised when a configured CDP target cannot be observed safely."""


def _validate_endpoint(endpoint: str, *, allow_remote: bool) -> str:
    """Validate a CDP endpoint without persisting credentials/query strings.

    Loopback endpoints are safe by default. Remote endpoints require an explicit opt-in
    because CDP grants powerful browser access and URLs may contain bearer credentials.
    """
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise CdpProbeUnavailable("CDP endpoint must be an absolute http(s)/ws(s) URL")
    host = parsed.hostname
    is_loopback = host.lower() == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback and not allow_remote:
        raise CdpProbeUnavailable(
            "remote CDP endpoints require explicit allow_remote=True; loopback is the safe default"
        )
    return host


class _NetworkAccumulator:
    def __init__(self, *, last_activity_at: float | None = None) -> None:
        self.event_count = 0
        self.request_count = 0
        self.response_count = 0
        self.data_event_count = 0
        self.encoded_data_bytes = 0
        self.loading_finished = 0
        self.loading_failed = 0
        self.websocket_frames = 0
        self.last_activity_at: float | None = last_activity_at
        self.inflight: set[str] = set()
        self.origins: Counter[str] = Counter()
        self.resource_types: Counter[str] = Counter()
        self.statuses: Counter[str] = Counter()
        self.failures: list[str] = []

    def activity(self) -> None:
        self.event_count += 1
        self.last_activity_at = time.time()

    def request_will_be_sent(self, params: dict[str, Any]) -> None:
        self.activity()
        self.request_count += 1
        request_id = str(params.get("requestId") or "")
        if request_id:
            self.inflight.add(request_id)
        request = params.get("request") or {}
        host = urlsplit(str(request.get("url") or "")).netloc
        if host:
            self.origins[host] += 1
        self.resource_types[str(params.get("type") or "unknown")] += 1

    def response_received(self, params: dict[str, Any]) -> None:
        self.activity()
        self.response_count += 1
        response = params.get("response") or {}
        status = response.get("status")
        if status is not None:
            rendered = str(int(status) if isinstance(status, (int, float)) else status)
            self.statuses[rendered] += 1
        host = urlsplit(str(response.get("url") or "")).netloc
        if host:
            self.origins[host] += 1

    def data_received(self, params: dict[str, Any]) -> None:
        self.activity()
        self.data_event_count += 1
        encoded = params.get("encodedDataLength")
        if isinstance(encoded, (int, float)) and encoded > 0:
            self.encoded_data_bytes += int(encoded)

    def loading_finished_event(self, params: dict[str, Any]) -> None:
        self.activity()
        self.loading_finished += 1
        request_id = str(params.get("requestId") or "")
        if request_id:
            self.inflight.discard(request_id)

    def loading_failed_event(self, params: dict[str, Any]) -> None:
        self.activity()
        self.loading_failed += 1
        request_id = str(params.get("requestId") or "")
        if request_id:
            self.inflight.discard(request_id)
        error = str(params.get("errorText") or "").strip()
        if error and error not in self.failures and len(self.failures) < 20:
            self.failures.append(error[:300])

    def websocket_frame(self, _params: dict[str, Any]) -> None:
        self.activity()
        self.websocket_frames += 1


def sample_cdp_session(
    session,
    *,
    worker_id: str,
    page_url: str,
    sample_s: float,
    source: str,
    wait: Callable[[int], None],
    raw_context: dict[str, Any] | None = None,
    previous_last_activity_at: float | None = None,
    previous_quiet_since_at: float | None = None,
) -> NetworkObservation:
    """Collect a bounded Network-domain sample from an already-created CDP session.

    `wait(milliseconds)` must keep the owning Playwright event loop pumping. The function
    records lifecycle counters/timing and deliberately ignores request headers, cookies,
    POST data, and response bodies.
    """
    started = time.time()
    acc = _NetworkAccumulator(last_activity_at=previous_last_activity_at)
    session.on("Network.requestWillBeSent", acc.request_will_be_sent)
    session.on("Network.responseReceived", acc.response_received)
    session.on("Network.dataReceived", acc.data_received)
    session.on("Network.loadingFinished", acc.loading_finished_event)
    session.on("Network.loadingFailed", acc.loading_failed_event)
    session.on("Network.webSocketFrameReceived", acc.websocket_frame)
    session.on("Network.webSocketFrameSent", acc.websocket_frame)
    session.send("Network.enable")
    wait(max(1, int(max(0.01, float(sample_s)) * 1000)))
    ended = time.time()
    quiet_since_at = (
        acc.last_activity_at
        if acc.event_count > 0 and acc.last_activity_at is not None
        else (previous_quiet_since_at or started)
    )
    try:
        session.send("Network.disable")
    except Exception:
        pass

    context = dict(raw_context or {})
    context.update(
        {
            "sample_s": max(0.01, float(sample_s)),
            "origins": dict(acc.origins.most_common(20)),
            "resource_types": dict(acc.resource_types.most_common(20)),
            "statuses": dict(acc.statuses.most_common(20)),
            "failures": acc.failures,
            "scope": "sample_only",
            "inflight_scope": "requests_observed_during_sample_only",
        }
    )
    return NetworkObservation(
        worker_id=worker_id,
        observed_at=ended,
        source=source,
        sample_started_at=started,
        sample_ended_at=ended,
        page_url=page_url,
        event_count=acc.event_count,
        request_count=acc.request_count,
        response_count=acc.response_count,
        data_event_count=acc.data_event_count,
        encoded_data_bytes=acc.encoded_data_bytes,
        loading_finished=acc.loading_finished,
        loading_failed=acc.loading_failed,
        websocket_frames=acc.websocket_frames,
        last_activity_at=acc.last_activity_at,
        quiet_since_at=quiet_since_at,
        inflight_requests=len(acc.inflight),
        raw=context,
    )


class CdpNetworkProbe:
    """Sample an already-open page through an explicitly exposed CDP endpoint.

    The probe never navigates, clicks, evaluates page JavaScript, reads headers/cookies,
    reads response bodies, or closes the remote browser. TCP/WebSocket CDP exposure is an
    optional integration path; LWS-owned Playwright browsers can use `sample_cdp_session`
    directly and do not require a remote-debugging TCP port.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        sample_s: float = 2.0,
        connect_timeout_ms: int = 5000,
        allow_remote: bool = False,
    ) -> None:
        self.endpoint = endpoint
        self._endpoint_host = _validate_endpoint(endpoint, allow_remote=allow_remote)
        self.allow_remote = bool(allow_remote)
        self.sample_s = max(0.1, float(sample_s))
        self.connect_timeout_ms = max(500, int(connect_timeout_ms))

    def sample(
        self,
        worker: WorkerRecord,
        *,
        previous: NetworkObservation | None = None,
    ) -> NetworkObservation:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional environment
            raise CdpProbeUnavailable(
                "Playwright is not installed; install the optional 'cdp' dependencies"
            ) from exc

        playwright = sync_playwright().start()
        session = None
        try:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    self.endpoint,
                    timeout=self.connect_timeout_ms,
                )
            except PlaywrightError as exc:
                raise CdpProbeUnavailable(f"cannot connect to CDP endpoint: {exc}") from exc

            expected = normalize_url(worker.conversation_url)
            pages = [page for context in browser.contexts for page in context.pages]
            page = next((page for page in pages if normalize_url(page.url) == expected), None)
            if page is None:
                raise CdpProbeUnavailable(
                    "no CDP page matched the exact registered conversation URL; "
                    f"attached browser exposes {len(pages)} page(s)"
                )

            session = page.context.new_cdp_session(page)
            observation = sample_cdp_session(
                session,
                worker_id=worker.worker_id,
                page_url=page.url,
                sample_s=self.sample_s,
                source="cdp",
                wait=page.wait_for_timeout,
                raw_context={
                    "endpoint_host": self._endpoint_host,
                    "endpoint_scope": (
                        "loopback_only"
                        if self._endpoint_host.lower() == "localhost"
                        or _host_is_loopback(self._endpoint_host)
                        else "remote_opt_in"
                    ),
                    "ownership": "external",
                },
                previous_last_activity_at=(
                    previous.last_activity_at if previous is not None else None
                ),
                previous_quiet_since_at=(
                    previous.quiet_since_at if previous is not None else None
                ),
            )
            session.detach()
            session = None
            return observation
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
            # Do not call browser.close(): for connect_over_cdp that would terminate the
            # observed remote browser. Stopping Playwright only tears down this client.
            playwright.stop()


def _host_is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
