from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SystemMemoryObservation:
    observed_at: float
    total_bytes: int
    available_bytes: int
    used_bytes: int
    used_fraction: float


@dataclass(slots=True)
class BrowserMemoryObservation:
    observed_at: float
    process_name: str
    process_count: int
    total_working_set_bytes: int
    largest_working_set_bytes: int
    window_process_count: int
    pids: list[int] = field(default_factory=list)
    error: str | None = None


class MemoryProbeUnavailable(RuntimeError):
    pass


def observe_system_memory() -> SystemMemoryObservation:
    """Read physical-memory pressure using only OS-local APIs."""
    now = time.time()
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        state = MEMORYSTATUSEX()
        state.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            raise MemoryProbeUnavailable("GlobalMemoryStatusEx failed")
        total = int(state.ullTotalPhys)
        available = int(state.ullAvailPhys)
    elif os.path.isfile("/proc/meminfo"):
        values: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="ascii", errors="replace") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if not rest:
                    continue
                number = rest.strip().split()[0]
                if number.isdigit():
                    values[key] = int(number) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        if total <= 0:
            raise MemoryProbeUnavailable("/proc/meminfo did not expose MemTotal")
    else:
        raise MemoryProbeUnavailable("system memory probe is unsupported on this platform")

    used = max(0, total - available)
    return SystemMemoryObservation(
        observed_at=now,
        total_bytes=total,
        available_bytes=available,
        used_bytes=used,
        used_fraction=(used / total) if total else 0.0,
    )


def observe_windows_process_group(
    process_name: str = "chrome",
    *,
    powershell: str = "powershell.exe",
    timeout_s: float = 5.0,
) -> BrowserMemoryObservation:
    """Aggregate one Windows process-name group without reading command lines or profiles.

    CWS intentionally records only PIDs and working-set totals here. It does not inspect
    browser command lines, profile paths, environment variables, cookies, or page contents.
    """
    if os.name != "nt":
        raise MemoryProbeUnavailable("Windows process-group probe is only available on Windows")
    safe_name = "".join(ch for ch in process_name if ch.isalnum() or ch in "-_./")
    if not safe_name:
        raise ValueError("process_name is empty after validation")
    script = rf"""
$rows = @(Get-Process -Name '{safe_name}' -ErrorAction SilentlyContinue | ForEach-Object {{
  [pscustomobject]@{{
    pid = [int]$_.Id
    working_set = [int64]$_.WorkingSet64
    has_window = [bool]($_.MainWindowHandle -ne 0)
  }}
}})
@($rows) | ConvertTo-Json -Compress
"""
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
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(1.0, float(timeout_s)),
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    now = time.time()
    if completed.returncode != 0:
        return BrowserMemoryObservation(
            observed_at=now,
            process_name=safe_name,
            process_count=0,
            total_working_set_bytes=0,
            largest_working_set_bytes=0,
            window_process_count=0,
            error=f"PowerShell exited {completed.returncode}",
        )
    text = completed.stdout.strip()
    try:
        payload: Any = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise MemoryProbeUnavailable(f"invalid process-memory JSON: {exc}") from exc
    rows = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
    cleaned = [row for row in rows if isinstance(row, dict)]
    working_sets = [max(0, int(row.get("working_set") or 0)) for row in cleaned]
    pids = [int(row.get("pid")) for row in cleaned if row.get("pid") is not None]
    return BrowserMemoryObservation(
        observed_at=now,
        process_name=safe_name,
        process_count=len(cleaned),
        total_working_set_bytes=sum(working_sets),
        largest_working_set_bytes=max(working_sets, default=0),
        window_process_count=sum(bool(row.get("has_window")) for row in cleaned),
        pids=sorted(pids),
    )
