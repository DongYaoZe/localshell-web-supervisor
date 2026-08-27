from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from .actions import ActionIntent, prompt_digest, render_action_prompt
from .child_spawn import (
    ChildSpawnBlocked,
    ChromeUiaChildSpawnTransport,
    conversation_url_matches,
    owned_project_root_matches,
    tagged_project_url,
    web_project_id,
)
from .dispatcher import DispatchAction
from .models import ChildSpawnAttempt, ChildSpawnAttemptState, WorkerRecord, WorkerStatus, WorkerWindowBinding
from .uia import ChromeUiaProbe, UiaProbeUnavailable, conversation_id_from_url, normalize_url
from .uia_actions import ChromeUiaAckObserver, ChromeUiaActionTransport, UiaActionUnavailable
from .watchdog_host import pid_exists


DEFAULT_IDLE_CLOSE_S = 90.0
DEFAULT_MAX_WINDOWS = 4
DEFAULT_RATE_LIMIT_COOLDOWN_S = 120.0
DEFAULT_RECONCILE_HOLD_S = 90.0
DEFAULT_LEASE_TTL_S = 60.0
DEFAULT_PRESEND_READINESS_WAIT_S = 30.0


class ChatDispatchState(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    OPENING = "OPENING"
    WINDOW_BOUND = "WINDOW_BOUND"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    RETRY_WAIT = "RETRY_WAIT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_JOB_STATES = {
    ChatDispatchState.ACKNOWLEDGED,
    ChatDispatchState.FAILED,
    ChatDispatchState.CANCELLED,
}
PENDING_JOB_STATES = {
    ChatDispatchState.QUEUED,
    ChatDispatchState.CLAIMED,
    ChatDispatchState.OPENING,
    ChatDispatchState.WINDOW_BOUND,
    ChatDispatchState.SUBMITTING,
    ChatDispatchState.SUBMITTED,
    ChatDispatchState.RECONCILE_REQUIRED,
    ChatDispatchState.RETRY_WAIT,
}


class ChatPageState(StrEnum):
    OPEN = "OPEN"
    IDLE = "IDLE"
    CLOSED = "CLOSED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(slots=True)
class ChatDispatchJob:
    dispatch_id: str
    dispatch_key: str | None
    conversation_key: str
    conversation_url: str | None
    project_url: str | None
    prompt_text: str
    prompt_sha256: str
    nonce: str
    state: ChatDispatchState
    created_at: float
    updated_at: float
    next_attempt_at: float
    attempts: int
    idle_close_s: float
    max_windows: int
    window_handle: int | None = None
    browser_pid: int | None = None
    chrome_executable: str | None = None
    page_owned: bool = False
    submitted_at: float | None = None
    acknowledged_at: float | None = None
    last_error: str | None = None

    @property
    def wire_prompt(self) -> str:
        return render_action_prompt(self.prompt_text, self.nonce)


@dataclass(slots=True)
class ChatPage:
    page_id: str
    conversation_key: str
    conversation_url: str
    window_handle: int
    browser_pid: int
    chrome_executable: str
    owned: bool
    state: ChatPageState
    opened_at: float
    last_used_at: float
    idle_since: float | None
    close_after_at: float | None
    last_error: str | None = None


@dataclass(slots=True)
class PageIdentity:
    conversation_url: str
    window_handle: int
    browser_pid: int
    chrome_executable: str
    owned: bool


@dataclass(slots=True)
class BrowserSendResult:
    submitted: bool
    side_effect_possible: bool
    detail: str
    conversation_url: str | None = None
    rate_limited: bool = False


@dataclass(slots=True)
class BrowserAckResult:
    observed: bool
    nonce_occurrences: int
    generating: bool | None
    detail: str
    conversation_url: str | None = None


@dataclass(slots=True)
class BrowserCloseResult:
    closed: bool
    absent: bool
    ambiguous: bool
    detail: str


class ChatDispatchBrowser(Protocol):
    chrome_executable: str

    def find_existing(self, conversation_url: str) -> PageIdentity | None: ...
    def open_existing(self, conversation_url: str) -> PageIdentity: ...
    def open_new(self, project_url: str, owner_token: str, prompt_sha256: str) -> PageIdentity: ...
    def recover_new(self, project_url: str, owner_token: str, prompt_sha256: str) -> PageIdentity | None: ...
    def send_existing(self, job: ChatDispatchJob, page: ChatPage) -> BrowserSendResult: ...
    def send_new(self, job: ChatDispatchJob, page: ChatPage) -> BrowserSendResult: ...
    def observe_ack(self, job: ChatDispatchJob, page: ChatPage) -> BrowserAckResult: ...
    def current_url(self, page: ChatPage) -> str | None: ...
    def close_page(self, page: ChatPage) -> BrowserCloseResult: ...


def default_chat_dispatch_db_path() -> Path:
    explicit = os.environ.get("LWS_CHAT_DISPATCH_DB")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "LocalShellWebSupervisor" / "chat-dispatch.sqlite3"
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "localshell-web-supervisor" / "chat-dispatch.sqlite3"


