from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from dataclasses import asdict
from typing import Any

from .browser import observation_from_dom_payload
from .models import BrowserObservation, WorkerRecord


class UiaProbeUnavailable(RuntimeError):
    """Raised when the Windows UI Automation probe cannot inspect a matching Chrome tab."""


_POWERSHELL_PROBE = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-Value([System.Windows.Automation.AutomationElement]$e) {
    try {
        $p = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if ($p) { return $p.Current.Value }
    } catch {}
    return $null
}

function Normalized-Url([string]$value) {
    if (-not $value) { return '' }
    $v = $value.Trim()
    $v = $v -replace '^https?://', ''
    return $v.TrimEnd('/')
}

$expected = Normalized-Url $env:LWS_EXPECTED_URL
$expectedExe = [string]$env:LWS_EXPECTED_CHROME_EXE
$expectedHwnd = [int64]0
if ($env:LWS_EXPECTED_HWND) {
    [void][int64]::TryParse($env:LWS_EXPECTED_HWND, [ref]$expectedHwnd)
}

$desktop = [System.Windows.Automation.AutomationElement]::RootElement
$topWindows = $desktop.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
)
$addressCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
    'view_1012'
)
$matches = @()

for ($w = 0; $w -lt $topWindows.Count; $w++) {
    $root = $topWindows.Item($w)
    if ([string]$root.Current.ClassName -ne 'Chrome_WidgetWin_1') { continue }
    $chromePid = [int]$root.Current.ProcessId
    if (-not $chromePid) { continue }
    try { $proc = Get-Process -Id $chromePid -ErrorAction Stop } catch { continue }
    if ($proc.ProcessName -ne 'chrome') { continue }
    $procPath = [string]$proc.Path
    if ($expectedExe -and (
        -not $procPath -or -not $procPath.Equals(
            $expectedExe,
            [StringComparison]::OrdinalIgnoreCase
        )
    )) { continue }
    $hwnd = [int64]$root.Current.NativeWindowHandle
    if ($expectedHwnd -and $hwnd -ne $expectedHwnd) { continue }

    $addressBar = $root.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $addressCond
    )
    if (-not $addressBar) { continue }
    $address = Get-Value $addressBar
    if (-not $address) { continue }
    $normalizedAddress = Normalized-Url $address
    if ($expected -and $normalizedAddress -ne $expected) { continue }
    $matches += [pscustomobject]@{
        root = $root
        proc = $proc
        address = [string]$address
        hwnd = $hwnd
    }
}

