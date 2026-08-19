from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    objective TEXT NOT NULL,
    cwd TEXT NOT NULL,
    state TEXT NOT NULL,
    lsm_session_id TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    current_worker_id TEXT,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    max_recovery_attempts INTEGER NOT NULL DEFAULT 3,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    conversation_url TEXT NOT NULL,
    conversation_id TEXT,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    last_seen_at REAL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS idx_workers_task ON workers(task_id);
CREATE TABLE IF NOT EXISTS task_jobs (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    PRIMARY KEY (task_id, job_id)
);
CREATE TABLE IF NOT EXISTS browser_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
    observed_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_browser_obs_worker_time
    ON browser_observations(worker_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS network_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
    observed_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_network_obs_worker_time
    ON network_observations(worker_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS lsm_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    observed_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lsm_obs_task_time
    ON lsm_observations(task_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS workspace_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    observed_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_obs_task_time
    ON workspace_observations(task_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS reconciliation_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reconcile_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    state TEXT NOT NULL,
    confidence TEXT NOT NULL,
    fence_token TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_task_time
    ON reconciliation_records(task_id, created_at DESC);
CREATE TABLE IF NOT EXISTS recovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    action TEXT NOT NULL,
    safe_to_dispatch INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchdog_leases (
    name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    host TEXT NOT NULL,
    started_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS action_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_attempts_task_time
    ON action_attempts(task_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_action_attempts_one_unresolved
    ON action_attempts(task_id)
    WHERE state IN ('ARMED', 'SUBMITTED', 'RECONCILE_REQUIRED');
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, 1, SCHEMA_VERSION}:
        conn.close()
        raise RuntimeError(
            f"unsupported CWS registry schema version {version}; expected <= {SCHEMA_VERSION}"
        )
    conn.executescript(SCHEMA)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return conn