def _connect(path: str | Path) -> sqlite3.Connection:
    db = Path(path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_conversations (
            conversation_key TEXT PRIMARY KEY,
            conversation_url TEXT,
            project_url TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_conversation_url
            ON chat_conversations(conversation_url)
            WHERE conversation_url IS NOT NULL;

        CREATE TABLE IF NOT EXISTS chat_dispatch_jobs (
            dispatch_id TEXT PRIMARY KEY,
            dispatch_key TEXT UNIQUE,
            conversation_key TEXT NOT NULL,
            conversation_url TEXT,
            project_url TEXT,
            prompt_text TEXT NOT NULL,
            prompt_sha256 TEXT NOT NULL,
            nonce TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            next_attempt_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            idle_close_s REAL NOT NULL,
            max_windows INTEGER NOT NULL,
            window_handle INTEGER,
            browser_pid INTEGER,
            chrome_executable TEXT,
            page_owned INTEGER NOT NULL DEFAULT 0,
            submitted_at REAL,
            acknowledged_at REAL,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_chat_jobs_state_time
            ON chat_dispatch_jobs(state, next_attempt_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_chat_jobs_conversation_time
            ON chat_dispatch_jobs(conversation_key, created_at);

        CREATE TABLE IF NOT EXISTS chat_dispatch_pages (
            page_id TEXT PRIMARY KEY,
            conversation_key TEXT NOT NULL,
            conversation_url TEXT NOT NULL,
            window_handle INTEGER NOT NULL,
            browser_pid INTEGER NOT NULL,
            chrome_executable TEXT NOT NULL,
            owned INTEGER NOT NULL,
            state TEXT NOT NULL,
            opened_at REAL NOT NULL,
            last_used_at REAL NOT NULL,
            idle_since REAL,
            close_after_at REAL,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_chat_pages_conversation
            ON chat_dispatch_pages(conversation_key, state, last_used_at DESC);

        CREATE TABLE IF NOT EXISTS chat_dispatch_leases (
            name TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            pid INTEGER NOT NULL,
            heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            max_windows INTEGER NOT NULL,
            idle_close_s REAL NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def _job_from_row(row: sqlite3.Row) -> ChatDispatchJob:
    return ChatDispatchJob(
        dispatch_id=str(row["dispatch_id"]),
        dispatch_key=row["dispatch_key"],
        conversation_key=str(row["conversation_key"]),
        conversation_url=row["conversation_url"],
        project_url=row["project_url"],
        prompt_text=str(row["prompt_text"]),
        prompt_sha256=str(row["prompt_sha256"]),
        nonce=str(row["nonce"]),
        state=ChatDispatchState(str(row["state"])),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        next_attempt_at=float(row["next_attempt_at"]),
        attempts=int(row["attempts"]),
        idle_close_s=float(row["idle_close_s"]),
        max_windows=int(row["max_windows"]),
        window_handle=row["window_handle"],
        browser_pid=row["browser_pid"],
        chrome_executable=row["chrome_executable"],
        page_owned=bool(row["page_owned"]),
        submitted_at=row["submitted_at"],
        acknowledged_at=row["acknowledged_at"],
        last_error=row["last_error"],
    )


def _page_from_row(row: sqlite3.Row) -> ChatPage:
    return ChatPage(
        page_id=str(row["page_id"]),
        conversation_key=str(row["conversation_key"]),
        conversation_url=str(row["conversation_url"]),
        window_handle=int(row["window_handle"]),
        browser_pid=int(row["browser_pid"]),
        chrome_executable=str(row["chrome_executable"]),
        owned=bool(row["owned"]),
        state=ChatPageState(str(row["state"])),
        opened_at=float(row["opened_at"]),
        last_used_at=float(row["last_used_at"]),
        idle_since=row["idle_since"],
        close_after_at=row["close_after_at"],
        last_error=row["last_error"],
    )


def job_payload(job: ChatDispatchJob) -> dict:
    data = asdict(job)
    data["state"] = job.state.value
    data.pop("prompt_text", None)
    data["prompt_bytes"] = len(job.prompt_text.encode("utf-8"))
    return data


def page_payload(page: ChatPage) -> dict:
    data = asdict(page)
    data["state"] = page.state.value
    return data


class ChatDispatchStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or default_chat_dispatch_db_path()).resolve()
        self._conn = _connect(self.path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ChatDispatchStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def enqueue(
        self,
        *,
        prompt: str,
        conversation_key: str | None = None,
        conversation_url: str | None = None,
        project_url: str | None = None,
        dispatch_key: str | None = None,
        idle_close_s: float = DEFAULT_IDLE_CLOSE_S,
        max_windows: int = DEFAULT_MAX_WINDOWS,
        now: float | None = None,
    ) -> ChatDispatchJob:
        prompt = str(prompt)
        if not prompt.strip():
            raise ValueError("prompt must be non-empty")
        if len(prompt.encode("utf-8")) > 256 * 1024:
            raise ValueError("prompt exceeds 256 KiB")
        current = time.time() if now is None else float(now)
        idle_close_s = max(1.0, float(idle_close_s))
        max_windows = max(1, min(int(max_windows), 16))
        conversation_url = str(conversation_url).strip() if conversation_url else None
        project_url = str(project_url).strip() if project_url else None
        if conversation_url and conversation_id_from_url(conversation_url) is None:
            raise ValueError("conversation_url must be a ChatGPT /c/ conversation URL")
        if project_url:
            web_project_id(project_url)
        key = str(conversation_key or "").strip()
        if not key:
            if conversation_url:
                key = f"conversation:{conversation_id_from_url(conversation_url)}"
            else:
                key = f"new:{uuid.uuid4().hex[:16]}"
        if len(key) > 200:
            raise ValueError("conversation_key must be <= 200 characters")
        dispatch_key = str(dispatch_key).strip() if dispatch_key else None
        if dispatch_key and len(dispatch_key) > 240:
            raise ValueError("dispatch_key must be <= 240 characters")

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            known = self._conn.execute(
                "SELECT conversation_url, project_url FROM chat_conversations WHERE conversation_key=?",
                (key,),
            ).fetchone()
            known_url = str(known["conversation_url"]) if known and known["conversation_url"] else None
            known_project = str(known["project_url"]) if known and known["project_url"] else None
            if known_url:
                if conversation_url and not conversation_url_matches(known_url, conversation_url):
                    raise ValueError("conversation_key is already bound to a different conversation")
                conversation_url = known_url
            if not project_url and known_project:
                project_url = known_project
            if not conversation_url and not project_url:
                raise ValueError(
                    "new conversation dispatch requires project_url unless conversation_key is already resolved"
                )
            if dispatch_key:
                existing = self._conn.execute(
                    "SELECT * FROM chat_dispatch_jobs WHERE dispatch_key=?", (dispatch_key,)
                ).fetchone()
                if existing is not None:
                    job = _job_from_row(existing)
                    expected = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                    if (
                        job.prompt_sha256 != expected
                        or job.conversation_key != key
                        or (conversation_url and job.conversation_url and not conversation_url_matches(job.conversation_url, conversation_url))
                    ):
                        raise ValueError("dispatch_key already exists for a different dispatch")
                    self._conn.rollback()
                    return job

            self._conn.execute(
                """INSERT INTO chat_conversations
                   (conversation_key, conversation_url, project_url, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(conversation_key) DO UPDATE SET
                     conversation_url=COALESCE(chat_conversations.conversation_url, excluded.conversation_url),
                     project_url=COALESCE(chat_conversations.project_url, excluded.project_url),
                     updated_at=excluded.updated_at""",
                (key, conversation_url, project_url, current, current),
            )
            dispatch_id = f"chat_{uuid.uuid4().hex[:16]}"
            nonce = uuid.uuid4().hex
            self._conn.execute(
                """INSERT INTO chat_dispatch_jobs
                   (dispatch_id, dispatch_key, conversation_key, conversation_url, project_url,
                    prompt_text, prompt_sha256, nonce, state, created_at, updated_at,
                    next_attempt_at, attempts, idle_close_s, max_windows)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    dispatch_id,
                    dispatch_key,
                    key,
                    conversation_url,
                    project_url,
                    prompt,
                    hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    nonce,
                    ChatDispatchState.QUEUED.value,
                    current,
                    current,
                    current,
                    idle_close_s,
                    max_windows,
                ),
            )
            self._conn.commit()
            return self.get_job(dispatch_id)
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def get_job(self, dispatch_id: str) -> ChatDispatchJob:
        row = self._conn.execute(
            "SELECT * FROM chat_dispatch_jobs WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown chat dispatch: {dispatch_id}")
        return _job_from_row(row)

    def list_jobs(self, limit: int = 50) -> list[ChatDispatchJob]:
        rows = self._conn.execute(
            "SELECT * FROM chat_dispatch_jobs ORDER BY rowid DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [_job_from_row(row) for row in rows]

    def list_pages(self, *, include_closed: bool = False) -> list[ChatPage]:
        sql = "SELECT * FROM chat_dispatch_pages"
        params: tuple[object, ...] = ()
        if not include_closed:
            sql += " WHERE state != ?"
            params = (ChatPageState.CLOSED.value,)
        sql += " ORDER BY last_used_at DESC"
        return [_page_from_row(row) for row in self._conn.execute(sql, params).fetchall()]

    def page_for_conversation(self, conversation_key: str) -> ChatPage | None:
        row = self._conn.execute(
            """SELECT * FROM chat_dispatch_pages
               WHERE conversation_key=? AND state IN (?, ?)
               ORDER BY last_used_at DESC LIMIT 1""",
            (conversation_key, ChatPageState.OPEN.value, ChatPageState.IDLE.value),
        ).fetchone()
        return _page_from_row(row) if row else None

    def upsert_page(
        self,
        *,
        conversation_key: str,
        identity: PageIdentity,
        now: float | None = None,
    ) -> ChatPage:
        current = time.time() if now is None else float(now)
        page_id = f"hwnd:{identity.window_handle}:pid:{identity.browser_pid}"
        self._conn.execute(
            """INSERT INTO chat_dispatch_pages
               (page_id, conversation_key, conversation_url, window_handle, browser_pid,
                chrome_executable, owned, state, opened_at, last_used_at, idle_since, close_after_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
               ON CONFLICT(page_id) DO UPDATE SET
                 conversation_key=excluded.conversation_key,
                 conversation_url=excluded.conversation_url,
                 chrome_executable=excluded.chrome_executable,
                 owned=CASE WHEN chat_dispatch_pages.owned=1 THEN 1 ELSE excluded.owned END,
                 state=excluded.state,
                 last_used_at=excluded.last_used_at,
                 idle_since=NULL,
                 close_after_at=NULL,
                 last_error=NULL""",
            (
                page_id,
                conversation_key,
                identity.conversation_url,
                int(identity.window_handle),
                int(identity.browser_pid),
                identity.chrome_executable,
                1 if identity.owned else 0,
                ChatPageState.OPEN.value,
                current,
                current,
            ),
        )
        self._conn.commit()
        return self.get_page(page_id)

    def get_page(self, page_id: str) -> ChatPage:
        row = self._conn.execute(
            "SELECT * FROM chat_dispatch_pages WHERE page_id=?", (page_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown chat page: {page_id}")
        return _page_from_row(row)

    def update_page_url(self, page_id: str, conversation_url: str, *, now: float | None = None) -> ChatPage:
        current = time.time() if now is None else float(now)
        self._conn.execute(
            """UPDATE chat_dispatch_pages
               SET conversation_url=?, state=?, last_used_at=?, idle_since=NULL,
                   close_after_at=NULL, last_error=NULL WHERE page_id=?""",
            (conversation_url, ChatPageState.OPEN.value, current, page_id),
        )
        self._conn.commit()
        return self.get_page(page_id)

    def mark_page_open(self, page_id: str, *, now: float | None = None) -> ChatPage:
        current = time.time() if now is None else float(now)
        self._conn.execute(
            """UPDATE chat_dispatch_pages
               SET state=?, last_used_at=?, idle_since=NULL, close_after_at=NULL, last_error=NULL
               WHERE page_id=?""",
            (ChatPageState.OPEN.value, current, page_id),
        )
        self._conn.commit()
        return self.get_page(page_id)

    def mark_page_idle(self, page_id: str, idle_close_s: float, *, now: float | None = None) -> ChatPage:
        current = time.time() if now is None else float(now)
        page = self.get_page(page_id)
        idle_since = page.idle_since if page.state == ChatPageState.IDLE and page.idle_since is not None else current
        close_after = idle_since + max(1.0, float(idle_close_s)) if page.owned else None
        self._conn.execute(
            """UPDATE chat_dispatch_pages
               SET state=?, idle_since=?, close_after_at=?, last_used_at=? WHERE page_id=?""",
            (ChatPageState.IDLE.value, idle_since, close_after, current, page_id),
        )
        self._conn.commit()
        return self.get_page(page_id)

    def mark_page_closed(self, page_id: str, *, ambiguous: bool = False, error: str | None = None) -> ChatPage:
        state = ChatPageState.AMBIGUOUS if ambiguous else ChatPageState.CLOSED
        self._conn.execute(
            "UPDATE chat_dispatch_pages SET state=?, last_error=?, close_after_at=NULL WHERE page_id=?",
            (state.value, error, page_id),
        )
        self._conn.commit()
        return self.get_page(page_id)

    def bind_conversation(self, conversation_key: str, conversation_url: str, *, now: float | None = None) -> None:
        current = time.time() if now is None else float(now)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            conflicting = self._conn.execute(
                "SELECT conversation_key FROM chat_conversations WHERE conversation_url=? AND conversation_key != ?",
                (conversation_url, conversation_key),
            ).fetchone()
            if conflicting is not None:
                raise RuntimeError(
                    f"conversation URL is already owned by key {conflicting['conversation_key']}"
                )
            self._conn.execute(
                "UPDATE chat_conversations SET conversation_url=?, updated_at=? WHERE conversation_key=?",
                (conversation_url, current, conversation_key),
            )
            self._conn.execute(
                "UPDATE chat_dispatch_jobs SET conversation_url=?, updated_at=? WHERE conversation_key=? AND conversation_url IS NULL",
                (conversation_url, current, conversation_key),
            )
            self._conn.commit()
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def update_job(
        self,
        dispatch_id: str,
        *,
        state: ChatDispatchState | None = None,
        now: float | None = None,
        next_attempt_at: float | None = None,
        attempts: int | None = None,
        window_handle: int | None = None,
        browser_pid: int | None = None,
        chrome_executable: str | None = None,
        page_owned: bool | None = None,
        conversation_url: str | None = None,
        submitted_at: float | None = None,
        acknowledged_at: float | None = None,
        last_error: str | None = None,
    ) -> ChatDispatchJob:
        current = time.time() if now is None else float(now)
        updates = ["updated_at=?"]
        values: list[object] = [current]
        mapping = {
            "state": state.value if state is not None else None,
            "next_attempt_at": next_attempt_at,
            "attempts": attempts,
            "window_handle": window_handle,
            "browser_pid": browser_pid,
            "chrome_executable": chrome_executable,
            "page_owned": (1 if page_owned else 0) if page_owned is not None else None,
            "conversation_url": conversation_url,
            "submitted_at": submitted_at,
            "acknowledged_at": acknowledged_at,
        }
        for name, value in mapping.items():
            if value is not None:
                updates.append(f"{name}=?")
                values.append(value)
        if last_error is not None:
            updates.append("last_error=?")
            values.append(last_error)
        elif state in {ChatDispatchState.QUEUED, ChatDispatchState.SUBMITTED, ChatDispatchState.ACKNOWLEDGED}:
            updates.append("last_error=NULL")
        values.append(dispatch_id)
        self._conn.execute(
            f"UPDATE chat_dispatch_jobs SET {', '.join(updates)} WHERE dispatch_id=?", values
        )
        self._conn.commit()
        return self.get_job(dispatch_id)

    def cancel(self, dispatch_id: str) -> ChatDispatchJob:
        job = self.get_job(dispatch_id)
        if job.state in TERMINAL_JOB_STATES:
            return job
        if job.state in {ChatDispatchState.SUBMITTING, ChatDispatchState.RECONCILE_REQUIRED, ChatDispatchState.SUBMITTED}:
            raise RuntimeError("cannot cancel a dispatch whose external send may already have happened")
        return self.update_job(dispatch_id, state=ChatDispatchState.CANCELLED)

    def eligible_jobs(self, *, now: float | None = None, limit: int = 100) -> list[ChatDispatchJob]:
        current = time.time() if now is None else float(now)
        rows = self._conn.execute(
            """SELECT * FROM chat_dispatch_jobs
               WHERE state IN (?, ?, ?, ?, ?, ?, ?) AND next_attempt_at <= ?
               ORDER BY rowid LIMIT ?""",
            (
                ChatDispatchState.QUEUED.value,
                ChatDispatchState.OPENING.value,
                ChatDispatchState.WINDOW_BOUND.value,
                ChatDispatchState.SUBMITTING.value,
                ChatDispatchState.RETRY_WAIT.value,
                ChatDispatchState.SUBMITTED.value,
                ChatDispatchState.RECONCILE_REQUIRED.value,
                current,
                max(1, min(limit, 500)),
            ),
        ).fetchall()
        return [_job_from_row(row) for row in rows]

    def has_older_unfinished(self, job: ChatDispatchJob) -> bool:
        # rowid is the durable FIFO sequence. Wall-clock timestamps can collide when a model
        # enqueues several prompts in the same scheduler tick and therefore cannot define order.
        row = self._conn.execute(
            """SELECT 1 FROM chat_dispatch_jobs
               WHERE conversation_key=?
                 AND rowid < (SELECT rowid FROM chat_dispatch_jobs WHERE dispatch_id=?)
                 AND state NOT IN (?, ?, ?)
               LIMIT 1""",
            (
                job.conversation_key,
                job.dispatch_id,
                ChatDispatchState.ACKNOWLEDGED.value,
                ChatDispatchState.FAILED.value,
                ChatDispatchState.CANCELLED.value,
            ),
        ).fetchone()
        return row is not None

    def has_pending_for_conversation(self, conversation_key: str) -> bool:
        placeholders = ",".join("?" for _ in PENDING_JOB_STATES)
        row = self._conn.execute(
            f"SELECT 1 FROM chat_dispatch_jobs WHERE conversation_key=? AND state IN ({placeholders}) LIMIT 1",
            (conversation_key, *(state.value for state in PENDING_JOB_STATES)),
        ).fetchone()
        return row is not None

    def pending_count(self) -> int:
        placeholders = ",".join("?" for _ in PENDING_JOB_STATES)
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM chat_dispatch_jobs WHERE state IN ({placeholders})",
            tuple(state.value for state in PENDING_JOB_STATES),
        ).fetchone()
        return int(row[0])

    def owned_open_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM chat_dispatch_pages WHERE owned=1 AND state IN (?, ?)",
            (ChatPageState.OPEN.value, ChatPageState.IDLE.value),
        ).fetchone()
        return int(row[0])

    def acquire_lease(
        self,
        *,
        owner_id: str,
        pid: int,
        max_windows: int,
        idle_close_s: float,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT owner_id, pid, expires_at FROM chat_dispatch_leases WHERE name='default'"
            ).fetchone()
            if row is not None and str(row["owner_id"]) != owner_id:
                lease_is_fresh = float(row["expires_at"]) > current
                lease_pid = int(row["pid"] or 0)
                lease_process_alive = lease_pid > 0 and pid_exists(lease_pid)
                if lease_is_fresh or lease_process_alive:
                    # TTL expiry alone is not authority to replace a process that can still
                    # perform browser side effects. A stalled live worker must be stopped or
                    # reconciled explicitly before a new owner is allowed to take over.
                    self._conn.rollback()
                    return False
            self._conn.execute(
                """INSERT INTO chat_dispatch_leases
                   (name, owner_id, pid, heartbeat_at, expires_at, max_windows, idle_close_s)
                   VALUES ('default', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     owner_id=excluded.owner_id,
                     pid=excluded.pid,
                     heartbeat_at=excluded.heartbeat_at,
                     expires_at=excluded.expires_at,
                     max_windows=excluded.max_windows,
                     idle_close_s=excluded.idle_close_s""",
                (
                    owner_id,
                    int(pid),
                    current,
                    current + max(5.0, float(ttl_s)),
                    max(1, int(max_windows)),
                    max(1.0, float(idle_close_s)),
                ),
            )
            self._conn.commit()
            return True
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def heartbeat_lease(self, owner_id: str, *, ttl_s: float = DEFAULT_LEASE_TTL_S) -> bool:
        current = time.time()
        cursor = self._conn.execute(
            """UPDATE chat_dispatch_leases SET heartbeat_at=?, expires_at=?
               WHERE name='default' AND owner_id=?""",
            (current, current + max(5.0, float(ttl_s)), owner_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def release_lease(self, owner_id: str) -> None:
        self._conn.execute(
            "DELETE FROM chat_dispatch_leases WHERE name='default' AND owner_id=?", (owner_id,)
        )
        self._conn.commit()

    def lease(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM chat_dispatch_leases WHERE name='default'"
        ).fetchone()
        return dict(row) if row else None


class ChromeChatDispatchBrowser:
    def __init__(self, *, chrome_executable: str | None = None, timeout_s: float = 8.0) -> None:
        probe = ChromeUiaProbe(chrome_executable=chrome_executable, timeout_s=timeout_s)
        if not probe.chrome_executable:
            raise RuntimeError("Google Chrome executable could not be resolved")
        self.probe = probe
        self.chrome_executable = probe.chrome_executable
        self.timeout_s = max(1.0, float(timeout_s))
        self.spawn = ChromeUiaChildSpawnTransport(
            chrome_executable=self.chrome_executable,
            enabled=True,
            timeout_s=self.timeout_s,
            open_timeout_s=12.0,
            conversation_timeout_s=20.0,
        )

    @staticmethod
    def _is_presend_readiness_failure(result: BrowserSendResult) -> bool:
        if result.submitted or result.side_effect_possible or result.rate_limited:
            return False
        detail = (result.detail or "").casefold()
        return any(
            marker in detail
            for marker in (
                "web chat document is not present",
                "prompt-textarea is not present",
            )
        )

    def _wait_for_presend_readiness(self, submitter) -> BrowserSendResult:
        """Retry only proven pre-Send UI readiness failures for a bounded window."""

        deadline = time.time() + DEFAULT_PRESEND_READINESS_WAIT_S
        last = BrowserSendResult(False, False, "browser composer readiness not observed")
        while time.time() < deadline:
            last = submitter()
            if not self._is_presend_readiness_failure(last):
                return last
            time.sleep(0.2)
        return last

    def _discover_matching(self, conversation_url: str) -> list[dict]:
        target_id = conversation_id_from_url(conversation_url)
        if target_id is None:
            return []
        matches = []
        for item in self.probe.discover_conversations():
            if str(item.get("conversation_id") or "") == target_id:
                matches.append(item)
        return matches

    def find_existing(self, conversation_url: str) -> PageIdentity | None:
        matches = self._discover_matching(conversation_url)
        if not matches:
            return None
        exact = [
            item
            for item in matches
            if normalize_url(str(item.get("url") or "")) == normalize_url(conversation_url)
        ]
        candidates = exact or matches
        if len(candidates) != 1:
            raise RuntimeError(
                f"multiple normal-Chrome windows show conversation {conversation_id_from_url(conversation_url)}"
            )
        item = candidates[0]
        return PageIdentity(
            conversation_url=str(item["url"]),
            window_handle=int(item["window_handle"]),
            browser_pid=int(item["browser_pid"]),
            chrome_executable=self.chrome_executable,
            owned=False,
        )

    def open_existing(self, conversation_url: str) -> PageIdentity:
        existing = self.find_existing(conversation_url)
        if existing is not None:
            return existing
        subprocess.Popen([self.chrome_executable, "--new-window", conversation_url], close_fds=True)
        deadline = time.time() + 12.0
        while time.time() < deadline:
            found = self.find_existing(conversation_url)
            if found is not None:
                found.owned = True
                self._activate_exact_web_contents(
                    found,
                    lambda current: conversation_url_matches(current, conversation_url),
                    label="new owned conversation",
                )
                return found
            time.sleep(0.1)
        raise RuntimeError("opened existing conversation window was not observed within 12 seconds")

    def _spawn_attempt(
        self,
        *,
        owner_token: str,
        project_url: str,
        prompt_sha256: str,
        state: ChildSpawnAttemptState,
        window_handle: int | None = None,
        browser_pid: int | None = None,
        conversation_url: str | None = None,
    ) -> ChildSpawnAttempt:
        return ChildSpawnAttempt(
            attempt_id=f"chatq_{uuid.uuid4().hex[:12]}",
            child_task_id=f"chatq:{owner_token}",
            state=state,
            owner_token=owner_token,
            project_url=project_url,
            project_id=web_project_id(project_url),
            tagged_project_url=tagged_project_url(project_url, owner_token),
            prompt_sha256=prompt_sha256,
            chrome_executable=self.chrome_executable,
            created_at=time.time(),
            updated_at=time.time(),
            window_handle=window_handle,
            browser_pid=browser_pid,
            conversation_url=conversation_url,
        )

    def _observe_page_identity_url(self, identity: PageIdentity) -> str | None:
        # _observe_bound_url validates exact HWND/PID/Chrome executable before returning the
        # address. Its project fields are irrelevant for this read-only exact-window probe.
        dummy_project = "https://chatgpt.com/g/g-p-00000000000000000000000000000000"
        probe = ChildSpawnAttempt(
            attempt_id=f"chatq_focus_{uuid.uuid4().hex[:8]}",
            child_task_id="chatq-focus",
            state=ChildSpawnAttemptState.WINDOW_BOUND,
            owner_token="chat-dispatch-focus",
            project_url=dummy_project,
            project_id=web_project_id(dummy_project),
            tagged_project_url=tagged_project_url(dummy_project, "chat-dispatch-focus"),
            prompt_sha256="focus",
            chrome_executable=identity.chrome_executable,
            created_at=time.time(),
            updated_at=time.time(),
            window_handle=identity.window_handle,
            browser_pid=identity.browser_pid,
            conversation_url=identity.conversation_url,
        )
        observed = self.spawn._observe_bound_url(probe)  # noqa: SLF001 - exact-HWND primitive.
        return observed.url or None

    def _activate_exact_web_contents(self, identity: PageIdentity, validator, *, label: str) -> None:
        """Activate renderer accessibility on one dispatcher-owned Chrome window.

        Normal Chrome may expose only native browser controls on a freshly opened window until
        focus cycles into web contents. F6 is safe to replay because it neither edits the
        composer nor sends a message. Exact HWND/PID/executable plus caller-specific URL identity
        are revalidated before and after foregrounding so the global key event cannot target an
        unrelated user window.
        """
        if os.name != "nt":
            return
        before = self._observe_page_identity_url(identity)
        if not before or not validator(before):
            raise RuntimeError(f"{label} identity changed before accessibility activation")
        user32 = ctypes.windll.user32
        if not bool(user32.SetForegroundWindow(int(identity.window_handle))):
            raise RuntimeError(f"could not foreground exact {label} Chrome window")
        time.sleep(0.05)
        if int(user32.GetForegroundWindow()) != int(identity.window_handle):
            raise RuntimeError(f"foreground changed before {label} accessibility activation")
        after = self._observe_page_identity_url(identity)
        if not after or not validator(after):
            raise RuntimeError(f"{label} identity changed after foregrounding")
        vk_f6 = 0x75
        keyeventf_keyup = 0x0002
        user32.keybd_event(vk_f6, 0, 0, 0)
        user32.keybd_event(vk_f6, 0, keyeventf_keyup, 0)
        time.sleep(0.1)

    def open_new(self, project_url: str, owner_token: str, prompt_sha256: str) -> PageIdentity:
        attempt = self._spawn_attempt(
            owner_token=owner_token,
            project_url=project_url,
            prompt_sha256=prompt_sha256,
            state=ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
        )
        result = self.spawn.open_authorized(attempt)
        if not result.changed or not result.window_handle or not result.browser_pid or not result.url:
            raise RuntimeError(result.detail or "new conversation project window did not open safely")
        identity = PageIdentity(
            conversation_url=result.url,
            window_handle=int(result.window_handle),
            browser_pid=int(result.browser_pid),
            chrome_executable=self.chrome_executable,
            owned=True,
        )
        self._activate_exact_web_contents(
            identity,
            lambda current: owned_project_root_matches(current, attempt),
            label="new owned project",
        )
        return identity

    def recover_new(self, project_url: str, owner_token: str, prompt_sha256: str) -> PageIdentity | None:
        attempt = self._spawn_attempt(
            owner_token=owner_token,
            project_url=project_url,
            prompt_sha256=prompt_sha256,
            state=ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
        )
        result = self.spawn.observe_owned_project(attempt)
        if not result.window_handle or not result.browser_pid or not result.url:
            return None
        return PageIdentity(
            conversation_url=result.url,
            window_handle=int(result.window_handle),
            browser_pid=int(result.browser_pid),
            chrome_executable=self.chrome_executable,
            owned=True,
        )

    def send_existing(self, job: ChatDispatchJob, page: ChatPage) -> BrowserSendResult:
        binding = WorkerWindowBinding(
            worker_id=page.page_id,
            window_handle=page.window_handle,
            browser_pid=page.browser_pid,
            chrome_executable=page.chrome_executable,
            conversation_url=page.conversation_url,
            source="windows_uia_chrome",
            bound_at=time.time() - 0.01,
            observed_at=time.time(),
            expires_at=time.time() + 60.0,
        )
        transport = ChromeUiaActionTransport.from_binding(
            binding,
            expected_worker_id=page.page_id,
            conversation_url=page.conversation_url,
            enabled=True,
        )
        intent = ActionIntent(
            attempt_id=job.dispatch_id,
            task_id=f"chat-dispatch:{job.conversation_key}",
            worker_id=page.page_id,
            action=DispatchAction.CONTINUE_CURRENT_WORKER.value,
            prompt=job.wire_prompt,
            prompt_hash=prompt_digest(job.wire_prompt),
            nonce=job.nonce,
            fence_token=job.dispatch_id,
            fence_version=1,
        )
        def submit_once() -> BrowserSendResult:
            result = transport.submit(intent)
            return BrowserSendResult(
                submitted=result.submitted,
                side_effect_possible=result.side_effect_possible,
                detail=result.detail,
                conversation_url=page.conversation_url,
                rate_limited="Too many requests" in (result.detail or ""),
            )

        return self._wait_for_presend_readiness(submit_once)

    def send_new(self, job: ChatDispatchJob, page: ChatPage) -> BrowserSendResult:
        if not job.project_url:
            raise RuntimeError("new conversation job is missing project_url")
        owner_token = f"chat-dispatch:{job.dispatch_id}:{job.nonce}"
        attempt = self._spawn_attempt(
            owner_token=owner_token,
            project_url=job.project_url,
            prompt_sha256=prompt_digest(job.wire_prompt),
            state=ChildSpawnAttemptState.PROMPT_SUBMITTED,
            window_handle=page.window_handle,
            browser_pid=page.browser_pid,
        )
        # The page was opened using the same deterministic owner token. Rebuild the exact tagged
        # project URL rather than storing it as a second source of truth.
        def submit_once() -> BrowserSendResult:
            result = self.spawn.send_authorized(attempt, job.wire_prompt)
            return BrowserSendResult(
                submitted=bool(result.submitted),
                side_effect_possible=bool(result.side_effect_possible),
                detail=result.detail,
                conversation_url=None,
                rate_limited=bool(result.rate_limited),
            )

        ready = self._wait_for_presend_readiness(submit_once)
        result = ready
        if not result.submitted:
            return result
        delivery = self.spawn.wait_for_delivery(attempt, job.wire_prompt)
        url = delivery.url if conversation_id_from_url(delivery.url) else None
        return BrowserSendResult(
            submitted=True,
            side_effect_possible=True,
            detail=delivery.detail or result.detail,
            conversation_url=url,
            rate_limited=result.rate_limited,
        )

    def observe_ack(self, job: ChatDispatchJob, page: ChatPage) -> BrowserAckResult:
        if conversation_id_from_url(page.conversation_url) is None:
            return BrowserAckResult(False, 0, None, "page has not reached a conversation URL")
        observer = ChromeUiaAckObserver(chrome_executable=page.chrome_executable, timeout_s=self.timeout_s)
        try:
            observed = observer.observe(
                worker_id=page.page_id,
                conversation_url=page.conversation_url,
                expected_nonce=job.nonce,
                expected_hwnd=page.window_handle,
                expected_browser_pid=page.browser_pid,
            )
        except (UiaActionUnavailable, RuntimeError) as exc:
            return BrowserAckResult(False, 0, None, f"ack observation unavailable: {type(exc).__name__}")
        return BrowserAckResult(
            observed=True,
            nonce_occurrences=observed.nonce_occurrences,
            generating=observed.generating,
            detail="exact-window nonce observation",
            conversation_url=observed.url,
        )

    def current_url(self, page: ChatPage) -> str | None:
        # Read the address bar from the exact HWND without requiring the URL to equal the
        # previous value. New conversations legitimately navigate from a tagged project root
        # into /c/<id>, so an exact-old-URL probe would hide the transition we must persist.
        probe_attempt = ChildSpawnAttempt(
            attempt_id=f"chatq_probe_{uuid.uuid4().hex[:8]}",
            child_task_id=f"chatq-probe:{page.conversation_key}",
            state=ChildSpawnAttemptState.WINDOW_BOUND,
            owner_token="chat-dispatch-probe",
            project_url="https://chatgpt.com/",
            project_id="probe",
            tagged_project_url="https://chatgpt.com/",
            prompt_sha256="probe",
            chrome_executable=page.chrome_executable,
            created_at=time.time(),
            updated_at=time.time(),
            window_handle=page.window_handle,
            browser_pid=page.browser_pid,
            conversation_url=page.conversation_url,
        )
        try:
            result = self.spawn._observe_bound_url(probe_attempt)  # noqa: SLF001 - same-package exact HWND primitive.
        except (UiaProbeUnavailable, RuntimeError):
            return None
        return result.url or None

    def close_page(self, page: ChatPage) -> BrowserCloseResult:
        # Chrome canonicalizes a newly opened tagged project root from /g/<slug>#owner to
        # /g/<slug>/project#owner. Re-read the exact bound HWND and accept that URL only when
        # it is still the same conversation OR the same LWS-owned project root with the exact
        # owner token. This keeps close fail-closed without depending on literal route spelling.
        current_url = self.current_url(page)
        if not current_url:
            return BrowserCloseResult(
                False, False, True, "exact owned-page URL unavailable before close"
            )
        safe_url = page.conversation_url
        if normalize_url(current_url) != normalize_url(page.conversation_url):
            if conversation_url_matches(current_url, page.conversation_url):
                safe_url = current_url
            else:
                parsed = urlsplit(page.conversation_url)
                owner_prefix = "lws-child="
                owner_token = (
                    parsed.fragment[len(owner_prefix):]
                    if parsed.fragment.startswith(owner_prefix)
                    else ""
                )
                project_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                try:
                    project_id = web_project_id(project_url)
                except ValueError:
                    project_id = ""
                if owner_token and project_id:
                    probe_attempt = ChildSpawnAttempt(
                        attempt_id=f"chatq_close_{uuid.uuid4().hex[:8]}",
                        child_task_id=f"chatq-close:{page.conversation_key}",
                        state=ChildSpawnAttemptState.WINDOW_BOUND,
                        owner_token=owner_token,
                        project_url=project_url,
                        project_id=project_id,
                        tagged_project_url=page.conversation_url,
                        prompt_sha256="close",
                        chrome_executable=page.chrome_executable,
                        created_at=page.opened_at,
                        updated_at=time.time(),
                        window_handle=page.window_handle,
                        browser_pid=page.browser_pid,
                    )
                    if owned_project_root_matches(current_url, probe_attempt):
                        safe_url = current_url
                    else:
                        return BrowserCloseResult(
                            False, False, True, "exact owned-page project identity changed before close"
                        )
                else:
                    return BrowserCloseResult(
                        False, False, True, "exact owned-page conversation identity changed before close"
                    )

        worker = WorkerRecord(
            worker_id=page.page_id,
            task_id="chat-dispatch",
            conversation_url=safe_url,
            conversation_id=conversation_id_from_url(safe_url),
            status=WorkerStatus.ACTIVE,
            started_at=page.opened_at,
        )
        binding = WorkerWindowBinding(
            worker_id=page.page_id,
            window_handle=page.window_handle,
            browser_pid=page.browser_pid,
            chrome_executable=page.chrome_executable,
            conversation_url=safe_url,
            source="windows_uia_chrome",
            bound_at=page.opened_at,
            observed_at=time.time(),
            expires_at=time.time() + 60.0,
        )
        result = self.spawn.close_worker_binding_authorized(worker=worker, binding=binding)
        return BrowserCloseResult(
            closed=bool(result.changed),
            absent=(not result.changed and not result.side_effect_possible and "already absent" in (result.detail or "")),
            ambiguous=bool(result.side_effect_possible and not result.changed),
            detail=result.detail,
        )


class ChatDispatchEngine:
    def __init__(
        self,
        store: ChatDispatchStore,
        browser: ChatDispatchBrowser,
        *,
        max_windows: int = DEFAULT_MAX_WINDOWS,
        idle_close_s: float = DEFAULT_IDLE_CLOSE_S,
        rate_limit_cooldown_s: float = DEFAULT_RATE_LIMIT_COOLDOWN_S,
    ) -> None:
        self.store = store
        self.browser = browser
        self.max_windows = max(1, min(int(max_windows), 16))
        self.idle_close_s = max(1.0, float(idle_close_s))
        self.rate_limit_cooldown_s = max(1.0, float(rate_limit_cooldown_s))

    def _page(self, job: ChatDispatchJob) -> ChatPage | None:
        page = self.store.page_for_conversation(job.conversation_key)
        if page is None:
            return None
        current = self.browser.current_url(page)
        if current is None:
            self.store.mark_page_closed(page.page_id, error="owned/bound window is no longer observable")
            return None
        if job.conversation_url and conversation_id_from_url(current) == conversation_id_from_url(job.conversation_url):
            if normalize_url(current) != normalize_url(page.conversation_url):
                page = self.store.update_page_url(page.page_id, current)
            return page
        if not job.conversation_url and job.project_url:
            return page
        self.store.mark_page_closed(page.page_id, ambiguous=True, error="page identity changed")
        return None

    def _ensure_page(self, job: ChatDispatchJob) -> ChatPage | None:
        page = self._page(job)
        if page is not None:
            # A prior idle grace belongs to the previous queue drain. Reusing the page for a
            # new job must cancel that timer before any send can occur.
            return self.store.mark_page_open(page.page_id)

        owner_token = f"chat-dispatch:{job.dispatch_id}:{job.nonce}"
        identity: PageIdentity | None = None

        if job.state == ChatDispatchState.OPENING:
            # OPENING is write-ahead authority for a browser-open side effect. After a crash we
            # only reconcile what is now observable; we never repeat an ambiguous open.
            if job.conversation_url:
                identity = self.browser.find_existing(job.conversation_url)
                if identity is not None:
                    # We can prove conversation identity but not that this process created the
                    # window, so recovered existing-conversation pages are conservatively borrowed.
                    identity.owned = False
            elif job.project_url:
                identity = self.browser.recover_new(
                    job.project_url, owner_token, prompt_digest(job.wire_prompt)
                )
            if identity is None:
                self.store.update_job(
                    job.dispatch_id,
                    state=ChatDispatchState.RECONCILE_REQUIRED,
                    last_error="browser open outcome is unresolved; no automatic replay",
                )
                return None
        elif job.conversation_url:
            identity = self.browser.find_existing(job.conversation_url)
            if identity is None:
                if self.store.owned_open_count() >= self.max_windows:
                    return None
                self.store.update_job(job.dispatch_id, state=ChatDispatchState.OPENING)
                try:
                    identity = self.browser.open_existing(job.conversation_url)
                except BaseException as exc:
                    self.store.update_job(
                        job.dispatch_id,
                        state=ChatDispatchState.RECONCILE_REQUIRED,
                        last_error=f"existing-conversation window open outcome unresolved: {type(exc).__name__}: {exc}",
                    )
                    return None
        else:
            if not job.project_url:
                raise RuntimeError("unresolved conversation has no project_url")
            if self.store.owned_open_count() >= self.max_windows:
                return None
            self.store.update_job(job.dispatch_id, state=ChatDispatchState.OPENING)
            try:
                identity = self.browser.open_new(
                    job.project_url, owner_token, prompt_digest(job.wire_prompt)
                )
            except BaseException as exc:
                self.store.update_job(
                    job.dispatch_id,
                    state=ChatDispatchState.RECONCILE_REQUIRED,
                    last_error=f"new-conversation window open outcome unresolved: {type(exc).__name__}: {exc}",
                )
                return None

        assert identity is not None
        page = self.store.upsert_page(conversation_key=job.conversation_key, identity=identity)
        self.store.update_job(
            job.dispatch_id,
            state=ChatDispatchState.WINDOW_BOUND,
            window_handle=page.window_handle,
            browser_pid=page.browser_pid,
            chrome_executable=page.chrome_executable,
            page_owned=page.owned,
        )
        return page

    def _submit(self, job: ChatDispatchJob, page: ChatPage) -> None:
        current = time.time()
        job = self.store.update_job(
            job.dispatch_id,
            state=ChatDispatchState.SUBMITTING,
            attempts=job.attempts + 1,
            window_handle=page.window_handle,
            browser_pid=page.browser_pid,
            chrome_executable=page.chrome_executable,
            page_owned=page.owned,
        )
        try:
            result = (
                self.browser.send_existing(job, page)
                if job.conversation_url
                else self.browser.send_new(job, page)
            )
        except BaseException as exc:
            self.store.update_job(
                job.dispatch_id,
                state=ChatDispatchState.RECONCILE_REQUIRED,
                last_error=f"send raised {type(exc).__name__}: {exc}",
            )
            return
        if result.conversation_url:
            self.store.bind_conversation(job.conversation_key, result.conversation_url)
            page = self.store.update_page_url(page.page_id, result.conversation_url)
        if result.submitted:
            self.store.update_job(
                job.dispatch_id,
                state=ChatDispatchState.SUBMITTED,
                conversation_url=result.conversation_url or job.conversation_url,
                submitted_at=current,
                last_error=None,
            )
            return
        if result.side_effect_possible:
            self.store.update_job(
                job.dispatch_id,
                state=ChatDispatchState.RECONCILE_REQUIRED,
                last_error=result.detail or "send outcome is ambiguous",
            )
            return
        if result.rate_limited:
            self.store.update_job(
                job.dispatch_id,
                state=ChatDispatchState.RETRY_WAIT,
                next_attempt_at=current + self.rate_limit_cooldown_s,
                last_error=result.detail or "rate limited before send",
            )
            return
        self.store.update_job(
            job.dispatch_id,
            state=ChatDispatchState.FAILED,
            last_error=result.detail or "send was safely rejected before side effect",
        )

    def _reconcile(self, job: ChatDispatchJob) -> None:
        page = self._page(job)
        if page is None:
            return
        if not job.conversation_url:
            current = self.browser.current_url(page)
            if current and conversation_id_from_url(current):
                self.store.bind_conversation(job.conversation_key, current)
                self.store.update_page_url(page.page_id, current)
                job = self.store.update_job(
                    job.dispatch_id,
                    state=ChatDispatchState.SUBMITTED,
                    conversation_url=current,
                    submitted_at=job.submitted_at or time.time(),
                )
                page = self.store.get_page(page.page_id)
            else:
                return
        ack = self.browser.observe_ack(job, page)
        if not ack.observed:
            return
        if ack.nonce_occurrences > 1:
            self.store.update_job(
                job.dispatch_id,
                state=ChatDispatchState.RECONCILE_REQUIRED,
                last_error=f"nonce appeared {ack.nonce_occurrences} times; duplicate delivery requires human review",
            )
            self.store.mark_page_closed(page.page_id, ambiguous=True, error="duplicate nonce evidence")
            return
        if ack.nonce_occurrences == 1 and ack.generating is False:
            now = time.time()
            self.store.update_job(
                job.dispatch_id,
                state=ChatDispatchState.ACKNOWLEDGED,
                acknowledged_at=now,
                last_error=None,
            )
            if not self.store.has_pending_for_conversation(job.conversation_key):
                self.store.mark_page_idle(page.page_id, job.idle_close_s, now=now)

    def _close_idle_pages(self) -> None:
        now = time.time()
        for page in self.store.list_pages():
            if not page.owned:
                continue
            if self.store.has_pending_for_conversation(page.conversation_key):
                continue
            if page.state == ChatPageState.OPEN:
                self.store.mark_page_idle(page.page_id, self.idle_close_s, now=now)
                continue
            if page.state != ChatPageState.IDLE or page.close_after_at is None or page.close_after_at > now:
                continue
            result = self.browser.close_page(page)
            if result.closed or result.absent:
                self.store.mark_page_closed(page.page_id)
            else:
                # The idle dispatcher must never become a resident leak. Once the exact-close
                # deadline is reached, any result that does not positively prove closed/absent
                # is terminally ambiguous. Preserve the exact page identity/error for later
                # reconciliation, but stop automatic retries and let the short-lived worker exit.
                self.store.mark_page_closed(
                    page.page_id,
                    ambiguous=True,
                    error=result.detail or "exact owned-page close could not be proven",
                )

    def run_once(self) -> dict:
        progressed = False
        for job in self.store.eligible_jobs(limit=200):
            if job.state in TERMINAL_JOB_STATES:
                continue
            if self.store.has_older_unfinished(job):
                continue
            if job.state == ChatDispatchState.SUBMITTING:
                # SUBMITTING is the write-ahead record immediately before the external Send.
                # A restarted worker cannot know whether Send happened, so it must never replay.
                self.store.update_job(
                    job.dispatch_id,
                    state=ChatDispatchState.RECONCILE_REQUIRED,
                    last_error="worker resumed from SUBMITTING; Send outcome requires reconciliation",
                )
                self._reconcile(self.store.get_job(job.dispatch_id))
                progressed = True
                continue
            if job.state in {ChatDispatchState.SUBMITTED, ChatDispatchState.RECONCILE_REQUIRED}:
                before = job.state
                self._reconcile(job)
                if self.store.get_job(job.dispatch_id).state != before:
                    progressed = True
                continue
            if job.state == ChatDispatchState.RETRY_WAIT and job.next_attempt_at > time.time():
                continue
            try:
                page = self._ensure_page(job)
            except BaseException as exc:
                self.store.update_job(
                    job.dispatch_id,
                    state=ChatDispatchState.FAILED,
                    last_error=f"page acquisition failed: {type(exc).__name__}: {exc}",
                )
                progressed = True
                continue
            if page is None:
                continue
            self._submit(self.store.get_job(job.dispatch_id), page)
            progressed = True
        self._close_idle_pages()
        return {
            "progressed": progressed,
            "pending": self.store.pending_count(),
            "owned_open": self.store.owned_open_count(),
        }

    def should_exit(self) -> bool:
        jobs = self.store.list_jobs(limit=500)
        if self.store.pending_count() > 0:
            # RECONCILE_REQUIRED is deliberately not replayed. Keep observing for a bounded
            # window in case nonce/URL evidence arrives, then let the short-lived worker exit
            # even if an ambiguous owned page must remain open for later human/model reconcile.
            now = time.time()
            recent_unresolved = any(
                job.state == ChatDispatchState.RECONCILE_REQUIRED
                and now - job.updated_at < DEFAULT_RECONCILE_HOLD_S
                for job in jobs
            )
            other_pending = any(
                job.state in PENDING_JOB_STATES
                and job.state != ChatDispatchState.RECONCILE_REQUIRED
                for job in jobs
            )
            if recent_unresolved or other_pending:
                return False
            return True
        # A cleanly drained queue keeps only dispatcher-owned pages alive through their idle
        # close grace. Borrowed user pages never keep the worker resident.
        return self.store.owned_open_count() == 0


def run_chat_dispatch_worker(
    *,
    db_path: str | Path | None = None,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    idle_close_s: float = DEFAULT_IDLE_CLOSE_S,
    poll_s: float = 1.0,
) -> int:
    owner = f"chat-worker:{uuid.uuid4().hex}"
    with ChatDispatchStore(db_path) as store:
        if not store.acquire_lease(
            owner_id=owner,
            pid=os.getpid(),
            max_windows=max_windows,
            idle_close_s=idle_close_s,
        ):
            return 0
        browser = ChromeChatDispatchBrowser()
        engine = ChatDispatchEngine(
            store,
            browser,
            max_windows=max_windows,
            idle_close_s=idle_close_s,
        )
        try:
            while True:
                if not store.heartbeat_lease(owner):
                    return 3
                engine.run_once()
                if engine.should_exit():
                    return 0
                time.sleep(max(0.2, min(float(poll_s), 10.0)))
        finally:
            store.release_lease(owner)


def _pythonw_executable() -> str:
    current = Path(sys.executable)
    if os.name == "nt":
        candidate = current.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return str(current)


def ensure_chat_dispatch_worker(
    *,
    db_path: str | Path | None = None,
    max_windows: int = DEFAULT_MAX_WINDOWS,
    idle_close_s: float = DEFAULT_IDLE_CLOSE_S,
    repo_root: str | Path | None = None,
) -> dict:
    path = Path(db_path or default_chat_dispatch_db_path()).resolve()
    with ChatDispatchStore(path) as store:
        lease = store.lease()
        now = time.time()
        if lease:
            lease_pid = int(lease.get("pid") or 0)
            owner_id = str(lease.get("owner_id") or "")
            alive = lease_pid > 0 and pid_exists(lease_pid)
            if alive:
                return {
                    "started": False,
                    "pid": lease_pid,
                    "owner_id": owner_id,
                    "detail": (
                        "existing chat dispatcher lease is fresh"
                        if float(lease.get("expires_at") or 0.0) > now
                        else "dispatcher PID is still alive behind an expired lease; replacement is fenced"
                    ),
                }
            # A dead PID cannot perform a later browser side effect. Clear only its exact owner
            # token before replacement, regardless of whether the lease TTL has elapsed yet.
            store.release_lease(owner_id)
    command = [
        _pythonw_executable(),
        "-m",
        "lws",
        "chat-dispatch-worker",
        "--queue-db",
        str(path),
        "--max-windows",
        str(max(1, min(int(max_windows), 16))),
        "--idle-close",
        str(max(1.0, float(idle_close_s))),
    ]
    env = os.environ.copy()
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if repo_root is not None:
        root = Path(repo_root).resolve()
        kwargs["cwd"] = str(root)
        src = str(root / "src")
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **kwargs)
    deadline = time.time() + 5.0
    lease = None
    while time.time() < deadline:
        time.sleep(0.05)
        with ChatDispatchStore(path) as store:
            lease = store.lease()
        if lease and int(lease.get("pid") or 0) > 0:
            break
        if proc.poll() is not None:
            break
    return {
        "started": True,
        "spawn_pid": proc.pid,
        "pid": int(lease["pid"]) if lease else None,
        "owner_id": str(lease["owner_id"]) if lease else None,
        "detail": "chat dispatcher worker started" if lease else "chat dispatcher launch returned before lease was observed",
    }


def chat_dispatch_status(
    *,
    db_path: str | Path | None = None,
    dispatch_id: str | None = None,
    limit: int = 50,
) -> dict:
    with ChatDispatchStore(db_path) as store:
        jobs = [store.get_job(dispatch_id)] if dispatch_id else store.list_jobs(limit=limit)
        lease = store.lease()
        return {
            "db_path": str(store.path),
            "jobs": [job_payload(job) for job in jobs],
            "pages": [page_payload(page) for page in store.list_pages()],
            "lease": lease,
            "pending": store.pending_count(),
        }