$result = $null
if ($matches.Count -gt 1) {
    $result = [pscustomobject]@{
        observed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        error = 'multiple Chrome windows matched the requested URL; bind an exact HWND'
        match_count = [int]$matches.Count
    }
} elseif ($matches.Count -eq 1) {
    $match = $matches[0]
    $root = $match.root
    $proc = $match.proc
    $address = $match.address

    $docCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Document
    )
    $doc = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $docCond)
    if ($doc) {
        $all = $doc.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        )

        $texts = @()
        $visibleTexts = @()
        $buttons = @()
        $composerPresent = $false
        for ($i = 0; $i -lt $all.Count; $i++) {
            $e = $all.Item($i)
            $name = [string]$e.Current.Name
            $type = $e.Current.ControlType.ProgrammaticName
            if ([string]$e.Current.AutomationId -eq 'prompt-textarea') { $composerPresent = $true }
            if ($name) { $name = ($name -replace "`r|`n", ' ').Trim() }
            if ($type -eq 'ControlType.Text' -and $name) {
                $texts += $name
                if (-not [bool]$e.Current.IsOffscreen) { $visibleTexts += $name }
            }
            if ($type -eq 'ControlType.Button' -and $name) {
                $buttons += [pscustomobject]@{
                    text = $name
                    automation_id = [string]$e.Current.AutomationId
                    enabled = [bool]$e.Current.IsEnabled
                    offscreen = [bool]$e.Current.IsOffscreen
                }
            }
        }

        $tailParts = @()
        $tailChars = 0
        for ($i = $texts.Count - 1; $i -ge 0 -and $tailChars -lt 30000; $i--) {
            $part = $texts[$i]
            $tailParts = @($part) + $tailParts
            $tailChars += $part.Length + 1
        }
        $textTail = ($tailParts -join "`n")
        if ($textTail.Length -gt 30000) { $textTail = $textTail.Substring($textTail.Length - 30000) }

        $visibleTailParts = @()
        $visibleTailChars = 0
        for ($i = $visibleTexts.Count - 1; $i -ge 0 -and $visibleTailChars -lt 8000; $i--) {
            $part = $visibleTexts[$i]
            $visibleTailParts = @($part) + $visibleTailParts
            $visibleTailChars += $part.Length + 1
        }
        $visibleTextTail = ($visibleTailParts -join "`n")
        if ($visibleTextTail.Length -gt 8000) { $visibleTextTail = $visibleTextTail.Substring($visibleTextTail.Length - 8000) }

        $result = [pscustomobject]@{
            observed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
            browser_pid = [int]$proc.Id
            window_handle = [int64]$match.hwnd
            address = [string]$address
            text_tail = [string]$textTail
            visible_text_tail = [string]$visibleTextTail
            text_elements = [int]$texts.Count
            visible_text_elements = [int]$visibleTexts.Count
            element_count = [int]$all.Count
            buttons = @($buttons)
            composer_present = [bool]$composerPresent
            process_working_set_bytes = [int64]$proc.WorkingSet64
        }
    }
}

if (-not $result) {
    $result = [pscustomobject]@{
        observed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        error = if ($expected) { "no Chrome window matched $expected" } else { 'no inspectable Chrome window found' }
    }
}

$json = $result | ConvertTo-Json -Compress -Depth 8
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''

_POWERSHELL_DISCOVER = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-Value([System.Windows.Automation.AutomationElement]$e) {
    try {
        $p = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if ($p) { return $p.Current.Value }
    } catch {}
    return $null
}

$expectedExe = [string]$env:LWS_EXPECTED_CHROME_EXE
$desktop = [System.Windows.Automation.AutomationElement]::RootElement
$topWindows = $desktop.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
)
$addressCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
    'view_1012'
)
$rows = @()
for ($w = 0; $w -lt $topWindows.Count; $w++) {
    $root = $topWindows.Item($w)
    if ([string]$root.Current.ClassName -ne 'Chrome_WidgetWin_1') { continue }
    $chromePid = [int]$root.Current.ProcessId
    if (-not $chromePid) { continue }
    try { $proc = Get-Process -Id $chromePid -ErrorAction Stop } catch { continue }
    if ($proc.ProcessName -ne 'chrome') { continue }
    $procPath = [string]$proc.Path
    if ($expectedExe -and (
        -not $procPath -or -not $procPath.Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase)
    )) { continue }
    $addressBar = $root.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $addressCond
    )
    if (-not $addressBar) { continue }
    $address = Get-Value $addressBar
    if (-not $address) { continue }
    $rows += [pscustomobject]@{
        observed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        browser_pid = [int]$proc.Id
        window_handle = [int64]$root.Current.NativeWindowHandle
        address = [string]$address
    }
}
$json = @($rows) | ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''

_STOP_MARKERS = ("stop answering", "stop generating", "stop response", "停止生成", "停止回答")
_SEND_MARKERS = ("send", "send prompt", "send message", "发送", "发送消息")
_ERROR_MARKERS = (
    "message delivery timed out",
    "error in message stream",
    "there was an error generating a response",
    "something went wrong",
)


def normalize_url(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE)
    return text.rstrip("/")


def conversation_id_from_url(value: str) -> str | None:
    match = re.search(r"/c/([0-9a-fA-F-]{16,})", value)
    return match.group(1) if match else None


