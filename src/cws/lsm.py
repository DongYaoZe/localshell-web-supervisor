from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .models import LsmObservation

IN_FLIGHT_LEASE_S = 2 * 60 * 60
ACTIVE_JOB_STATES = {"starting", "running", "stopping", "retrying"}
FAILED_JOB_STATES = {"failed", "lost"}
SUPPORTED_SESSION_STATE_VERSION = 1
SUPPORTED_JOB_STORE_VERSION = 2


class UnsupportedLsmState(RuntimeError):
    """Raised when a Local Shell MCP durable state schema is not understood."""


def detect_lsm_state_dir(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for name in ("CWS_LSM_STATE_DIR", "LOCAL_SHELL_MCP_STATE_DIR"):
        value = os.getenv(name)
        if value:
            candidates.append(Path(value))
    if os.name == "nt":
        candidates.append(Path(r"C:\ProgramData\LocalShellMCP-Hardened\control\state"))
    for candidate in candidates:
        if (candidate / "sessions").is_dir():
            return candidate
    return None


def _read_json(path: Path, retries: int = 3) -> Any:
    last: Exception | None = None
    for _ in range(retries):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(0.02)
    if last:
        raise last
    raise RuntimeError(f"unable to read {path}")


class FileLsmTelemetry:
    """Read-only adapter over Local Shell MCP's durable file backend.

    It deliberately does not mutate LSM state and does not pretend to be an MCP client.
    The adapter consumes durable session/plan/job evidence that remains useful when a
    ChatGPT Web message lifecycle is broken.
    """

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self._jobs_cache_key: tuple[int, int] | None = None
        self._jobs_cache: dict[str, Any] | None = None

    def session_payload(self, session_id: str) -> dict[str, Any]:
        payload = _read_json(self.state_dir / "sessions" / f"{session_id}.json")
        version = payload.get("version") if isinstance(payload, dict) else None
        if version != SUPPORTED_SESSION_STATE_VERSION:
            raise UnsupportedLsmState(
                f"unsupported LSM session-state version {version!r}; "
                f"expected {SUPPORTED_SESSION_STATE_VERSION}"
            )
        return payload

    def jobs_payload(self) -> dict[str, Any]:
        path = self.state_dir / "jobs.json"
        if not path.exists():
            return {"version": SUPPORTED_JOB_STORE_VERSION, "jobs": []}
        try:
            stat = path.stat()
            cache_key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            cache_key = None
        if cache_key is not None and cache_key == self._jobs_cache_key and self._jobs_cache is not None:
            return self._jobs_cache
        try:
            data = _read_json(path)
        except (PermissionError, json.JSONDecodeError):
            backup = self.state_dir / "jobs.json.bak"
            if not backup.exists():
                raise
            data = _read_json(backup)
        if not isinstance(data, dict):
            raise UnsupportedLsmState("LSM jobs store is not an object")
        version = data.get("version")
        if version != SUPPORTED_JOB_STORE_VERSION:
            raise UnsupportedLsmState(
                f"unsupported LSM job-store version {version!r}; "
                f"expected {SUPPORTED_JOB_STORE_VERSION}"
            )
        if cache_key is not None:
            self._jobs_cache_key = cache_key
            self._jobs_cache = data
        return data

    @staticmethod
    def _effective_job(row: dict[str, Any]) -> dict[str, Any]:
        """Overlay a terminal runner status file without mutating LSM state.

        LSM itself performs richer reconciliation (including live persistent-shell
        membership). CWS only adopts a terminal attempt payload because that is a
        monotonic durable fact; absence of such a payload is never treated as proof
        that a running job died.
        """
        job = deepcopy(row)
        status = str(job.get("status") or "unknown")
        if status not in ACTIVE_JOB_STATES:
            return job
        raw_path = job.get("pending_status_path") if status == "retrying" else job.get("status_path")
        if not raw_path or str(raw_path).startswith("state://"):
            return job
        try:
            payload = _read_json(Path(str(raw_path)), retries=1)
        except (OSError, json.JSONDecodeError):
            return job
        if not isinstance(payload, dict) or "exit_code" not in payload:
            return job
        exit_code = payload.get("exit_code")
        completed_at = payload.get("completed_at") or job.get("updated_at")
        job.update(
            {
                "status": "succeeded" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "completed_at": completed_at,
                "updated_at": completed_at,
                "error": payload.get("error"),
                "status_source": "attempt_status",
            }
        )
        return job

    def observe(self, *, task_id: str, session_id: str, tracked_job_ids: list[str]) -> LsmObservation:
        now = time.time()
        session = self.session_payload(session_id)
        plan = session.get("plan") or {}
        activity = session.get("activity") or []
        recent = activity[-1] if activity else {}

        live_heartbeats: list[float] = []
        for lease in (session.get("in_flight_calls") or {}).values():
            heartbeat = float(lease.get("heartbeat_at") or lease.get("started_at") or 0)
            if heartbeat and now - heartbeat < IN_FLIGHT_LEASE_S:
                live_heartbeats.append(heartbeat)

        jobs_by_id = {
            str(row.get("job_id")): self._effective_job(row)
            for row in self.jobs_payload().get("jobs", [])
            if isinstance(row, dict) and row.get("job_id")
        }
        tracked = [jobs_by_id[job_id] for job_id in tracked_job_ids if job_id in jobs_by_id]
        active_jobs = sum(str(row.get("status")) in ACTIVE_JOB_STATES for row in tracked)
        failed_jobs = sum(str(row.get("status")) in FAILED_JOB_STATES for row in tracked)
        succeeded_jobs = sum(str(row.get("status")) == "succeeded" for row in tracked)
        steps = plan.get("steps") or []

        last_activity = plan.get("last_agent_activity")
        continuation_due = None
        if plan and plan.get("status") == "active" and last_activity is not None:
            lease_s = float(plan.get("execution_lease_s") or 900)
            continuation_due = now >= float(last_activity) + lease_s

        return LsmObservation(
            task_id=task_id,
            observed_at=now,
            session_id=session.get("session_id"),
            session_status=session.get("status"),
            active_run_id=session.get("active_run_id"),
            plan_status=plan.get("status"),
            plan_last_agent_activity=float(last_activity) if last_activity is not None else None,
            continuation_due=continuation_due,
            continuation_pending=bool(plan.get("continuation_pending")) if plan else None,
            in_flight_calls=len(live_heartbeats),
            freshest_in_flight_heartbeat=max(live_heartbeats) if live_heartbeats else None,
            active_jobs=active_jobs,
            failed_jobs=failed_jobs,
            succeeded_jobs=succeeded_jobs,
            recent_event_type=recent.get("type"),
            recent_event_at=float(recent["ts"]) if recent.get("ts") is not None else None,
            completed_steps=sum(str(step.get("status")) in {"completed", "skipped"} for step in steps),
            total_steps=len(steps),
            raw={
                "plan_id": plan.get("plan_id"),
                "continuation_count": plan.get("continuation_count"),
                "active_run_count": int(bool(session.get("active_run_id"))),
                "tracked_jobs": [
                    {
                        "job_id": row.get("job_id"),
                        "status": row.get("status"),
                        "updated_at": row.get("updated_at"),
                        "status_source": row.get("status_source", "jobs_store"),
                    }
                    for row in tracked
                ],
            },
        )
