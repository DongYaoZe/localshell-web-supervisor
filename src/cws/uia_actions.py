from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .actions import (
    ActionAcknowledgement,
    ActionAttempt,
    ActionIntent,
    ActionTransportDisabled,
    TransportSubmission,
    evidence_digest,
)
from .models import WorkerWindowBinding
from .uia import conversation_id_from_url, normalize_url


class UiaActionUnavailable(RuntimeError):
    """Raised when exact-window UI Automation evidence cannot be obtained safely."""


@dataclass(slots=True)
class UiaAckObservation:
    worker_id: str
    observed_at: float
    url: str
    window_handle: int
    browser_pid: int
    generating: bool | None
    send_button_ready: bool | None
    composer_present: bool
    signed_in_likely: bool
    nonce_occurrences: int
    text_element_count: int
    text_signature: str


_POWERSHELL_ACTION = r'''
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
    $v = $value.Trim() -replace '^https?://', ''
    return $v.TrimEnd('/')
}

function Find-ByAutomationId(
    [System.Windows.Automation.AutomationElement]$root,
    [string]$automationId
) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        $automationId
    )
    return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
}

function Emit([bool]$submitted, [bool]$sideEffectPossible, [string]$phase, [string]$detail) {
    $obj = [pscustomobject]@{
        submitted = $submitted
        side_effect_possible = $sideEffectPossible
        phase = $phase
        detail = $detail
    }
    $json = $obj | ConvertTo-Json -Compress -Depth 4
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}

$phase = 'preflight'
$sideEffectPossible = $false
try {
    $expected = Normalized-Url $env:CWS_EXPECTED_URL
    $expectedExe = [string]$env:CWS_EXPECTED_CHROME_EXE
    $expectedHwnd = [int64]0
    if (-not [int64]::TryParse($env:CWS_EXPECTED_HWND, [ref]$expectedHwnd) -or $expectedHwnd -le 0) {
        Emit $false $false $phase 'an exact positive HWND is required'
        exit 0
    }
    $expectedPid = [int]0
    if (-not [int]::TryParse($env:CWS_EXPECTED_BROWSER_PID, [ref]$expectedPid) -or $expectedPid -le 0) {
        Emit $false $false $phase 'an exact positive Chrome PID is required'
        exit 0
    }
    if (-not $expected -or -not $expectedExe) {
        Emit $false $false $phase 'exact URL and Chrome executable are required'
        exit 0
    }

    $window = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$expectedHwnd)
    if (-not $window -or [string]$window.Current.ClassName -ne 'Chrome_WidgetWin_1') {
        Emit $false $false $phase 'bound HWND is not a live Chrome top-level window'
        exit 0
    }
    $chromePid = [int]$window.Current.ProcessId
    if ($chromePid -ne $expectedPid) {
        Emit $false $false $phase 'bound HWND Chrome PID does not match the lease'
        exit 0
    }
    try { $proc = Get-Process -Id $chromePid -ErrorAction Stop } catch {
        Emit $false $false $phase 'bound Chrome process is not live'
        exit 0
    }
    if ($proc.ProcessName -ne 'chrome' -or -not ([string]$proc.Path).Equals(
        $expectedExe,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        Emit $false $false $phase 'bound window does not belong to the expected Google Chrome executable'
        exit 0
    }

    $addressBar = Find-ByAutomationId $window 'view_1012'
    $address = if ($addressBar) { Get-Value $addressBar } else { $null }
    if ((Normalized-Url $address) -ne $expected) {
        Emit $false $false $phase 'bound window URL does not exactly match the worker URL'
        exit 0
    }

    $docCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Document
    )
    $doc = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $docCond)
    if (-not $doc) {
        Emit $false $false $phase 'ChatGPT document is not present'
        exit 0
    }
    $buttonCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    )
    $buttons = $doc.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonCond)
    $loginControl = $false
    $profileControl = $false
    for ($i = 0; $i -lt $buttons.Count; $i++) {
        $name = [string]$buttons.Item($i).Current.Name
        if ($name -match '^(Log in|Sign in|Sign up|登录|免费注册)$') { $loginControl = $true }
        if ($name -match 'Profile|Account|个人资料') { $profileControl = $true }
    }
    if ($loginControl -or -not $profileControl) {
        Emit $false $false $phase 'positive signed-in profile evidence is missing'
        exit 0
    }

    $composer = Find-ByAutomationId $doc 'prompt-textarea'
    if (-not $composer) {
        Emit $false $false $phase 'prompt-textarea is not present'
        exit 0
    }
    $existingSend = Find-ByAutomationId $doc 'composer-submit-button'
    if ($existingSend -and [bool]$existingSend.Current.IsEnabled -and -not [bool]$existingSend.Current.IsOffscreen) {
        Emit $false $false $phase 'composer already has a send-ready draft; refusing to overwrite it'
        exit 0
    }

    $promptBytes = [Convert]::FromBase64String($env:CWS_PROMPT_B64)
    $prompt = [Text.Encoding]::UTF8.GetString($promptBytes)
    if (-not $prompt.Trim()) {
        Emit $false $false $phase 'prompt is empty'
        exit 0
    }
    $valuePattern = $composer.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    if (-not $valuePattern) {
        Emit $false $false $phase 'prompt-textarea lacks ValuePattern'
        exit 0
    }

    $valuePattern.SetValue($prompt)
    $phase = 'draft_set'
    $sideEffectPossible = $true

    # ChatGPT's application state can lag the accessibility ValuePattern update. Poll for a
    # bounded 2 seconds rather than assuming a fixed 300 ms is enough. Every iteration
    # revalidates the exact worker URL before trusting a Send control.
    $send = $null
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 50
        $addressBar = Find-ByAutomationId $window 'view_1012'
        $address = if ($addressBar) { Get-Value $addressBar } else { $null }
        if ((Normalized-Url $address) -ne $expected) {
            Emit $false $true $phase 'worker URL changed after draft input'
            exit 0
        }
        $candidate = Find-ByAutomationId $window 'composer-submit-button'
        if ($candidate -and [bool]$candidate.Current.IsEnabled -and -not [bool]$candidate.Current.IsOffscreen) {
            $send = $candidate
            break
        }
    }
    if (-not $send) {
        Emit $false $true $phase 'positive ready Send control did not appear within the bounded post-draft wait'
        exit 0
    }
    try {
        $invoke = $send.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    } catch {
        Emit $false $true $phase 'Send control lacks an invokable pattern'
        exit 0
    }
    if (-not $invoke) {
        Emit $false $true $phase 'Send control lacks an invokable pattern'
        exit 0
    }

    $phase = 'invoking_send'
    $invoke.Invoke()
    $phase = 'invoke_returned'
    Emit $true $true $phase 'exact-window UIA Send invocation returned'
} catch {
    $kind = $_.Exception.GetType().Name
    Emit $false $sideEffectPossible $phase ("UIA action failed in phase $phase ($kind)")
}
'''


