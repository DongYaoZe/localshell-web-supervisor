from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

from .models import WorkspaceObservation


def detect_git_bin(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    env = os.getenv("LWS_GIT_BIN")
    if env:
        return env
    found = shutil.which("git")
    if found:
        return found
    if os.name == "nt":
        roots = [
            os.getenv("PROGRAMFILES"),
            os.getenv("PROGRAMFILES(X86)"),
            os.getenv("LOCALAPPDATA"),
        ]
        for root in filter(None, roots):
            base = Path(root)
            candidates = (
                base / "Git" / "cmd" / "git.exe",
                base / "Git" / "bin" / "git.exe",
                base / "Programs" / "Git" / "cmd" / "git.exe",
            )
            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate)
    return None


class WorkspaceProbe:
    """Read-only workspace/git reconciliation probe."""

    def __init__(
        self,
        *,
        git_bin: str | None = None,
        timeout_s: float = 5.0,
        max_status_entries: int = 100,
    ) -> None:
        self.git_bin = detect_git_bin(git_bin)
        self.timeout_s = max(0.5, float(timeout_s))
        self.max_status_entries = max(1, int(max_status_entries))

    def _run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if not self.git_bin:
            raise FileNotFoundError("git executable not found")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.run(
            [self.git_bin, "-C", str(cwd), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=self.timeout_s,
            creationflags=creationflags,
            check=False,
        )

    def observe(self, *, task_id: str, cwd: str) -> WorkspaceObservation:
        now = time.time()
        path = Path(cwd)
        if not path.exists():
            return WorkspaceObservation(
                task_id=task_id,
                observed_at=now,
                cwd=str(path),
                cwd_exists=False,
                error="working directory does not exist",
            )
        if not self.git_bin:
            return WorkspaceObservation(
                task_id=task_id,
                observed_at=now,
                cwd=str(path),
                cwd_exists=True,
                is_git_repo=None,
                error="git executable not found",
            )
        try:
            identity = self._run_git(path, "rev-parse", "--show-toplevel")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return WorkspaceObservation(
                task_id=task_id,
                observed_at=now,
                cwd=str(path),
                cwd_exists=True,
                error=f"git probe failed: {type(exc).__name__}: {exc}",
            )
        if identity.returncode != 0:
            stderr = identity.stderr.strip()
            not_repo = "not a git repository" in stderr.lower()
            return WorkspaceObservation(
                task_id=task_id,
                observed_at=now,
                cwd=str(path),
                cwd_exists=True,
                is_git_repo=False if not_repo else None,
                error=None if not_repo else (stderr or f"git rev-parse exited {identity.returncode}"),
            )
        lines = [line.strip() for line in identity.stdout.splitlines() if line.strip()]
        git_root = lines[0] if lines else str(path)
        root = Path(git_root)
        try:
            head = self._run_git(root, "rev-parse", "--verify", "HEAD")
            git_head = head.stdout.strip() if head.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            git_head = None
        try:
            branch = self._run_git(root, "branch", "--show-current")
            git_branch = branch.stdout.strip() if branch.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            git_branch = None
        try:
            status = self._run_git(root, "status", "--porcelain=v1", "--untracked-files=normal")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return WorkspaceObservation(
                task_id=task_id,
                observed_at=now,
                cwd=str(path),
                cwd_exists=True,
                is_git_repo=True,
                git_root=git_root,
                git_head=git_head,
                error=f"git status failed: {type(exc).__name__}: {exc}",
            )
        if status.returncode != 0:
            return WorkspaceObservation(
                task_id=task_id,
                observed_at=now,
                cwd=str(path),
                cwd_exists=True,
                is_git_repo=True,
                git_root=git_root,
                git_head=git_head,
                error=status.stderr.strip() or f"git status exited {status.returncode}",
            )
        raw_status = status.stdout
        entries = [line for line in raw_status.splitlines() if line]
        return WorkspaceObservation(
            task_id=task_id,
            observed_at=now,
            cwd=str(path),
            cwd_exists=True,
            is_git_repo=True,
            git_root=git_root,
            git_head=git_head,
            git_branch=git_branch,
            git_dirty=bool(entries),
            git_status_hash=hashlib.sha256(raw_status.encode("utf-8")).hexdigest(),
            git_status_entries=entries[: self.max_status_entries],
            raw={"git_status_truncated": len(entries) > self.max_status_entries},
        )
