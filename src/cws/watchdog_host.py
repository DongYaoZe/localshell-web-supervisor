from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .registry import Registry


@dataclass(slots=True)
class WatchdogHostStatus:
    name: str
    lease_present: bool
    stop_requested: bool
    pid: int | None
    pid_alive: bool | None
    host: str | None
    heartbeat_at: float | None
    expires_at: float | None
    fresh: bool
    detail: str


@dataclass(slots=True)
class WatchdogLaunchResult:
    spawn_pid: int
    lease_pid: int | None
    lease_owner: str
    command: list[str]
    log_path: str
    lease_ready: bool
    detail: str


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def inspect_watchdog_host(
    registry: Registry,
    *,
    name: str = "default",
    now: float | None = None,
) -> WatchdogHostStatus:
    now = time.time() if now is None else float(now)
    lease = registry.watchdog_lease(name)
    if lease is None:
        return WatchdogHostStatus(
            name=name,
            lease_present=False,
            stop_requested=False,
            pid=None,
            pid_alive=None,
            host=None,
            heartbeat_at=None,
            expires_at=None,
            fresh=False,
            detail="no watchdog lease",
        )
    pid = int(lease.get("pid") or 0)
    owner = str(lease.get("owner_id") or "")
    expires_at = float(lease.get("expires_at") or 0)
    fresh = expires_at > now
    alive = pid_exists(pid) if pid else False
    stop_requested = owner.startswith("stop:") and fresh
    if stop_requested:
        detail = "cooperative stop requested; replacement watchdog remains fenced"
    elif fresh and alive:
        detail = "watchdog lease and PID are live"
    elif fresh and not alive:
        detail = "lease is fresh but PID is absent; wait for lease expiry before replacement"
    else:
        detail = "watchdog lease is stale"
    return WatchdogHostStatus(
        name=name,
        lease_present=True,
        stop_requested=stop_requested,
        pid=pid or None,
        pid_alive=alive,
        host=lease.get("host"),
        heartbeat_at=lease.get("heartbeat_at"),
        expires_at=expires_at,
        fresh=fresh,
        detail=detail,
    )


def build_watchdog_command(
    *,
    python_executable: str,
    db_path: str | Path,
    interval_s: float,
    use_uia: bool,
    auto_recover_timeouts: bool = False,
    lsm_state_dir: str | None = None,
    git_bin: str | None = None,
    lease_owner: str | None = None,
) -> list[str]:
    command = [python_executable, "-m", "cws", "--db", str(db_path)]
    if lsm_state_dir:
        command += ["--lsm-state-dir", str(lsm_state_dir)]
    if git_bin:
        command += ["--git-bin", str(git_bin)]
    command += ["watch", "--interval", str(max(1.0, float(interval_s)))]
    if lease_owner:
        command += ["--lease-owner", lease_owner]
    if use_uia or auto_recover_timeouts:
        command.append("--uia")
    if auto_recover_timeouts:
        command.append("--auto-recover-timeouts")
    return command


def launch_detached_watchdog(
    registry: Registry,
    *,
    repo_root: str | Path,
    db_path: str | Path,
    interval_s: float = 30.0,
    use_uia: bool = False,
    auto_recover_timeouts: bool = False,
    lsm_state_dir: str | None = None,
    git_bin: str | None = None,
    log_path: str | Path | None = None,
    ready_timeout_s: float = 8.0,
) -> WatchdogLaunchResult:
    status = inspect_watchdog_host(registry)
    if status.fresh:
        raise RuntimeError(f"watchdog start refused: {status.detail}")

    repo_root = Path(repo_root).resolve()
    db_path = Path(db_path).resolve()
    log_path = Path(log_path or (repo_root / ".cws" / "watchdog.log")).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lease_owner = f"host:{uuid.uuid4().hex}"
    command = build_watchdog_command(
        python_executable=sys.executable,
        db_path=db_path,
        interval_s=interval_s,
        use_uia=use_uia,
        auto_recover_timeouts=auto_recover_timeouts,
        lsm_state_dir=lsm_state_dir,
        git_bin=git_bin,
        lease_owner=lease_owner,
    )
    env = os.environ.copy()
    src = str(repo_root / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        start_new_session = True

    with log_path.open("ab", buffering=0) as log:
        proc = subprocess.Popen(
            command,
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=creationflags,
            start_new_session=start_new_session,
            close_fds=True,
        )

    deadline = time.time() + max(1.0, float(ready_timeout_s))
    lease_ready = False
    lease_pid = None
    detail = "detached watchdog started; owned lease not observed before timeout"
    while time.time() < deadline:
        time.sleep(0.1)
        current = registry.watchdog_lease("default")
        if current and str(current.get("owner_id") or "") == lease_owner:
            lease_ready = True
            lease_pid = int(current.get("pid") or 0) or None
            detail = "detached watchdog acquired the singleton lease with the launch owner token"
            break
        if proc.poll() is not None:
            detail = f"watchdog launcher exited during startup with code {proc.returncode}"
            break

    return WatchdogLaunchResult(
        spawn_pid=proc.pid,
        lease_pid=lease_pid,
        lease_owner=lease_owner,
        command=command,
        log_path=str(log_path),
        lease_ready=lease_ready,
        detail=detail,
    )


def request_cooperative_stop(
    registry: Registry,
    *,
    name: str = "default",
    grace_s: float = 90.0,
) -> dict | None:
    return registry.request_watchdog_stop(name=name, grace_s=grace_s)
