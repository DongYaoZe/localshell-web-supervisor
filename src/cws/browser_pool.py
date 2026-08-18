from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class PageRole(StrEnum):
    ACTIVE = "active"
    PROBE = "probe"


@dataclass(slots=True)
class PageLease:
    page_id: str
    role: PageRole
    worker_id: str | None
    opened_at: float
    last_used_at: float


@dataclass(slots=True)
class ProbeTarget:
    worker_id: str
    conversation_url: str
    last_probed_at: float | None = None
    priority: int = 0


@dataclass(slots=True)
class ProbeResourcePolicy:
    """Conservative resource blocking for a dedicated read-only probe page.

    Scripts, documents, XHR/fetch, WebSockets, and stylesheets are deliberately allowed
    because ChatGPT state detection may depend on them. Large visual-only resources can be
    blocked to reduce RAM/network pressure on a dedicated CWS-owned probe context.
    """

    blocked_resource_types: frozenset[str] = frozenset({"image", "media", "font"})

    def should_block(self, resource_type: str) -> bool:
        return resource_type.lower() in self.blocked_resource_types


class PagePoolError(RuntimeError):
    pass


class PagePool:
    """Ephemeral page-lease registry independent of a browser implementation.

    This class does not launch, close, navigate, or authenticate a browser. A future
    Playwright/LSM adapter owns those actions and reports page IDs into this pool. Keeping
    the control state transport-neutral prevents a browser restart from becoming durable
    task state.
    """

    def __init__(self, *, max_active_pages: int = 4, max_probe_pages: int = 1) -> None:
        self.max_active_pages = max(1, int(max_active_pages))
        self.max_probe_pages = max(1, int(max_probe_pages))
        self._leases: dict[str, PageLease] = {}
        self._probe_targets: dict[str, ProbeTarget] = {}

    def leases(self) -> list[PageLease]:
        return sorted(self._leases.values(), key=lambda lease: (lease.role.value, lease.page_id))

    def register_page(
        self,
        page_id: str,
        *,
        role: PageRole,
        worker_id: str | None,
        now: float | None = None,
    ) -> PageLease:
        now = time.time() if now is None else float(now)
        existing = self._leases.get(page_id)
        if existing:
            existing.role = role
            existing.worker_id = worker_id
            existing.last_used_at = now
            self._enforce_capacity(exclude_page_id=page_id)
            return existing
        lease = PageLease(
            page_id=page_id,
            role=role,
            worker_id=worker_id,
            opened_at=now,
            last_used_at=now,
        )
        self._leases[page_id] = lease
        try:
            self._enforce_capacity(exclude_page_id=page_id)
        except Exception:
            self._leases.pop(page_id, None)
            raise
        return lease

    def touch(self, page_id: str, *, now: float | None = None) -> None:
        lease = self._leases.get(page_id)
        if not lease:
            raise PagePoolError(f"unknown page lease: {page_id}")
        lease.last_used_at = time.time() if now is None else float(now)

    def release_page(self, page_id: str) -> PageLease | None:
        """Forget an already-closed/detached page; does not close the browser page itself."""
        return self._leases.pop(page_id, None)

    def register_probe_target(
        self,
        worker_id: str,
        conversation_url: str,
        *,
        priority: int = 0,
        last_probed_at: float | None = None,
    ) -> ProbeTarget:
        target = ProbeTarget(
            worker_id=worker_id,
            conversation_url=conversation_url,
            last_probed_at=last_probed_at,
            priority=int(priority),
        )
        self._probe_targets[worker_id] = target
        return target

    def remove_probe_target(self, worker_id: str) -> None:
        self._probe_targets.pop(worker_id, None)

    def next_probe_target(self, *, now: float | None = None) -> ProbeTarget | None:
        if not self._probe_targets:
            return None
        _ = time.time() if now is None else float(now)
        # Higher priority first; within a priority, never-probed then oldest-probed.
        return min(
            self._probe_targets.values(),
            key=lambda target: (
                -target.priority,
                target.last_probed_at is not None,
                target.last_probed_at if target.last_probed_at is not None else float("-inf"),
                target.worker_id,
            ),
        )

    def mark_probed(self, worker_id: str, *, now: float | None = None) -> None:
        target = self._probe_targets.get(worker_id)
        if not target:
            raise PagePoolError(f"unknown probe target: {worker_id}")
        target.last_probed_at = time.time() if now is None else float(now)

    def _enforce_capacity(self, *, exclude_page_id: str | None = None) -> None:
        active = [lease for lease in self._leases.values() if lease.role == PageRole.ACTIVE]
        probes = [lease for lease in self._leases.values() if lease.role == PageRole.PROBE]
        if len(active) > self.max_active_pages:
            raise PagePoolError(
                f"active page capacity exceeded: {len(active)} > {self.max_active_pages}; "
                "pool does not evict pages implicitly"
            )
        if len(probes) > self.max_probe_pages:
            raise PagePoolError(
                f"probe page capacity exceeded: {len(probes)} > {self.max_probe_pages}; "
                "pool does not evict pages implicitly"
            )
