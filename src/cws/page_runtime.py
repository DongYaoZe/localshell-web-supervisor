from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from .models import BrowserObservation, ProbeWindowSlotBinding, WorkerRecord, WorkerStatus
from .uia import ChromeUiaProbe, UiaProbeUnavailable, conversation_id_from_url


PROBE_SLOT_SOURCE = "windows_uia_cws_probe"
DEFAULT_PROBE_SLOT_ID = "probe:default"


class ProbeSlotAction(StrEnum):
    REUSE = "REUSE"
    OPEN = "OPEN"
    ROTATE = "ROTATE"
    BLOCKED = "BLOCKED"


@dataclass(slots=True)
class ProbeSlotPlan:
    slot_id: str
    worker_id: str
    target_conversation_url: str
    action: ProbeSlotAction
    mutation_required: bool
    reason: str
    checks: dict[str, bool]
    owner_token: str | None = None


@dataclass(slots=True)
class ProbeSlotExecution:
    changed: bool
    side_effect_possible: bool
    detail: str
    binding: ProbeWindowSlotBinding | None = None


class ProbeWindowTransportDisabled(RuntimeError):
    pass


class ProbeWindowTransport(Protocol):
    name: str

    def execute(
        self,
        plan: ProbeSlotPlan,
        *,
        existing: ProbeWindowSlotBinding | None,
    ) -> ProbeSlotExecution:
        ...


class DisabledProbeWindowTransport:
    name = "disabled"

    def execute(
        self,
        plan: ProbeSlotPlan,
        *,
        existing: ProbeWindowSlotBinding | None,
    ) -> ProbeSlotExecution:
        raise ProbeWindowTransportDisabled(
            f"probe-window transport is disabled for {plan.slot_id}; plan only"
        )


def _conversation_identity(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "chatgpt.com":
        return None
    conversation_id = conversation_id_from_url(url)
    if not conversation_id:
        return None
    return parsed.netloc.lower(), conversation_id.lower()


def tagged_probe_url(conversation_url: str, *, slot_id: str, owner_token: str) -> str:
    if _conversation_identity(conversation_url) is None:
        raise ValueError("probe slot requires an https://chatgpt.com/c/... conversation URL")
    if not slot_id.strip() or not owner_token.strip():
        raise ValueError("probe slot tag requires slot id and owner token")
    parsed = urlsplit(conversation_url.strip())
    fragment = f"cws-probe={slot_id}:{owner_token}"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment))


def slot_owns_actual_url(slot: ProbeWindowSlotBinding) -> bool:
    try:
        expected = tagged_probe_url(
            slot.target_conversation_url,
            slot_id=slot.slot_id,
            owner_token=slot.owner_token,
        )
    except ValueError:
        return False
    return slot.actual_url.rstrip("/") == expected.rstrip("/")