_POWERSHELL_ACK = r'''
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
    return (($value.Trim() -replace '^https?://', '').TrimEnd('/'))
}
function Find-ByAutomationId(
    [System.Windows.Automation.AutomationElement]$root,
    [string]$automationId
) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        $automationId
    )
    return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
}
function Sha256([string]$value) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($value)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}
function Emit-Error([string]$detail) {
    $obj = [pscustomobject]@{ error = $detail }
    $json = $obj | ConvertTo-Json -Compress -Depth 4
    [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
}

$expected = Normalized-Url $env:CWS_EXPECTED_URL
$expectedExe = [string]$env:CWS_EXPECTED_CHROME_EXE
$expectedHwnd = [int64]0
if ($env:CWS_EXPECTED_HWND) { [void][int64]::TryParse($env:CWS_EXPECTED_HWND, [ref]$expectedHwnd) }
$expectedPid = [int]0
if ($env:CWS_EXPECTED_BROWSER_PID) { [void][int]::TryParse($env:CWS_EXPECTED_BROWSER_PID, [ref]$expectedPid) }
$nonce = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:CWS_ACK_NONCE_B64))

$desktop = [System.Windows.Automation.AutomationElement]::RootElement
$tops = $desktop.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
)
$matches = @()
for ($i = 0; $i -lt $tops.Count; $i++) {
    $window = $tops.Item($i)
    if ([string]$window.Current.ClassName -ne 'Chrome_WidgetWin_1') { continue }
    $chromePid = [int]$window.Current.ProcessId
    if (-not $chromePid) { continue }
    if ($expectedPid -and $chromePid -ne $expectedPid) { continue }
    try { $proc = Get-Process -Id $chromePid -ErrorAction Stop } catch { continue }
    if ($proc.ProcessName -ne 'chrome') { continue }
    if ($expectedExe -and -not ([string]$proc.Path).Equals($expectedExe, [StringComparison]::OrdinalIgnoreCase)) { continue }
    $hwnd = [int64]$window.Current.NativeWindowHandle
    if ($expectedHwnd -and $hwnd -ne $expectedHwnd) { continue }
    $bar = Find-ByAutomationId $window 'view_1012'
    $address = if ($bar) { Get-Value $bar } else { $null }
    if ((Normalized-Url $address) -ne $expected) { continue }
    $matches += [pscustomobject]@{ window=$window; proc=$proc; hwnd=$hwnd; address=[string]$address }
}
if ($matches.Count -ne 1) {
    Emit-Error ("expected exactly one matching Chrome window, observed $($matches.Count)")
    exit 0
}
$match = $matches[0]
$window = $match.window
$docCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Document
)
$doc = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $docCond)
if (-not $doc) { Emit-Error 'ChatGPT document is not present'; exit 0 }
$all = $doc.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)
$textBuilder = New-Object Text.StringBuilder
$textCount = 0
$nonceCount = 0
$stopVisible = $false
$sendReady = $null
$composerPresent = $false
$loginControl = $false
$profileControl = $false
for ($i = 0; $i -lt $all.Count; $i++) {
    $e = $all.Item($i)
    $name = [string]$e.Current.Name
    $type = [string]$e.Current.ControlType.ProgrammaticName
    if ($type -eq 'ControlType.Text' -and $name) {
        [void]$textBuilder.AppendLine($name)
        $textCount++
        if ($nonce) { $nonceCount += ([regex]::Matches($name, [regex]::Escape($nonce))).Count }
    }
    if ([string]$e.Current.AutomationId -eq 'prompt-textarea') { $composerPresent = $true }
    if ($type -eq 'ControlType.Button') {
        if ($name -match 'Stop answering|Stop generating|Stop response|停止生成|停止回答') { $stopVisible = $true }
        if ($name -match '^(Log in|Sign in|Sign up|登录|免费注册)$') { $loginControl = $true }
        if ($name -match 'Profile|Account|个人资料') { $profileControl = $true }
        if ([string]$e.Current.AutomationId -eq 'composer-submit-button') {
            $sendReady = [bool]$e.Current.IsEnabled -and -not [bool]$e.Current.IsOffscreen
        }
    }
}
$obj = [pscustomobject]@{
    observed_at = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    url = [string]$match.address
    window_handle = [int64]$match.hwnd
    browser_pid = [int]$match.proc.Id
    generating = if ($stopVisible) { $true } elseif ($composerPresent -or $sendReady -eq $true) { $false } else { $null }
    send_button_ready = $sendReady
    composer_present = [bool]$composerPresent
    signed_in_likely = (-not $loginControl) -and $profileControl
    nonce_occurrences = [int]$nonceCount
    text_element_count = [int]$textCount
    text_signature = Sha256 $textBuilder.ToString()
}
$json = $obj | ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


def _run_powershell_json(
    script: str,
    *,
    powershell: str,
    timeout_s: float,
    env: dict[str, str],
) -> dict[str, Any]:
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
        text=False,
        capture_output=True,
        timeout=max(1.0, float(timeout_s)),
        env=env,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise UiaActionUnavailable(f"PowerShell UIA helper exited {completed.returncode}")
    lines = completed.stdout.decode("ascii", errors="ignore").strip().splitlines()
    if not lines:
        raise UiaActionUnavailable("PowerShell UIA helper produced no output")
    try:
        decoded = base64.b64decode(lines[-1]).decode("utf-8")
        result = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise UiaActionUnavailable(f"invalid UIA helper output: {exc}") from exc
    if not isinstance(result, dict):
        raise UiaActionUnavailable("UIA helper output is not an object")
    return result


class ChromeUiaActionTransport:
    """Gated exact-window ChatGPT sender for an already-ARMED action.

    The transport remains disabled by default. CWS 0.6 exposes only an explicit one-shot
    executor that enables it after semantic, action, task-confirmation, and fresh-window fences.
    """

    name = "windows_uia_exact_window"

    def __init__(
        self,
        *,
        expected_worker_id: str,
        conversation_url: str,
        expected_hwnd: int,
        expected_browser_pid: int,
        chrome_executable: str,
        enabled: bool = False,
        binding_expires_at: float | None = None,
        powershell: str = "powershell.exe",
        timeout_s: float = 8.0,
    ) -> None:
        self.expected_worker_id = expected_worker_id
        self.conversation_url = conversation_url
        self.expected_hwnd = int(expected_hwnd)
        self.expected_browser_pid = int(expected_browser_pid)
        self.chrome_executable = chrome_executable
        self.enabled = bool(enabled)
        self.binding_expires_at = (
            None if binding_expires_at is None else float(binding_expires_at)
        )
        self.powershell = powershell
        self.timeout_s = max(1.0, float(timeout_s))

    @classmethod
    def from_binding(
        cls,
        binding: WorkerWindowBinding,
        *,
        expected_worker_id: str | None = None,
        conversation_url: str | None = None,
        enabled: bool = False,
        powershell: str = "powershell.exe",
        timeout_s: float = 8.0,
        now: float | None = None,
    ) -> "ChromeUiaActionTransport":
        worker_id = expected_worker_id or binding.worker_id
        url = conversation_url or binding.conversation_url
        current = time.time() if now is None else float(now)
        if binding.worker_id != worker_id:
            raise UiaActionUnavailable("window binding belongs to a different worker")
        if normalize_url(binding.conversation_url) != normalize_url(url):
            raise UiaActionUnavailable("window binding URL does not match the requested worker URL")
        if binding.expires_at <= current:
            raise UiaActionUnavailable("window binding is stale and must be refreshed")
        if binding.source != "windows_uia_chrome":
            raise UiaActionUnavailable("window binding source is not the normal-Chrome UIA probe")
        if binding.window_handle <= 0 or binding.browser_pid <= 0 or not binding.chrome_executable:
            raise UiaActionUnavailable("window binding is incomplete")
        return cls(
            expected_worker_id=worker_id,
            conversation_url=url,
            expected_hwnd=binding.window_handle,
            expected_browser_pid=binding.browser_pid,
            chrome_executable=binding.chrome_executable,
            enabled=enabled,
            binding_expires_at=binding.expires_at,
            powershell=powershell,
            timeout_s=timeout_s,
        )

    def submit(self, intent: ActionIntent) -> TransportSubmission:
        if not self.enabled:
            raise ActionTransportDisabled(
                "exact-window UIA mutation transport is gated; explicit per-invocation enablement is required"
            )
        if self.binding_expires_at is not None and time.time() >= self.binding_expires_at:
            return TransportSubmission(
                submitted=False,
                side_effect_possible=False,
                transport_name=self.name,
                detail="exact-window binding lease expired; refresh UIA observation before action",
            )
        if intent.worker_id != self.expected_worker_id:
            return TransportSubmission(
                submitted=False,
                side_effect_possible=False,
                transport_name=self.name,
                detail="action worker id does not match the exact-window transport binding",
            )
        if conversation_id_from_url(self.conversation_url) is None:
            return TransportSubmission(
                submitted=False,
                side_effect_possible=False,
                transport_name=self.name,
                detail="exact-window mutation requires an existing /c/ conversation URL",
            )
        if self.expected_hwnd <= 0 or self.expected_browser_pid <= 0 or not self.chrome_executable:
            return TransportSubmission(
                submitted=False,
                side_effect_possible=False,
                transport_name=self.name,
                detail="exact positive HWND/PID and Google Chrome executable are required",
            )
        if os.name != "nt":
            return TransportSubmission(
                submitted=False,
                side_effect_possible=False,
                transport_name=self.name,
                detail="Windows UI Automation is unavailable on this platform",
            )

        env = os.environ.copy()
        env["CWS_EXPECTED_URL"] = self.conversation_url
        env["CWS_EXPECTED_HWND"] = str(self.expected_hwnd)
        env["CWS_EXPECTED_BROWSER_PID"] = str(self.expected_browser_pid)
        env["CWS_EXPECTED_CHROME_EXE"] = self.chrome_executable
        env["CWS_PROMPT_B64"] = base64.b64encode(intent.prompt.encode("utf-8")).decode("ascii")
        result = _run_powershell_json(
            _POWERSHELL_ACTION,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )
        return TransportSubmission(
            submitted=bool(result.get("submitted")),
            side_effect_possible=bool(result.get("side_effect_possible")),
            transport_name=self.name,
            detail=str(result.get("detail") or result.get("phase") or "UIA action completed"),
        )


class ChromeUiaAckObserver:
    """Observe only exact-window state/counts/hashes needed for action acknowledgement."""

    def __init__(
        self,
        *,
        chrome_executable: str,
        powershell: str = "powershell.exe",
        timeout_s: float = 8.0,
    ) -> None:
        self.chrome_executable = chrome_executable
        self.powershell = powershell
        self.timeout_s = max(1.0, float(timeout_s))

    def observe(
        self,
        *,
        worker_id: str,
        conversation_url: str,
        expected_nonce: str,
        expected_hwnd: int | None = None,
        expected_browser_pid: int | None = None,
    ) -> UiaAckObservation:
        if os.name != "nt":
            raise UiaActionUnavailable("Windows UI Automation is unavailable on this platform")
        if conversation_id_from_url(conversation_url) is None:
            raise UiaActionUnavailable("ack observation requires an existing /c/ conversation URL")
        if not expected_nonce:
            raise UiaActionUnavailable("ack observation requires a non-empty known nonce")
        env = os.environ.copy()
        env["CWS_EXPECTED_URL"] = conversation_url
        env["CWS_EXPECTED_CHROME_EXE"] = self.chrome_executable
        env["CWS_ACK_NONCE_B64"] = base64.b64encode(expected_nonce.encode("utf-8")).decode("ascii")
        if expected_hwnd is not None:
            env["CWS_EXPECTED_HWND"] = str(int(expected_hwnd))
        if expected_browser_pid is not None:
            env["CWS_EXPECTED_BROWSER_PID"] = str(int(expected_browser_pid))
        result = _run_powershell_json(
            _POWERSHELL_ACK,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )
        if result.get("error"):
            raise UiaActionUnavailable(str(result["error"]))
        address = str(result.get("url") or "").strip()
        url = address if address.lower().startswith(("http://", "https://")) else f"https://{address}"
        if normalize_url(url) != normalize_url(conversation_url):
            raise UiaActionUnavailable("ack observer URL changed after exact-window selection")
        return UiaAckObservation(
            worker_id=worker_id,
            observed_at=float(result.get("observed_at") or time.time()),
            url=url,
            window_handle=int(result.get("window_handle") or 0),
            browser_pid=int(result.get("browser_pid") or 0),
            generating=result.get("generating") if isinstance(result.get("generating"), bool) else None,
            send_button_ready=(
                result.get("send_button_ready")
                if isinstance(result.get("send_button_ready"), bool)
                else None
            ),
            composer_present=bool(result.get("composer_present")),
            signed_in_likely=bool(result.get("signed_in_likely")),
            nonce_occurrences=int(result.get("nonce_occurrences") or 0),
            text_element_count=int(result.get("text_element_count") or 0),
            text_signature=str(result.get("text_signature") or ""),
        )


def acknowledgement_from_uia_observation(
    attempt: ActionAttempt,
    observation: UiaAckObservation,
    *,
    min_nonce_occurrences: int = 1,
    max_nonce_occurrences: int | None = None,
    require_generation_complete: bool = True,
) -> ActionAcknowledgement | None:
    """Build a positive ACK only from bounded UIA evidence; otherwise return None."""
    if observation.worker_id != attempt.worker_id:
        return None
    if not observation.signed_in_likely:
        return None
    if observation.nonce_occurrences < max(1, int(min_nonce_occurrences)):
        return None
    if max_nonce_occurrences is not None and observation.nonce_occurrences > max_nonce_occurrences:
        return None
    if require_generation_complete and observation.generating is not False:
        return None
    if not observation.text_signature:
        return None
    evidence = "|".join(
        [
            normalize_url(observation.url),
            f"hwnd={observation.window_handle}",
            f"nonce_count={observation.nonce_occurrences}",
            f"generating={observation.generating}",
            f"text_count={observation.text_element_count}",
            f"text_sha256={observation.text_signature}",
        ]
    )
    return ActionAcknowledgement(
        attempt_id=attempt.attempt_id,
        worker_id=attempt.worker_id,
        observed_at=observation.observed_at,
        accepted=True,
        kind="uia_nonce_hash",
        evidence_hash=evidence_digest(evidence),
        detail="positive exact-window nonce/hash UIA evidence",
    )
