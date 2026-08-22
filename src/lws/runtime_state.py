from __future__ import annotations

import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Mapping

WINDOWS_STATE_DIR_NAME = "LocalShellWebSupervisor"
POSIX_STATE_DIR_NAME = "localshell-web-supervisor"
REGISTRY_FILENAME = "registry.sqlite3"


def durable_state_dir(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | Path | None = None,
) -> Path:
    """Return the per-user durable LWS state directory outside a source checkout."""

    env = os.environ if environ is None else environ
    override = str(env.get("LWS_STATE_HOME") or "").strip()
    if override:
        return Path(override).expanduser()

    platform = os.name if platform is None else platform
    home_path = Path.home() if home is None else Path(home)
    if platform == "nt":
        local_app_data = str(env.get("LOCALAPPDATA") or "").strip()
        base = Path(local_app_data) if local_app_data else home_path / "AppData" / "Local"
        return base / WINDOWS_STATE_DIR_NAME

    xdg_state_home = str(env.get("XDG_STATE_HOME") or "").strip()
    base = Path(xdg_state_home) if xdg_state_home else home_path / ".local" / "state"
    return base / POSIX_STATE_DIR_NAME


def durable_registry_path(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | Path | None = None,
) -> Path:
    return durable_state_dir(environ=environ, platform=platform, home=home) / REGISTRY_FILENAME


def legacy_registry_path(*, cwd: str | Path | None = None) -> Path:
    return Path.cwd() / ".lws" / REGISTRY_FILENAME if cwd is None else Path(cwd) / ".lws" / REGISTRY_FILENAME


def resolve_default_registry_path(
    *,
    cwd: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | Path | None = None,
    now: float | None = None,
) -> Path:
    """Resolve the implicit registry path with conservative legacy migration.

    Explicit ``LWS_DB`` remains authoritative. Otherwise LWS prefers a per-user state
    directory that survives deleting/recloning the source tree. If the durable registry
    does not exist but the old repo-local registry does, a consistent SQLite backup is
    migrated automatically only when no fresh watchdog lease fences the legacy database.

    If legacy lease state cannot be inspected or migration fails, the legacy path remains
    authoritative for that invocation rather than silently splitting one control plane
    across two registries.
    """

    env = os.environ if environ is None else environ
    explicit = str(env.get("LWS_DB") or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    durable = durable_registry_path(environ=env, platform=platform, home=home)
    if durable.exists():
        return durable

    legacy = legacy_registry_path(cwd=cwd)
    if not legacy.exists():
        return durable

    lease_state = _legacy_fresh_watchdog_lease(legacy, now=now)
    if lease_state is not False:
        # True means an active/fenced watchdog. None means inspection was ambiguous.
        # Both cases retain the old registry until a later clean invocation can migrate.
        return legacy

    if _migrate_sqlite_registry(legacy, durable):
        return durable
    return legacy


def _legacy_fresh_watchdog_lease(path: Path, *, now: float | None = None) -> bool | None:
    """Return True for a fresh lease, False for none/stale, None when uncertain."""

    timestamp = time.time() if now is None else float(now)
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=0.25)
        try:
            conn.execute("PRAGMA query_only = ON")
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchdog_leases'"
            ).fetchone()
            if table is None:
                return False
            row = conn.execute(
                "SELECT MAX(expires_at) FROM watchdog_leases"
            ).fetchone()
            return bool(row and row[0] is not None and float(row[0]) > timestamp)
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError):
        return None


def _migrate_sqlite_registry(source: Path, target: Path, *, timeout_s: float = 3.0) -> bool:
    """Copy a legacy SQLite registry into durable state without modifying the source."""

    if target.exists():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.migrating-{uuid.uuid4().hex}")
    started = time.monotonic()
    source_conn: sqlite3.Connection | None = None
    target_conn: sqlite3.Connection | None = None
    try:
        source_conn = sqlite3.connect(_readonly_uri(source), uri=True, timeout=0.5)
        source_conn.execute("PRAGMA query_only = ON")
        target_conn = sqlite3.connect(temporary)

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() - started > max(0.1, float(timeout_s)):
                raise TimeoutError("legacy registry migration timed out")

        source_conn.backup(target_conn, pages=128, progress=progress, sleep=0.05)
        target_conn.commit()
        integrity = target_conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise sqlite3.DatabaseError("migrated registry failed integrity_check")
        target_conn.close()
        target_conn = None
        source_conn.close()
        source_conn = None

        # The durable path may have appeared while the backup was running. Never overwrite
        # an already-established durable registry; the temporary copy is disposable.
        if target.exists():
            temporary.unlink(missing_ok=True)
            return True
        temporary.replace(target)
        return True
    except (OSError, sqlite3.Error, TimeoutError):
        return False
    finally:
        if target_conn is not None:
            target_conn.close()
        if source_conn is not None:
            source_conn.close()
        temporary.unlink(missing_ok=True)


def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"