def plan_probe_slot(
    worker: WorkerRecord,
    existing: ProbeWindowSlotBinding | None,
    *,
    slot_id: str = DEFAULT_PROBE_SLOT_ID,
    now: float | None = None,
) -> ProbeSlotPlan:
    """Plan a single logical probe slot without opening or closing any browser window.

    A stale/ambiguous slot blocks. It never causes an implicit second probe window to open.
    A different fresh target rotates only through exact-close-before-open semantics.
    """
    now = time.time() if now is None else float(now)
    checks: dict[str, bool] = {
        "worker_is_parked": worker.status == WorkerStatus.PARKED,
        "target_is_chatgpt_conversation": _conversation_identity(worker.conversation_url) is not None,
    }
    if not all(checks.values()):
        return ProbeSlotPlan(
            slot_id=slot_id,
            worker_id=worker.worker_id,
            target_conversation_url=worker.conversation_url,
            action=ProbeSlotAction.BLOCKED,
            mutation_required=False,
            reason="probe slots are only for parked workers with an existing ChatGPT conversation URL",
            checks=checks,
        )

    if existing is None:
        return ProbeSlotPlan(
            slot_id=slot_id,
            worker_id=worker.worker_id,
            target_conversation_url=worker.conversation_url,
            action=ProbeSlotAction.OPEN,
            mutation_required=True,
            reason="no probe slot is bound; open exactly one tagged CWS probe window",
            checks=checks,
            owner_token=uuid.uuid4().hex,
        )

    checks.update(
        {
            "slot_id_matches": existing.slot_id == slot_id,
            "slot_source_owned": existing.source == PROBE_SLOT_SOURCE,
            "slot_fresh": existing.is_fresh(now=now),
            "slot_owner_tag_matches": slot_owns_actual_url(existing),
        }
    )
    if not all(checks.values()):
        return ProbeSlotPlan(
            slot_id=slot_id,
            worker_id=worker.worker_id,
            target_conversation_url=worker.conversation_url,
            action=ProbeSlotAction.BLOCKED,
            mutation_required=False,
            reason=(
                "existing probe slot is stale, ambiguous, or no longer proves CWS ownership; "
                "reconcile it before opening another window"
            ),
            checks=checks,
            owner_token=existing.owner_token,
        )

    same_target = (
        existing.target_worker_id == worker.worker_id
        and _conversation_identity(existing.target_conversation_url)
        == _conversation_identity(worker.conversation_url)
    )
    checks["same_target"] = same_target
    if same_target:
        return ProbeSlotPlan(
            slot_id=slot_id,
            worker_id=worker.worker_id,
            target_conversation_url=worker.conversation_url,
            action=ProbeSlotAction.REUSE,
            mutation_required=False,
            reason="fresh CWS-owned probe slot already targets this parked worker",
            checks=checks,
            owner_token=existing.owner_token,
        )

    return ProbeSlotPlan(
        slot_id=slot_id,
        worker_id=worker.worker_id,
        target_conversation_url=worker.conversation_url,
        action=ProbeSlotAction.ROTATE,
        mutation_required=True,
        reason="rotate the single CWS probe slot by exact-close-before-open",
        checks=checks,
        owner_token=uuid.uuid4().hex,
    )


def observe_probe_slot(
    worker: WorkerRecord,
    slot: ProbeWindowSlotBinding,
    probe: ChromeUiaProbe,
    *,
    previous: BrowserObservation | None = None,
) -> BrowserObservation:
    """Observe a tagged CWS-owned probe window and attribute it to its parked worker."""
    if worker.status != WorkerStatus.PARKED:
        raise UiaProbeUnavailable("probe-slot observation requires a parked worker")
    if slot.target_worker_id != worker.worker_id:
        raise UiaProbeUnavailable("probe slot belongs to a different worker")
    if not slot_owns_actual_url(slot):
        raise UiaProbeUnavailable("probe slot no longer proves CWS URL ownership")
    if _conversation_identity(slot.actual_url) != _conversation_identity(worker.conversation_url):
        raise UiaProbeUnavailable("probe slot conversation identity does not match the worker")

    tagged_worker = replace(worker, conversation_url=slot.actual_url)
    obs = probe.observe(tagged_worker, previous=previous, expected_hwnd=slot.window_handle)
    obs.worker_id = worker.worker_id
    obs.url = worker.conversation_url
    obs.raw = dict(obs.raw or {})
    obs.raw.update({"probe_slot_id": slot.slot_id, "probe_slot_owner": True})
    return obs


_FIND_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
$expected=[string]$env:CWS_EXPECTED_ACTUAL_URL
$expectedExe=[string]$env:CWS_EXPECTED_CHROME_EXE
$desktop=[System.Windows.Automation.AutomationElement]::RootElement
$wins=$desktop.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
$cond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'view_1012')
$rows=@()
for($i=0;$i -lt $wins.Count;$i++){
  $w=$wins.Item($i); if([string]$w.Current.ClassName -ne 'Chrome_WidgetWin_1'){continue}
  $pid=[int]$w.Current.ProcessId; if(-not $pid){continue}; try{$proc=Get-Process -Id $pid -ErrorAction Stop}catch{continue}
  if($proc.ProcessName -ne 'chrome' -or -not ([string]$proc.Path).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase)){continue}
  $bar=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond); if(-not $bar){continue}
  $address=Get-Value $bar; if(-not $address){continue}; if(-not $address.StartsWith('http')){$address='https://'+$address}
  if($address.TrimEnd('/') -ne $expected.TrimEnd('/')){continue}
  $rows += [pscustomobject]@{window_handle=[int64]$w.Current.NativeWindowHandle;browser_pid=$pid;actual_url=$address}
}
$obj=[pscustomobject]@{count=[int]$rows.Count;matches=@($rows)}
$json=$obj|ConvertTo-Json -Compress -Depth 5
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''