def payload_from_uia_result(result: dict[str, Any]) -> dict[str, Any]:
    error = result.get("error")
    if error:
        raise UiaProbeUnavailable(str(error))
    buttons = [row for row in result.get("buttons", []) if isinstance(row, dict)]
    labels = [str(row.get("text") or "").strip().lower() for row in buttons]
    stop_visible = any(any(marker in label for marker in _STOP_MARKERS) for label in labels)
    send_ready: bool | None = None
    for row, label in zip(buttons, labels, strict=False):
        if any(label == marker or label.startswith(marker + " ") for marker in _SEND_MARKERS):
            send_ready = bool(row.get("enabled", True)) and not bool(row.get("offscreen", False))
            break
    composer_present = bool(result.get("composer_present"))
    generating: bool | None = (
        True if stop_visible else (False if (send_ready is True or composer_present) else None)
    )
    text_tail = str(result.get("text_tail") or "")
    visible_text_tail = str(result.get("visible_text_tail") or "")
    lower_tail = visible_text_tail.lower()
    visible_error = None
    for marker in _ERROR_MARKERS:
        index = lower_tail.rfind(marker)
        if index >= 0:
            visible_error = marker[:1].upper() + marker[1:]
            break
    address = str(result.get("address") or "").strip()
    url = address if re.match(r"^https?://", address, flags=re.IGNORECASE) else f"https://{address}"
    return {
        "observed_at": float(result.get("observed_at") or time.time()),
        "url": url,
        "text_tail": text_tail,
        "generating": generating,
        "send_button_ready": send_ready,
        "pending_tool_calls": None,
        "visible_error": visible_error,
        "buttons": [
            {
                "text": row.get("text"),
                "disabled": not bool(row.get("enabled", True)),
                "offscreen": bool(row.get("offscreen", False)),
                "automation_id": row.get("automation_id"),
            }
            for row in buttons[-200:]
        ],
        "raw": {
            "source": "windows_uia_chrome",
            "browser_pid": result.get("browser_pid"),
            "window_handle": result.get("window_handle"),
            "composer_present": result.get("composer_present"),
            "text_elements": result.get("text_elements"),
            "visible_text_elements": result.get("visible_text_elements"),
            "element_count": result.get("element_count"),
            "process_working_set_bytes": result.get("process_working_set_bytes"),
        },
    }