_CLOSE_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
$hwnd=[int64]$env:CWS_EXPECTED_HWND; $pidExpected=[int]$env:CWS_EXPECTED_BROWSER_PID
$expected=[string]$env:CWS_EXPECTED_ACTUAL_URL; $expectedExe=[string]$env:CWS_EXPECTED_CHROME_EXE
try{$w=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)}catch{$w=$null}
if(-not $w){$obj=[pscustomobject]@{closed=$false;absent=$true;ambiguous=$false;detail='slot window already absent'}}
else{
  $pid=[int]$w.Current.ProcessId
  if([string]$w.Current.ClassName -ne 'Chrome_WidgetWin_1' -or $pid -ne $pidExpected){$obj=[pscustomobject]@{closed=$false;absent=$false;ambiguous=$true;detail='HWND no longer matches bound Chrome identity'}}
  else{
    try{$proc=Get-Process -Id $pid -ErrorAction Stop}catch{$proc=$null}
    $cond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'view_1012')
    $bar=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond); $address=if($bar){Get-Value $bar}else{$null}; if($address -and -not $address.StartsWith('http')){$address='https://'+$address}
    if(-not $proc -or -not ([string]$proc.Path).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase) -or -not $address -or $address.TrimEnd('/') -ne $expected.TrimEnd('/')){$obj=[pscustomobject]@{closed=$false;absent=$false;ambiguous=$true;detail='slot executable or ownership URL changed'}}
    else{
      try{$wp=$w.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern);$wp.Close();$obj=[pscustomobject]@{closed=$true;absent=$false;ambiguous=$false;detail='exact CWS probe window close requested'}}catch{$obj=[pscustomobject]@{closed=$false;absent=$false;ambiguous=$true;detail='exact window could not be closed safely'}}
    }
  }
}
$json=$obj|ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


def _run_ps(
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
        capture_output=True,
        text=False,
        timeout=timeout_s,
        env=env,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace")[-1000:])
    lines = completed.stdout.decode("ascii", errors="ignore").strip().splitlines()
    if not lines:
        raise RuntimeError("UIA probe-slot helper produced no output")
    return json.loads(base64.b64decode(lines[-1]).decode("utf-8"))


class ChromeUiaProbeWindowTransport:
    """Gated single-window probe transport for normal Chrome.

    It never navigates or closes an arbitrary user window. Every close requires the durable
    CWS slot's exact HWND, PID, executable, and ownership-tagged URL. Rotation closes the old
    slot before launching the replacement, so one logical probe slot cannot proliferate into
    several live windows. There is no production CLI enable path by default.
    """

    name = PROBE_SLOT_SOURCE

    def __init__(
        self,
        *,
        chrome_executable: str,
        enabled: bool = False,
        powershell: str = "powershell.exe",
        timeout_s: float = 8.0,
        open_timeout_s: float = 12.0,
        slot_ttl_s: float = 120.0,
    ) -> None:
        self.chrome_executable = chrome_executable
        self.enabled = bool(enabled)
        self.powershell = powershell
        self.timeout_s = max(1.0, float(timeout_s))
        self.open_timeout_s = max(1.0, float(open_timeout_s))
        self.slot_ttl_s = max(5.0, float(slot_ttl_s))

    def _find(self, actual_url: str) -> dict[str, Any]:
        env = os.environ.copy()
        env["CWS_EXPECTED_ACTUAL_URL"] = actual_url
        env["CWS_EXPECTED_CHROME_EXE"] = self.chrome_executable
        return _run_ps(
            _FIND_SCRIPT,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )

    def _close(self, slot: ProbeWindowSlotBinding) -> dict[str, Any]:
        env = os.environ.copy()
        env.update(
            {
                "CWS_EXPECTED_HWND": str(slot.window_handle),
                "CWS_EXPECTED_BROWSER_PID": str(slot.browser_pid),
                "CWS_EXPECTED_ACTUAL_URL": slot.actual_url,
                "CWS_EXPECTED_CHROME_EXE": slot.chrome_executable,
            }
        )
        return _run_ps(
            _CLOSE_SCRIPT,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )

    def execute(
        self,
        plan: ProbeSlotPlan,
        *,
        existing: ProbeWindowSlotBinding | None,
    ) -> ProbeSlotExecution:
        if not self.enabled:
            raise ProbeWindowTransportDisabled(
                "normal-Chrome probe-window mutation transport is gated"
            )
        if os.name != "nt":
            return ProbeSlotExecution(False, False, "Windows UI Automation is unavailable")
        if plan.action == ProbeSlotAction.BLOCKED:
            return ProbeSlotExecution(False, False, plan.reason)

        if plan.action == ProbeSlotAction.REUSE:
            if existing is None:
                return ProbeSlotExecution(False, False, "reuse requires an existing slot")
            found = self._find(existing.actual_url)
            matches = found.get("matches") or []
            if int(found.get("count") or 0) != 1:
                return ProbeSlotExecution(
                    False,
                    False,
                    "fresh slot was not uniquely observable; reconcile before reuse",
                )
            row = matches[0]
            if (
                int(row.get("window_handle") or 0) != existing.window_handle
                or int(row.get("browser_pid") or 0) != existing.browser_pid
            ):
                return ProbeSlotExecution(
                    False,
                    False,
                    "fresh slot identity changed; reconcile before reuse",
                )
            now = time.time()
            refreshed = replace(
                existing,
                observed_at=now,
                expires_at=now + self.slot_ttl_s,
            )
            return ProbeSlotExecution(False, False, "existing CWS probe window reused", refreshed)

        if plan.action == ProbeSlotAction.ROTATE:
            if existing is None:
                return ProbeSlotExecution(False, False, "rotate requires the existing slot")
            close_result = self._close(existing)
            if bool(close_result.get("ambiguous")):
                return ProbeSlotExecution(
                    False,
                    False,
                    str(close_result.get("detail") or "slot close became ambiguous"),
                )
            # WindowPattern.Close is asynchronous. Do not launch a replacement merely because
            # Close() returned: first prove the ownership-tagged old window is absent.
            absence_deadline = time.time() + min(5.0, self.open_timeout_s)
            old_count = None
            while time.time() < absence_deadline:
                old_probe = self._find(existing.actual_url)
                old_count = int(old_probe.get("count") or 0)
                if old_count == 0:
                    break
                if old_count > 1:
                    return ProbeSlotExecution(
                        False,
                        True,
                        "old probe ownership tag became multiply bound after close; reconcile",
                    )
                time.sleep(0.05)
            if old_count != 0:
                return ProbeSlotExecution(
                    False,
                    True,
                    "exact old probe close was requested but absence was not proven; replacement not opened",
                )
        elif plan.action != ProbeSlotAction.OPEN:
            return ProbeSlotExecution(False, False, "unsupported probe-slot action")

        owner_token = plan.owner_token or uuid.uuid4().hex
        actual_url = tagged_probe_url(
            plan.target_conversation_url,
            slot_id=plan.slot_id,
            owner_token=owner_token,
        )
        try:
            subprocess.Popen(
                [self.chrome_executable, "--new-window", actual_url],
                close_fds=True,
            )
        except BaseException as exc:
            return ProbeSlotExecution(
                False,
                False,
                f"Chrome launch failed before a probe window was observed: {type(exc).__name__}",
            )

        deadline = time.time() + self.open_timeout_s
        while time.time() < deadline:
            found = self._find(actual_url)
            matches = found.get("matches") or []
            count = int(found.get("count") or 0)
            if count == 1:
                row = matches[0]
                now = time.time()
                binding = ProbeWindowSlotBinding(
                    slot_id=plan.slot_id,
                    owner_token=owner_token,
                    target_worker_id=plan.worker_id,
                    target_conversation_url=plan.target_conversation_url,
                    actual_url=actual_url,
                    window_handle=int(row.get("window_handle") or 0),
                    browser_pid=int(row.get("browser_pid") or 0),
                    chrome_executable=self.chrome_executable,
                    source=PROBE_SLOT_SOURCE,
                    bound_at=now,
                    observed_at=now,
                    expires_at=now + self.slot_ttl_s,
                )
                if binding.window_handle > 0 and binding.browser_pid > 0:
                    return ProbeSlotExecution(
                        True,
                        True,
                        "single tagged CWS probe window opened",
                        binding,
                    )
            if count > 1:
                return ProbeSlotExecution(
                    False,
                    True,
                    "multiple tagged probe windows appeared; stop and reconcile",
                )
            time.sleep(0.1)

        return ProbeSlotExecution(
            False,
            True,
            "Chrome launch occurred but the tagged probe window was not uniquely observed before timeout",
        )