def _default_chrome_executable() -> str | None:
    candidates = [
        os.path.join(
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
    ]
    return next((path for path in candidates if os.path.isfile(path)), None)


class ChromeUiaProbe:
    """Read-only observer for an already-authenticated, normal Chrome window on Windows.

    The probe uses the OS accessibility/UI Automation tree. It never clicks, types, reads
    browser cookie databases, or requires Chrome to expose a CDP port. This makes it a
    useful observer for the user's existing web chat session when transport-level
    attachment is unavailable.
    """

    def __init__(
        self,
        *,
        powershell: str = "powershell.exe",
        timeout_s: float = 8.0,
        chrome_executable: str | None = None,
    ) -> None:
        self.powershell = powershell
        self.timeout_s = max(1.0, float(timeout_s))
        self.chrome_executable = chrome_executable or _default_chrome_executable()

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def raw_probe(
        self,
        conversation_url: str,
        *,
        expected_hwnd: int | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            raise UiaProbeUnavailable("Windows UI Automation is only available on Windows")
        env = os.environ.copy()
        env["LWS_EXPECTED_URL"] = conversation_url
        if self.chrome_executable:
            env["LWS_EXPECTED_CHROME_EXE"] = self.chrome_executable
        if expected_hwnd is not None:
            env["LWS_EXPECTED_HWND"] = str(int(expected_hwnd))
        completed = subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _POWERSHELL_PROBE,
            ],
            text=False,
            capture_output=True,
            timeout=self.timeout_s,
            env=env,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise UiaProbeUnavailable(
                f"PowerShell UIA probe exited {completed.returncode}: {detail[-1000:]}"
            )
        encoded = completed.stdout.decode("ascii", errors="ignore").strip().splitlines()
        if not encoded:
            raise UiaProbeUnavailable("PowerShell UIA probe produced no output")
        try:
            data = base64.b64decode(encoded[-1]).decode("utf-8")
            result = json.loads(data)
        except (ValueError, json.JSONDecodeError) as exc:
            raise UiaProbeUnavailable(f"invalid UIA probe output: {exc}") from exc
        if not isinstance(result, dict):
            raise UiaProbeUnavailable("UIA probe output is not an object")
        return result

    def discover_conversations(self) -> list[dict[str, Any]]:
        """Return bounded identities for visible top-level ChatGPT conversation windows."""
        if not self.available:
            raise UiaProbeUnavailable("Windows UI Automation is only available on Windows")
        env = os.environ.copy()
        if self.chrome_executable:
            env["LWS_EXPECTED_CHROME_EXE"] = self.chrome_executable
        completed = subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _POWERSHELL_DISCOVER],
            text=False,
            capture_output=True,
            timeout=self.timeout_s,
            env=env,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise UiaProbeUnavailable(f"PowerShell UIA discovery exited {completed.returncode}: {detail[-1000:]}")
        encoded = completed.stdout.decode("ascii", errors="ignore").strip().splitlines()
        if not encoded:
            raise UiaProbeUnavailable("PowerShell UIA discovery produced no output")
        try:
            data = json.loads(base64.b64decode(encoded[-1]).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise UiaProbeUnavailable(f"invalid UIA discovery output: {exc}") from exc
        if isinstance(data, dict):
            data = [data]
        rows: list[dict[str, Any]] = []
        for row in data or []:
            if not isinstance(row, dict):
                continue
            address = str(row.get("address") or "").strip()
            url = address if re.match(r"^https?://", address, re.I) else f"https://{address}"
            if conversation_id_from_url(url) is None or not re.match(r"^https://chatgpt\.com/", url, re.I):
                continue
            rows.append({
                "observed_at": float(row.get("observed_at") or time.time()),
                "browser_pid": int(row.get("browser_pid") or 0),
                "window_handle": int(row.get("window_handle") or 0),
                "url": url,
                "conversation_id": conversation_id_from_url(url),
            })
        return rows

    def observe(
        self,
        worker: WorkerRecord,
        *,
        previous: BrowserObservation | None = None,
        expected_hwnd: int | None = None,
    ) -> BrowserObservation:
        result = self.raw_probe(worker.conversation_url, expected_hwnd=expected_hwnd)
        payload = payload_from_uia_result(result)
        observed_url = normalize_url(str(payload.get("url") or ""))
        expected_url = normalize_url(worker.conversation_url)
        if observed_url != expected_url:
            raise UiaProbeUnavailable(
                f"UIA address mismatch: expected {expected_url!r}, observed {observed_url!r}"
            )
        return observation_from_dom_payload(worker.worker_id, payload, previous=previous)

    def inspect(
        self,
        worker: WorkerRecord,
        *,
        expected_hwnd: int | None = None,
    ) -> dict[str, Any]:
        """Return state/signature diagnostics without returning conversation or draft text."""
        previous = None
        result = self.raw_probe(worker.conversation_url, expected_hwnd=expected_hwnd)
        payload = payload_from_uia_result(result)
        obs = observation_from_dom_payload(worker.worker_id, payload, previous=previous)
        return {
            "observed_at": obs.observed_at,
            "url": obs.url,
            "conversation_id": conversation_id_from_url(str(obs.url or "")),
            "generating": obs.generating,
            "send_button_ready": obs.send_button_ready,
            "pending_tool_calls": obs.pending_tool_calls,
            "visible_error": obs.visible_error,
            "message_signature": obs.message_signature,
            "raw": dict(obs.raw),
        }


def observation_dict(obs: BrowserObservation) -> dict[str, Any]:
    return asdict(obs)
