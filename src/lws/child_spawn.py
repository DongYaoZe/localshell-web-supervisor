from __future__ import annotations

import base64
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit, urlunsplit

from .models import (
    ChildSpawnAttempt,
    ChildSpawnAttemptState,
    WorkerRecord,
    WorkerWindowBinding,
    WorkspaceObservation,
)
from .page_runtime import _FIND_SCRIPT, _run_ps, ChromeUiaProbeWindowTransport
from .registry import Registry
from .uia_actions import _POWERSHELL_ACTION, _run_powershell_json


_PROJECT_TOKEN = re.compile(r"^(g-p-([0-9a-fA-F]{32}))(?:-[^/]+)?$")
_CONVERSATION_SEGMENT = re.compile(r"^[0-9a-fA-F-]{8,}$")
_WEB_CHILD_DISPATCH_COOLDOWN = "web_child_dispatch"
_DEFAULT_RATE_LIMIT_COOLDOWN_S = 120.0


class ChildSpawnBlocked(RuntimeError):
    def __init__(self, blockers: list[str]):
        self.blockers = tuple(blockers)
        super().__init__("; ".join(blockers))


@dataclass(slots=True)
class ChildSpawnExecution:
    changed: bool
    side_effect_possible: bool
    detail: str
    window_handle: int | None = None
    browser_pid: int | None = None
    url: str | None = None
    rate_limited: bool = False
    modal_dismissed: bool = False

    @property
    def submitted(self) -> bool:
        return self.changed


@dataclass(slots=True)
class ChildSpawnDeliveryObservation:
    url: str
    generating: bool
    composer_value: str
    send_button_ready: bool
    prompt_matches_draft: bool
    detail: str

    @property
    def delivered(self) -> bool:
        return bool(self.url) and (
            self.generating or (not self.prompt_matches_draft and not self.send_button_ready)
        )


_WINDOW_URL_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
$hwnd=[int64]$env:LWS_EXPECTED_HWND; $pidExpected=[int]$env:LWS_EXPECTED_BROWSER_PID
$expectedExe=[string]$env:LWS_EXPECTED_CHROME_EXE
try{$w=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)}catch{$w=$null}
if(-not $w){$obj=[pscustomobject]@{present=$false;ambiguous=$false;detail='bound spawn window absent'}}
else{
  $browserPid=[int]$w.Current.ProcessId
  if([string]$w.Current.ClassName -ne 'Chrome_WidgetWin_1' -or $browserPid -ne $pidExpected){$obj=[pscustomobject]@{present=$false;ambiguous=$true;detail='HWND no longer matches bound Chrome identity'}}
  else{
    try{$proc=Get-Process -Id $browserPid -ErrorAction Stop}catch{$proc=$null}
    $cond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'view_1012')
    $bar=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond); $address=if($bar){Get-Value $bar}else{$null}; if($address -and -not $address.StartsWith('http')){$address='https://'+$address}
    if(-not $proc -or -not ([string]$proc.Path).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase)){$obj=[pscustomobject]@{present=$false;ambiguous=$true;detail='bound spawn executable changed'}}
    elseif(-not $address){$obj=[pscustomobject]@{present=$true;ambiguous=$true;detail='bound spawn address unavailable'}}
    else{$obj=[pscustomobject]@{present=$true;ambiguous=$false;window_handle=[int64]$w.Current.NativeWindowHandle;browser_pid=$browserPid;url=$address;detail='bound spawn window observed'}}
  }
}
$json=$obj|ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


_DELIVERY_OBSERVE_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
function Find-ByAutomationId($root,[string]$id){$c=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty,$id);return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$c)}
$hwnd=[int64]$env:LWS_EXPECTED_HWND
$pidExpected=[int]$env:LWS_EXPECTED_BROWSER_PID
$expectedExe=[string]$env:LWS_EXPECTED_CHROME_EXE
try{$w=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)}catch{$w=$null}
if(-not $w){$obj=[pscustomobject]@{present=$false;ambiguous=$false;detail='bound delivery window absent'}}
else{
  $actualPid=[int]$w.Current.ProcessId
  try{$proc=Get-Process -Id $actualPid -ErrorAction Stop}catch{$proc=$null}
  $bar=Find-ByAutomationId $w 'view_1012'
  $address=if($bar){Get-Value $bar}else{$null}
  if($address -and -not $address.StartsWith('http')){$address='https://'+$address}
  if([string]$w.Current.ClassName -ne 'Chrome_WidgetWin_1' -or $actualPid -ne $pidExpected -or -not $proc -or -not ([string]$proc.Path).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase)){
    $obj=[pscustomobject]@{present=$false;ambiguous=$true;detail='bound delivery HWND/PID/executable identity changed'}
  } elseif(-not $address){
    $obj=[pscustomobject]@{present=$true;ambiguous=$true;detail='bound delivery address unavailable'}
  } else {
    $docCond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Document)
    $doc=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$docCond)
    $composer=if($doc){Find-ByAutomationId $doc 'prompt-textarea'}else{$null}
    $composerValue=if($composer){[string](Get-Value $composer)}else{''}
    $send=if($doc){Find-ByAutomationId $doc 'composer-submit-button'}else{$null}
    $sendReady=[bool]($send -and [bool]$send.Current.IsEnabled -and -not [bool]$send.Current.IsOffscreen)
    $generating=$false
    $buttonCond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Button)
    $buttons=$w.FindAll([System.Windows.Automation.TreeScope]::Descendants,$buttonCond)
    for($i=0;$i -lt $buttons.Count;$i++){
      $name=[string]$buttons.Item($i).Current.Name
      if($name -match 'Stop answering|Stop generating|停止生成|停止回答'){$generating=$true;break}
    }
    $obj=[pscustomobject]@{present=$true;ambiguous=$false;url=$address;generating=$generating;composer_value=$composerValue;send_ready=$sendReady;detail='bound delivery state observed'}
  }
}
$json=$obj|ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


_INVOKE_EXISTING_DRAFT_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
function Find-ByAutomationId($root,[string]$id){$c=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty,$id);return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$c)}
function NormPrompt([string]$v){if($null -eq $v){return ''};return ($v -replace "`r`n","`n")}
function SamePrompt([string]$a,[string]$b){$x=NormPrompt $a;$y=NormPrompt $b;if($x -ceq $y){return $true};if($y.EndsWith("`n") -and $x -ceq $y.Substring(0,$y.Length-1)){return $true};return $false}
$hwnd=[int64]$env:LWS_EXPECTED_HWND
$pidExpected=[int]$env:LWS_EXPECTED_BROWSER_PID
$expectedExe=[string]$env:LWS_EXPECTED_CHROME_EXE
$expectedUrl=[string]$env:LWS_EXPECTED_URL
$prompt=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:LWS_PROMPT_B64))
try{$w=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)}catch{$w=$null}
if(-not $w){$obj=[pscustomobject]@{submitted=$false;side_effect_possible=$false;detail='second-stage HWND absent'}}
else{
  $actualPid=[int]$w.Current.ProcessId
  try{$proc=Get-Process -Id $actualPid -ErrorAction Stop}catch{$proc=$null}
  $bar=Find-ByAutomationId $w 'view_1012'
  $address=if($bar){Get-Value $bar}else{$null}
  if($address -and -not $address.StartsWith('http')){$address='https://'+$address}
  if([string]$w.Current.ClassName -ne 'Chrome_WidgetWin_1' -or $actualPid -ne $pidExpected -or -not $proc -or -not ([string]$proc.Path).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase) -or $address.TrimEnd('/') -ne $expectedUrl.TrimEnd('/')){
    $obj=[pscustomobject]@{submitted=$false;side_effect_possible=$false;detail='second-stage exact window identity changed'}
  } else {
    $docCond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Document)
    $doc=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$docCond)
    $composer=if($doc){Find-ByAutomationId $doc 'prompt-textarea'}else{$null}
    $current=if($composer){Get-Value $composer}else{$null}
    $send=if($doc){Find-ByAutomationId $doc 'composer-submit-button'}else{$null}
    if(-not $composer -or -not (SamePrompt $current $prompt)){
      $obj=[pscustomobject]@{submitted=$false;side_effect_possible=$false;detail='second-stage composer no longer contains the exact persisted prompt'}
    } elseif(-not $send -or -not [bool]$send.Current.IsEnabled -or [bool]$send.Current.IsOffscreen){
      $obj=[pscustomobject]@{submitted=$false;side_effect_possible=$false;detail='second-stage exact prompt is not send-ready'}
    } else {
      try{$invoke=$send.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern);$invoke.Invoke();$obj=[pscustomobject]@{submitted=$true;side_effect_possible=$true;detail='second-stage exact-draft Send invocation returned'}}
      catch{$obj=[pscustomobject]@{submitted=$false;side_effect_possible=$true;detail='second-stage Send outcome ambiguous'}}
    }
  }
}
$json=$obj|ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


def _project_parts(url: str) -> tuple[str, bool]:
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError as exc:
        raise ValueError("invalid web project URL") from exc
    if parsed.scheme.lower() != "https" or parsed.hostname != "chatgpt.com":
        raise ValueError("web project URL must use https://chatgpt.com")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("web project URL must not contain credentials or a custom port")
    if parsed.query or parsed.fragment:
        raise ValueError("web project URL must not contain query parameters or a fragment")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) not in {2, 3} or segments[0] != "g":
        raise ValueError("web project URL must point to one project root")
    match = _PROJECT_TOKEN.fullmatch(segments[1])
    if match is None:
        raise ValueError("web project URL does not contain a supported g-p project id")
    if len(segments) == 3 and segments[2] != "project":
        raise ValueError("web project URL must not point to an existing conversation")
    return match.group(2).lower(), len(segments) == 3


def web_project_id(url: str) -> str:
    project_id, _ = _project_parts(url)
    return project_id


def web_conversation_project_id(url: str) -> str | None:
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.hostname != "chatgpt.com":
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 4 or segments[0] != "g" or segments[2] != "c":
        return None
    match = _PROJECT_TOKEN.fullmatch(segments[1])
    if match is None or _CONVERSATION_SEGMENT.fullmatch(segments[3]) is None:
        return None
    return match.group(2).lower()


def conversation_identity(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.hostname != "chatgpt.com":
        return None
    if parsed.query or parsed.fragment:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 4 or segments[0] != "g" or segments[2] != "c":
        return None
    match = _PROJECT_TOKEN.fullmatch(segments[1])
    if match is None or _CONVERSATION_SEGMENT.fullmatch(segments[3]) is None:
        return None
    return match.group(2).lower(), segments[3].lower()


def conversation_url_matches(left: str, right: str) -> bool:
    li = conversation_identity(left)
    ri = conversation_identity(right)
    return li is not None and li == ri


def conversation_url_candidates(url: str) -> tuple[str, ...]:
    ident = conversation_identity(url)
    if ident is None:
        return (str(url).strip(),)
    project_id, conversation_id = ident
    parsed = urlsplit(str(url).strip())
    segments = [segment for segment in parsed.path.split("/") if segment]
    token = segments[1]
    canonical = urlunsplit((parsed.scheme, parsed.netloc, f"/g/g-p-{project_id}/c/{conversation_id}", "", ""))
    original = urlunsplit((parsed.scheme, parsed.netloc, f"/g/{token}/c/{conversation_id}", "", ""))
    return tuple(dict.fromkeys((original, canonical)))


def _owned_project_url_candidates(attempt: ChildSpawnAttempt) -> tuple[str, ...]:
    parsed = urlsplit(attempt.project_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    token = segments[1]
    bases = [f"/g/{token}", f"/g/{token}/project", f"/g/g-p-{attempt.project_id}", f"/g/g-p-{attempt.project_id}/project"]
    return tuple(dict.fromkeys(urlunsplit((parsed.scheme, parsed.netloc, path, "", f"lws-child={attempt.owner_token}")) for path in bases))


def _prompt_equivalent(observed: str | None, expected: str) -> bool:
    left = (observed or "").replace("\r\n", "\n")
    right = expected.replace("\r\n", "\n")
    return left == right or (right.endswith("\n") and left == right[:-1])


def web_route_project_id(url: str) -> str | None:
    """Return an explicit project id from any recognized project-scoped route."""

    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.hostname != "chatgpt.com":
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0] != "g":
        return None
    match = _PROJECT_TOKEN.fullmatch(segments[1])
    return match.group(2).lower() if match is not None else None


def tagged_project_url(project_url: str, owner_token: str) -> str:
    web_project_id(project_url)
    owner = str(owner_token).strip()
    if not owner or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", owner):
        raise ValueError("child-spawn owner token is invalid")
    parsed = urlsplit(project_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", f"lws-child={owner}"))


def owned_project_root_matches(url: str, attempt: ChildSpawnAttempt) -> bool:
    """Accept web chat's root canonicalization only when the LWS owner tag is unchanged."""

    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return False
    if parsed.query or parsed.fragment != f"lws-child={attempt.owner_token}":
        return False
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    try:
        return web_project_id(base) == attempt.project_id
    except ValueError:
        return False


def _fresh_workspace(
    dispatch,
    workspace: WorkspaceObservation | None,
    *,
    now: float,
    max_age_s: float,
) -> list[str]:
    if workspace is None:
        return ["fresh workspace reconciliation is required before opening a child conversation"]
    blockers: list[str] = []
    age = now - float(workspace.observed_at)
    if age < 0 or age > max_age_s:
        blockers.append("workspace observation is stale or from the future")
    if not workspace.cwd_exists:
        blockers.append("child working directory does not exist")
    if workspace.error:
        blockers.append(f"workspace reconciliation failed: {workspace.error}")
    if dispatch.expected_branch:
        if workspace.is_git_repo is not True:
            blockers.append("dispatch expects a Git branch but workspace is not a verified Git repository")
        elif workspace.git_branch != dispatch.expected_branch:
            blockers.append(
                f"workspace branch changed: expected {dispatch.expected_branch}, "
                f"observed {workspace.git_branch or '<detached>'}"
            )
    return blockers


def arm_child_spawn(
    registry: Registry,
    *,
    child_task_id: str,
    chrome_executable: str,
    workspace: WorkspaceObservation | None,
    now: float | None = None,
    max_evidence_age_s: float = 60.0,
) -> ChildSpawnAttempt:
    ts = time.time() if now is None else float(now)
    dispatch = registry.get_child_dispatch(child_task_id)
    protocol = registry.load_worker_protocol(child_task_id)
    blockers: list[str] = []
    project_url = dispatch.web_project_url
    if not project_url:
        blockers.append("child dispatch has no explicit web project URL")
        project_id = ""
    else:
        try:
            project_id = web_project_id(project_url)
        except ValueError as exc:
            blockers.append(str(exc))
            project_id = ""
    chrome_executable = str(chrome_executable).strip()
    if not chrome_executable:
        blockers.append("Google Chrome executable is required")
    if protocol.current_worker_id is not None or protocol.generation != 0 or protocol.workers:
        blockers.append("automatic child-spawn is initial-worker only; task already has worker history")
    if registry.unresolved_action_attempt(child_task_id) is not None:
        blockers.append("child has an unresolved external browser action")
    if registry.unresolved_probe_mutation_operation() is not None:
        blockers.append("a global probe-window mutation is unresolved")
    if registry.unresolved_replacement_attempt(child_task_id) is not None:
        blockers.append("child has an unresolved replacement attempt")
    if registry.unresolved_child_spawn_attempt(child_task_id) is not None:
        blockers.append("child already has an unresolved spawn attempt")
    blockers.extend(
        _fresh_workspace(dispatch, workspace, now=ts, max_age_s=max_evidence_age_s)
    )
    if blockers:
        raise ChildSpawnBlocked(blockers)

    attempt_id = f"spawn_{uuid.uuid4().hex[:16]}"
    owner_token = f"{attempt_id}:{uuid.uuid4().hex[:16]}"
    attempt = ChildSpawnAttempt(
        attempt_id=attempt_id,
        child_task_id=child_task_id,
        state=ChildSpawnAttemptState.ARMED,
        owner_token=owner_token,
        project_url=project_url,
        project_id=project_id,
        tagged_project_url=tagged_project_url(project_url, owner_token),
        prompt_sha256=dispatch.prompt_sha256,
        chrome_executable=chrome_executable,
        created_at=ts,
        updated_at=ts,
        metadata={
            "workspace_git_head": workspace.git_head if workspace else None,
            "workspace_status_hash": workspace.git_status_hash if workspace else None,
            "workspace_git_branch": workspace.git_branch if workspace else None,
        },
    )
    return registry.record_child_spawn_attempt(attempt)


def submit_child_spawn_open(
    registry: Registry, attempt_id: str, *, now: float | None = None
) -> ChildSpawnAttempt:
    attempt = registry.get_child_spawn_attempt(attempt_id)
    if attempt.state != ChildSpawnAttemptState.ARMED:
        raise RuntimeError(f"child spawn {attempt_id} is not ARMED")
    return registry.update_child_spawn_attempt(
        attempt_id,
        state=ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
        now=now,
    )


def bind_child_spawn_window(
    registry: Registry,
    *,
    attempt_id: str,
    window_handle: int,
    browser_pid: int,
    observed_url: str,
    now: float | None = None,
) -> ChildSpawnAttempt:
    attempt = registry.get_child_spawn_attempt(attempt_id)
    if attempt.state not in {
        ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED,
        ChildSpawnAttemptState.RECONCILE_REQUIRED,
    }:
        raise RuntimeError("child spawn is not waiting for its owned project window")
    if int(window_handle) <= 0 or int(browser_pid) <= 0:
        raise ValueError("spawn window binding requires positive HWND and Chrome PID")
    if not owned_project_root_matches(observed_url, attempt):
        raise ChildSpawnBlocked(["observed project window URL does not match the LWS ownership tag/project id"])
    return registry.update_child_spawn_attempt(
        attempt_id,
        state=ChildSpawnAttemptState.WINDOW_BOUND,
        window_handle=int(window_handle),
        browser_pid=int(browser_pid),
        now=now,
    )


def submit_child_spawn_prompt(
    registry: Registry, attempt_id: str, *, now: float | None = None
) -> tuple[ChildSpawnAttempt, str]:
    attempt = registry.get_child_spawn_attempt(attempt_id)
    if attempt.state != ChildSpawnAttemptState.WINDOW_BOUND:
        raise RuntimeError("child spawn project window is not durably bound")
    dispatch = registry.get_child_dispatch(attempt.child_task_id)
    if dispatch.prompt_sha256 != attempt.prompt_sha256:
        raise ChildSpawnBlocked(["persisted child prompt changed after spawn was armed"])
    submitted = registry.update_child_spawn_attempt(
        attempt_id,
        state=ChildSpawnAttemptState.PROMPT_SUBMITTED,
        now=now,
    )
    return submitted, dispatch.prompt_text


def _complete_from_bound_conversation(
    registry: Registry,
    *,
    attempt: ChildSpawnAttempt,
    conversation_url: str,
    now: float,
    lease_seconds: float,
) -> ChildSpawnAttempt:
    observed_project = web_conversation_project_id(conversation_url)
    if observed_project != attempt.project_id:
        raise ChildSpawnBlocked(["bound window did not transition to a conversation in the expected project"])
    state = registry.adopt_child_worker(
        attempt.child_task_id,
        conversation_url,
        lease_seconds=lease_seconds,
        worker_id=attempt.worker_id,
        now=now,
    )
    worker_id = state.current_worker_id
    if not worker_id:
        raise RuntimeError("child-spawn conversation adoption did not publish a current worker")
    registry.bind_worker_window(
        worker_id,
        window_handle=int(attempt.window_handle or 0),
        browser_pid=int(attempt.browser_pid or 0),
        chrome_executable=attempt.chrome_executable,
        conversation_url=conversation_url,
        source="windows_uia_chrome",
        observed_at=now,
        ttl_s=60.0,
    )
    return registry.update_child_spawn_attempt(
        attempt.attempt_id,
        state=ChildSpawnAttemptState.COMPLETED,
        conversation_url=conversation_url,
        worker_id=worker_id,
        now=now,
    )


_NAVIGATE_EXACT_WINDOW_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class LwsNativeNav {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
"@
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
function Norm([string]$v){if(-not $v){return ''};$x=$v.Trim();if(-not $x.StartsWith('http')){$x='https://'+$x};return $x.TrimEnd('/')}
$hwnd=[int64]$env:LWS_EXPECTED_HWND
$pidExpected=[int]$env:LWS_EXPECTED_BROWSER_PID
$expectedExe=[string]$env:LWS_EXPECTED_CHROME_EXE
$source=Norm $env:LWS_EXPECTED_SOURCE_URL
$target=[string]$env:LWS_TARGET_URL
try{$w=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)}catch{$w=$null}
if(-not $w){$obj=[pscustomobject]@{changed=$false;ambiguous=$false;detail='dispatcher HWND absent'}}
else{
  $actualPid=[int]$w.Current.ProcessId
  try{$proc=Get-Process -Id $actualPid -ErrorAction Stop}catch{$proc=$null}
  $cond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'view_1012')
  $bar=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
  $cur=if($bar){Norm (Get-Value $bar)}else{''}
  if([string]$w.Current.ClassName -ne 'Chrome_WidgetWin_1' -or $actualPid -ne $pidExpected -or -not $proc -or -not ([string]$proc.Path).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase)){
    $obj=[pscustomobject]@{changed=$false;ambiguous=$true;detail='dispatcher HWND/PID/executable identity changed'}
  } elseif($cur -ne $source){
    $obj=[pscustomobject]@{changed=$false;ambiguous=$true;detail='dispatcher source URL changed'}
  } elseif(-not $bar){
    $obj=[pscustomobject]@{changed=$false;ambiguous=$false;detail='Chrome address bar unavailable'}
  } else {
    [void][LwsNativeNav]::SetForegroundWindow([IntPtr]$hwnd)
    Start-Sleep -Milliseconds 50
    if([LwsNativeNav]::GetForegroundWindow().ToInt64() -ne $hwnd){
      $obj=[pscustomobject]@{changed=$false;ambiguous=$false;detail='could not positively foreground exact dispatcher HWND'}
    } else {
      try{
        $bar.SetFocus()
        $vp=$bar.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $vp.SetValue($target)
        [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
        $obj=[pscustomobject]@{changed=$true;ambiguous=$false;detail='exact dispatcher HWND navigation submitted'}
      }catch{
        $obj=[pscustomobject]@{changed=$false;ambiguous=$true;detail='dispatcher navigation outcome ambiguous'}
      }
    }
  }
}
$json=$obj|ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


_CLOSE_OWNED_PROJECT_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
function Norm([string]$v){if(-not $v){return ''};$x=$v.Trim();if(-not $x.StartsWith('http')){$x='https://'+$x};return $x.TrimEnd('/')}
$hwnd=[int64]$env:LWS_EXPECTED_HWND
$pidExpected=[int]$env:LWS_EXPECTED_BROWSER_PID
$expectedExe=[string]$env:LWS_EXPECTED_CHROME_EXE
$expected=@()
if($env:LWS_EXPECTED_URL_1){$expected+=Norm $env:LWS_EXPECTED_URL_1}
if($env:LWS_EXPECTED_URL_2){$expected+=Norm $env:LWS_EXPECTED_URL_2}
try{$w=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)}catch{$w=$null}
if(-not $w){$obj=[pscustomobject]@{closed=$false;absent=$true;ambiguous=$false;detail='exact child window already absent'}}
else{
  $actualPid=[int]$w.Current.ProcessId
  try{$proc=Get-Process -Id $actualPid -ErrorAction Stop}catch{$proc=$null}
  $cond=New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty,'view_1012')
  $bar=$w.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$cond)
  $cur=if($bar){Norm (Get-Value $bar)}else{''}
  if([string]$w.Current.ClassName -ne 'Chrome_WidgetWin_1' -or $actualPid -ne $pidExpected -or -not $proc -or -not ([string]$proc.Path).Equals($expectedExe,[StringComparison]::OrdinalIgnoreCase) -or -not ($expected -contains $cur)){
    $obj=[pscustomobject]@{closed=$false;absent=$false;ambiguous=$true;detail='exact child window close identity changed'}
  } else {
    try{$wp=$w.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern);$wp.Close();$obj=[pscustomobject]@{closed=$true;absent=$false;ambiguous=$false;detail='exact LWS child window close requested'}}
    catch{$obj=[pscustomobject]@{closed=$false;absent=$false;ambiguous=$true;detail='exact child window could not be closed safely'}}
  }
}
$json=$obj|ConvertTo-Json -Compress -Depth 4
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
'''


class ChromeUiaChildSpawnTransport:
    """Explicitly gated normal-Chrome transport for one LWS-owned project-root window."""

    name = "windows_uia_child_spawn"

    def __init__(
        self,
        *,
        chrome_executable: str,
        enabled: bool = False,
        powershell: str = "powershell.exe",
        timeout_s: float = 8.0,
        open_timeout_s: float = 12.0,
        conversation_timeout_s: float = 20.0,
    ) -> None:
        self.chrome_executable = str(chrome_executable)
        self.enabled = bool(enabled)
        self.powershell = powershell
        self.timeout_s = max(1.0, float(timeout_s))
        self.open_timeout_s = max(1.0, float(open_timeout_s))
        self.conversation_timeout_s = max(1.0, float(conversation_timeout_s))

    def _find(self, url: str):
        env = os.environ.copy()
        env["LWS_EXPECTED_ACTUAL_URL"] = url
        env["LWS_EXPECTED_CHROME_EXE"] = self.chrome_executable
        found = _run_ps(
            _FIND_SCRIPT,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )
        return ChromeUiaProbeWindowTransport._matches(found)

    def observe_owned_project(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        try:
            matches_by_identity = {}
            for candidate in _owned_project_url_candidates(attempt):
                for item in self._find(candidate):
                    matches_by_identity[(item.window_handle, item.browser_pid)] = item
            matches = list(matches_by_identity.values())
        except BaseException as exc:
            return ChildSpawnExecution(False, False, f"owned project observation failed: {type(exc).__name__}")
        if len(matches) != 1:
            return ChildSpawnExecution(
                False,
                False,
                f"expected exactly one LWS-tagged project window, observed {len(matches)}",
            )
        match = matches[0]
        if match.chrome_executable.casefold() != attempt.chrome_executable.casefold():
            return ChildSpawnExecution(False, False, "owned project window executable changed")
        return ChildSpawnExecution(
            False,
            False,
            "single LWS-tagged project window observed",
            window_handle=match.window_handle,
            browser_pid=match.browser_pid,
            url=match.actual_url,
        )

    def open_authorized(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        if not self.enabled:
            raise ChildSpawnBlocked(["child-spawn browser mutation transport is gated"])
        if os.name != "nt":
            return ChildSpawnExecution(False, False, "Windows UI Automation is unavailable")
        if attempt.state != ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED:
            return ChildSpawnExecution(False, False, "WINDOW_OPEN authority was not durably submitted")
        if attempt.chrome_executable.casefold() != self.chrome_executable.casefold():
            return ChildSpawnExecution(False, False, "transport executable does not match durable spawn authority")
        try:
            subprocess.Popen(
                [self.chrome_executable, "--new-window", attempt.tagged_project_url],
                close_fds=True,
            )
        except BaseException as exc:
            return ChildSpawnExecution(False, False, f"Chrome launch failed before observation: {type(exc).__name__}")
        deadline = time.time() + self.open_timeout_s
        while time.time() < deadline:
            observed = self.observe_owned_project(attempt)
            if observed.window_handle and observed.browser_pid:
                observed.changed = True
                observed.side_effect_possible = True
                observed.detail = "single LWS-owned project window opened"
                return observed
            if "observed 0" not in observed.detail:
                return ChildSpawnExecution(False, True, observed.detail)
            time.sleep(0.1)
        return ChildSpawnExecution(
            False,
            True,
            "Chrome launch returned but the unique tagged project window was not observed in time",
        )

    def _navigate_reused_window(
        self,
        attempt: ChildSpawnAttempt,
        *,
        window_handle: int,
        browser_pid: int,
        chrome_executable: str,
        source_url: str,
    ) -> ChildSpawnExecution:
        if not self.enabled:
            raise ChildSpawnBlocked(["child-spawn browser mutation transport is gated"])
        if os.name != "nt":
            return ChildSpawnExecution(False, False, "Windows UI Automation is unavailable")
        env = os.environ.copy()
        env.update(
            {
                "LWS_EXPECTED_HWND": str(window_handle),
                "LWS_EXPECTED_BROWSER_PID": str(browser_pid),
                "LWS_EXPECTED_CHROME_EXE": chrome_executable,
                "LWS_EXPECTED_SOURCE_URL": source_url,
                "LWS_TARGET_URL": attempt.tagged_project_url,
            }
        )
        try:
            result = _run_ps(
                _NAVIGATE_EXACT_WINDOW_SCRIPT,
                powershell=self.powershell,
                timeout_s=self.timeout_s,
                env=env,
            )
        except BaseException as exc:
            return ChildSpawnExecution(
                False,
                False,
                f"dispatcher navigation failed before observation: {type(exc).__name__}",
            )
        if bool(result.get("ambiguous")):
            return ChildSpawnExecution(
                False,
                True,
                str(result.get("detail") or "dispatcher navigation outcome ambiguous"),
                window_handle=window_handle,
                browser_pid=browser_pid,
                url=source_url,
            )
        if not bool(result.get("changed")):
            return ChildSpawnExecution(
                False,
                False,
                str(result.get("detail") or "dispatcher navigation was not submitted"),
                window_handle=window_handle,
                browser_pid=browser_pid,
                url=source_url,
            )
        deadline = time.time() + self.open_timeout_s
        probe = ChildSpawnAttempt(
            **{
                **asdict(attempt),
                "window_handle": int(window_handle),
                "browser_pid": int(browser_pid),
            }
        )
        last = ChildSpawnExecution(False, True, "dispatcher navigation not observed")
        while time.time() < deadline:
            last = self._observe_bound_url(probe)
            if last.url and owned_project_root_matches(last.url, attempt):
                return ChildSpawnExecution(
                    True,
                    True,
                    "exact dispatcher HWND navigated to the new LWS-owned project root",
                    window_handle=window_handle,
                    browser_pid=browser_pid,
                    url=last.url,
                )
            if last.url:
                route_project_id = web_route_project_id(last.url)
                if route_project_id is not None and route_project_id != attempt.project_id:
                    return ChildSpawnExecution(
                        False,
                        True,
                        "dispatcher navigated into a different web project",
                        window_handle=window_handle,
                        browser_pid=browser_pid,
                        url=last.url,
                    )
            time.sleep(0.1)
        return ChildSpawnExecution(
            False,
            True,
            last.detail or "dispatcher navigation not observed in time",
            window_handle=window_handle,
            browser_pid=browser_pid,
            url=last.url or source_url,
        )

    def reuse_authorized(
        self,
        attempt: ChildSpawnAttempt,
        *,
        source_worker: WorkerRecord,
        source_binding: WorkerWindowBinding,
    ) -> ChildSpawnExecution:
        if attempt.state != ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED:
            return ChildSpawnExecution(False, False, "WINDOW_OPEN authority was not durably submitted")
        if source_binding.worker_id != source_worker.worker_id:
            return ChildSpawnExecution(False, False, "dispatcher source binding belongs to a different worker")
        probe = ChildSpawnAttempt(
            **{
                **asdict(attempt),
                "window_handle": source_binding.window_handle,
                "browser_pid": source_binding.browser_pid,
            }
        )
        current = self._observe_bound_url(probe)
        if not current.url or not conversation_url_matches(
            current.url, source_worker.conversation_url
        ):
            return ChildSpawnExecution(
                False,
                False,
                "dispatcher source conversation identity changed",
                window_handle=source_binding.window_handle,
                browser_pid=source_binding.browser_pid,
                url=current.url,
            )
        return self._navigate_reused_window(
            attempt,
            window_handle=source_binding.window_handle,
            browser_pid=source_binding.browser_pid,
            chrome_executable=source_binding.chrome_executable,
            source_url=current.url,
        )

    def reuse_completed_spawn_authorized(
        self,
        attempt: ChildSpawnAttempt,
        *,
        source_attempt: ChildSpawnAttempt,
    ) -> ChildSpawnExecution:
        if source_attempt.state != ChildSpawnAttemptState.COMPLETED:
            return ChildSpawnExecution(False, False, "dispatcher source spawn is not COMPLETED")
        if not (
            source_attempt.window_handle
            and source_attempt.browser_pid
            and source_attempt.conversation_url
        ):
            return ChildSpawnExecution(False, False, "dispatcher source spawn lacks exact page identity")
        probe = ChildSpawnAttempt(
            **{
                **asdict(attempt),
                "window_handle": source_attempt.window_handle,
                "browser_pid": source_attempt.browser_pid,
            }
        )
        current = self._observe_bound_url(probe)
        if not current.url or not conversation_url_matches(
            current.url, source_attempt.conversation_url
        ):
            return ChildSpawnExecution(
                False,
                False,
                "completed dispatcher source conversation identity changed",
                window_handle=source_attempt.window_handle,
                browser_pid=source_attempt.browser_pid,
                url=current.url,
            )
        return self._navigate_reused_window(
            attempt,
            window_handle=int(source_attempt.window_handle),
            browser_pid=int(source_attempt.browser_pid),
            chrome_executable=source_attempt.chrome_executable,
            source_url=current.url,
        )

    def _close_authorized_identity(
        self,
        *,
        window_handle: int,
        browser_pid: int,
        chrome_executable: str,
        expected_urls: tuple[str, ...],
    ) -> ChildSpawnExecution:
        if not self.enabled:
            raise ChildSpawnBlocked(["child-spawn browser mutation transport is gated"])
        if os.name != "nt":
            return ChildSpawnExecution(False, False, "Windows UI Automation is unavailable")
        if window_handle <= 0 or browser_pid <= 0 or not expected_urls:
            return ChildSpawnExecution(False, False, "exact child window close identity is incomplete")
        env = os.environ.copy()
        env.update(
            {
                "LWS_EXPECTED_HWND": str(window_handle),
                "LWS_EXPECTED_BROWSER_PID": str(browser_pid),
                "LWS_EXPECTED_CHROME_EXE": chrome_executable,
                "LWS_EXPECTED_URL_1": expected_urls[0],
            }
        )
        if len(expected_urls) > 1:
            env["LWS_EXPECTED_URL_2"] = expected_urls[1]
        try:
            result = _run_ps(
                _CLOSE_OWNED_PROJECT_SCRIPT,
                powershell=self.powershell,
                timeout_s=self.timeout_s,
                env=env,
            )
        except BaseException as exc:
            return ChildSpawnExecution(
                False,
                False,
                f"exact child window close failed before observation: {type(exc).__name__}",
            )
        if bool(result.get("ambiguous")):
            return ChildSpawnExecution(
                False,
                True,
                str(result.get("detail") or "exact child window close identity is ambiguous"),
                window_handle=window_handle,
                browser_pid=browser_pid,
            )
        if bool(result.get("absent")):
            return ChildSpawnExecution(
                False,
                False,
                str(result.get("detail") or "exact child window already absent"),
                window_handle=window_handle,
                browser_pid=browser_pid,
            )
        return ChildSpawnExecution(
            bool(result.get("closed")),
            bool(result.get("closed")),
            str(result.get("detail") or "exact LWS child window close completed"),
            window_handle=window_handle,
            browser_pid=browser_pid,
        )

    def close_owned_project(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        if attempt.state != ChildSpawnAttemptState.WINDOW_BOUND:
            return ChildSpawnExecution(False, False, "pre-send close requires WINDOW_BOUND state")
        if not attempt.window_handle or not attempt.browser_pid:
            return ChildSpawnExecution(False, False, "pre-send close requires exact HWND/PID binding")
        return self._close_authorized_identity(
            window_handle=int(attempt.window_handle),
            browser_pid=int(attempt.browser_pid),
            chrome_executable=attempt.chrome_executable,
            expected_urls=_owned_project_url_candidates(attempt),
        )

    def close_worker_binding_authorized(
        self,
        *,
        worker: WorkerRecord,
        binding: WorkerWindowBinding,
    ) -> ChildSpawnExecution:
        if binding.worker_id != worker.worker_id:
            return ChildSpawnExecution(False, False, "window binding belongs to a different worker")
        urls = tuple(
            dict.fromkeys(
                (
                    *conversation_url_candidates(binding.conversation_url),
                    *conversation_url_candidates(worker.conversation_url),
                )
            )
        )
        return self._close_authorized_identity(
            window_handle=binding.window_handle,
            browser_pid=binding.browser_pid,
            chrome_executable=binding.chrome_executable,
            expected_urls=urls,
        )

    def close_completed_spawn_authorized(
        self,
        *,
        source_attempt: ChildSpawnAttempt,
    ) -> ChildSpawnExecution:
        if source_attempt.state != ChildSpawnAttemptState.COMPLETED:
            return ChildSpawnExecution(
                False, False, "terminal close source spawn is not COMPLETED"
            )
        if (
            source_attempt.metadata.get("window_recycled_to")
            or source_attempt.metadata.get("window_closed_at")
        ):
            return ChildSpawnExecution(
                False, False, "terminal close source spawn was already consumed"
            )
        if not (
            source_attempt.window_handle
            and source_attempt.browser_pid
            and source_attempt.conversation_url
        ):
            return ChildSpawnExecution(
                False, False, "completed spawn lacks exact terminal page identity"
            )
        return self._close_authorized_identity(
            window_handle=int(source_attempt.window_handle),
            browser_pid=int(source_attempt.browser_pid),
            chrome_executable=source_attempt.chrome_executable,
            expected_urls=conversation_url_candidates(source_attempt.conversation_url),
        )

    def _observe_bound_url(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        if not attempt.window_handle or not attempt.browser_pid:
            return ChildSpawnExecution(False, False, "spawn attempt has no durable HWND/PID binding")
        env = os.environ.copy()
        env.update(
            {
                "LWS_EXPECTED_HWND": str(attempt.window_handle),
                "LWS_EXPECTED_BROWSER_PID": str(attempt.browser_pid),
                "LWS_EXPECTED_CHROME_EXE": attempt.chrome_executable,
            }
        )
        result = _run_ps(
            _WINDOW_URL_SCRIPT,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )
        if bool(result.get("ambiguous")):
            return ChildSpawnExecution(False, False, str(result.get("detail") or "spawn window identity ambiguous"))
        if not bool(result.get("present")):
            return ChildSpawnExecution(False, False, str(result.get("detail") or "spawn window absent"))
        return ChildSpawnExecution(
            False,
            False,
            str(result.get("detail") or "spawn window observed"),
            window_handle=int(result.get("window_handle") or 0),
            browser_pid=int(result.get("browser_pid") or 0),
            url=str(result.get("url") or ""),
        )

    def send_authorized(self, attempt: ChildSpawnAttempt, prompt: str) -> ChildSpawnExecution:
        if not self.enabled:
            raise ChildSpawnBlocked(["child-spawn browser mutation transport is gated"])
        if os.name != "nt":
            return ChildSpawnExecution(False, False, "Windows UI Automation is unavailable")
        if attempt.state != ChildSpawnAttemptState.PROMPT_SUBMITTED:
            return ChildSpawnExecution(False, False, "PROMPT_SUBMITTED authority was not durably recorded")
        if not attempt.window_handle or not attempt.browser_pid:
            return ChildSpawnExecution(False, False, "child-spawn has no exact owned window binding")
        try:
            current = self._observe_bound_url(attempt)
        except BaseException as exc:
            return ChildSpawnExecution(
                False,
                False,
                f"pre-send owned-window observation failed: {type(exc).__name__}",
            )
        if not current.url or not owned_project_root_matches(current.url, attempt):
            return ChildSpawnExecution(
                False,
                False,
                "bound HWND is no longer the same LWS-owned project root",
                window_handle=attempt.window_handle,
                browser_pid=attempt.browser_pid,
                url=current.url,
            )
        env = os.environ.copy()
        # The UIA sender still requires literal exact-URL equality. We merely derive
        # that exact value from the same bound HWND after project-id + owner-token checks.
        env["LWS_EXPECTED_URL"] = current.url
        env["LWS_EXPECTED_HWND"] = str(attempt.window_handle)
        env["LWS_EXPECTED_BROWSER_PID"] = str(attempt.browser_pid)
        env["LWS_EXPECTED_CHROME_EXE"] = attempt.chrome_executable
        env["LWS_PROMPT_B64"] = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        result = _run_powershell_json(
            _POWERSHELL_ACTION,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )
        return ChildSpawnExecution(
            bool(result.get("submitted")),
            bool(result.get("side_effect_possible")),
            str(result.get("detail") or result.get("phase") or "child-spawn UIA send completed"),
            window_handle=attempt.window_handle,
            browser_pid=attempt.browser_pid,
            url=current.url,
            rate_limited=bool(result.get("rate_limited")),
            modal_dismissed=bool(result.get("modal_dismissed")),
        )

    def observe_bound_delivery(
        self,
        attempt: ChildSpawnAttempt,
        prompt: str = "",
    ) -> ChildSpawnDeliveryObservation:
        if not attempt.window_handle or not attempt.browser_pid:
            return ChildSpawnDeliveryObservation(
                "", False, "", False, False, "spawn attempt has no exact HWND/PID binding"
            )
        env = os.environ.copy()
        env.update(
            {
                "LWS_EXPECTED_HWND": str(attempt.window_handle),
                "LWS_EXPECTED_BROWSER_PID": str(attempt.browser_pid),
                "LWS_EXPECTED_CHROME_EXE": attempt.chrome_executable,
            }
        )
        try:
            result = _run_ps(
                _DELIVERY_OBSERVE_SCRIPT,
                powershell=self.powershell,
                timeout_s=self.timeout_s,
                env=env,
            )
        except BaseException as exc:
            return ChildSpawnDeliveryObservation(
                "",
                False,
                "",
                False,
                False,
                f"delivery observation failed: {type(exc).__name__}",
            )
        if bool(result.get("ambiguous")) or not bool(result.get("present")):
            return ChildSpawnDeliveryObservation(
                str(result.get("url") or ""),
                False,
                "",
                False,
                False,
                str(result.get("detail") or "delivery window identity unavailable"),
            )
        value = str(result.get("composer_value") or "")
        return ChildSpawnDeliveryObservation(
            str(result.get("url") or ""),
            bool(result.get("generating")),
            value,
            bool(result.get("send_ready")),
            _prompt_equivalent(value, prompt) if prompt else False,
            str(result.get("detail") or "delivery state observed"),
        )

    def wait_for_delivery(
        self,
        attempt: ChildSpawnAttempt,
        prompt: str,
    ) -> ChildSpawnDeliveryObservation:
        deadline = time.time() + self.conversation_timeout_s
        last = ChildSpawnDeliveryObservation(
            "", False, "", False, False, "delivery state not observed"
        )
        while time.time() < deadline:
            last = self.observe_bound_delivery(attempt, prompt)
            if last.url and web_conversation_project_id(last.url) == attempt.project_id:
                if last.delivered or (last.prompt_matches_draft and last.send_button_ready):
                    return last
            elif last.url:
                route_project_id = web_route_project_id(last.url)
                if route_project_id is not None and route_project_id != attempt.project_id:
                    return ChildSpawnDeliveryObservation(
                        last.url,
                        False,
                        last.composer_value,
                        last.send_button_ready,
                        last.prompt_matches_draft,
                        "owned HWND navigated into a different web project during delivery",
                    )
            time.sleep(0.1)
        return last

    def invoke_existing_draft_authorized(
        self,
        attempt: ChildSpawnAttempt,
        *,
        prompt: str,
        conversation_url: str,
    ) -> ChildSpawnExecution:
        if not self.enabled:
            raise ChildSpawnBlocked(["child-spawn browser mutation transport is gated"])
        if not attempt.window_handle or not attempt.browser_pid:
            return ChildSpawnExecution(False, False, "second-stage Send lacks exact HWND/PID")
        if web_conversation_project_id(conversation_url) != attempt.project_id:
            return ChildSpawnExecution(False, False, "second-stage Send is outside the expected project")
        env = os.environ.copy()
        env.update(
            {
                "LWS_EXPECTED_HWND": str(attempt.window_handle),
                "LWS_EXPECTED_BROWSER_PID": str(attempt.browser_pid),
                "LWS_EXPECTED_CHROME_EXE": attempt.chrome_executable,
                "LWS_EXPECTED_URL": conversation_url,
                "LWS_PROMPT_B64": base64.b64encode(prompt.encode("utf-8")).decode("ascii"),
            }
        )
        try:
            result = _run_ps(
                _INVOKE_EXISTING_DRAFT_SCRIPT,
                powershell=self.powershell,
                timeout_s=self.timeout_s,
                env=env,
            )
        except BaseException as exc:
            return ChildSpawnExecution(
                False,
                False,
                f"second-stage exact-draft Send failed before observation: {type(exc).__name__}",
            )
        return ChildSpawnExecution(
            bool(result.get("submitted")),
            bool(result.get("side_effect_possible")),
            str(result.get("detail") or "second-stage exact-draft Send completed"),
            window_handle=attempt.window_handle,
            browser_pid=attempt.browser_pid,
            url=conversation_url,
        )

    def wait_for_conversation(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        deadline = time.time() + self.conversation_timeout_s
        last = ChildSpawnExecution(False, False, "conversation transition not observed")
        while time.time() < deadline:
            try:
                last = self._observe_bound_url(attempt)
            except BaseException as exc:
                return ChildSpawnExecution(False, False, f"bound spawn observation failed: {type(exc).__name__}")
            if last.url and web_conversation_project_id(last.url) == attempt.project_id:
                last.changed = True
                last.detail = "same LWS-owned HWND transitioned to an expected-project conversation"
                return last
            if last.url:
                try:
                    parsed = urlsplit(last.url)
                except ValueError:
                    return ChildSpawnExecution(
                        False, False, "owned HWND exposed an invalid URL during conversation routing", url=last.url
                    )
                if parsed.scheme.lower() != "https" or parsed.hostname != "chatgpt.com":
                    return ChildSpawnExecution(
                        False, False, "owned HWND navigated away from https://chatgpt.com", url=last.url
                    )
                route_project_id = web_route_project_id(last.url)
                if route_project_id is not None and route_project_id != attempt.project_id:
                    return ChildSpawnExecution(
                        False,
                        False,
                        "owned HWND navigated into a different web project",
                        url=last.url,
                    )
                # web chat may briefly route through generic/root/project URLs between
                # composer submission and the final /c/... URL. These are read-only
                # observations on the same fenced HWND/PID. Never accept them as success,
                # but also do not turn them into a false failure that invites replay.
            time.sleep(0.1)
        return last


def execute_child_spawn_open(
    registry: Registry,
    *,
    attempt_id: str,
    transport: ChromeUiaChildSpawnTransport,
    source_worker: WorkerRecord | None = None,
    source_binding: WorkerWindowBinding | None = None,
    source_spawn: ChildSpawnAttempt | None = None,
    now: float | None = None,
) -> ChildSpawnAttempt:
    if source_worker is not None and source_binding is None:
        raise ChildSpawnBlocked(["reused worker requires its exact window binding"])
    if source_binding is not None and source_worker is None:
        raise ChildSpawnBlocked(["reused window binding requires its worker identity"])
    if source_worker is not None and source_spawn is not None:
        raise ChildSpawnBlocked(
            ["choose either a current worker source or a completed-spawn source"]
        )
    submitted = submit_child_spawn_open(registry, attempt_id, now=now)
    if source_worker is not None:
        result = transport.reuse_authorized(
            submitted,
            source_worker=source_worker,
            source_binding=source_binding,
        )
    elif source_spawn is not None:
        result = transport.reuse_completed_spawn_authorized(
            submitted,
            source_attempt=source_spawn,
        )
    else:
        result = transport.open_authorized(submitted)
    ts = time.time() if now is None else float(now)
    if result.window_handle and result.browser_pid and result.url:
        bound = bind_child_spawn_window(
            registry,
            attempt_id=attempt_id,
            window_handle=result.window_handle,
            browser_pid=result.browser_pid,
            observed_url=result.url,
            now=ts,
        )
        if source_spawn is not None:
            try:
                registry.annotate_child_spawn_attempt(
                    source_spawn.attempt_id,
                    metadata_updates={"window_recycled_to": attempt_id},
                    now=ts,
                )
            except KeyError:
                # Tests and library callers may supply a detached completed source record.
                # Real CLI/batch reuse resolves the source from this registry and is marked.
                pass
        if source_worker is not None:
            for source_attempt in registry.child_spawn_attempts(
                source_worker.task_id, limit=50
            ):
                if (
                    source_attempt.state == ChildSpawnAttemptState.COMPLETED
                    and source_attempt.worker_id == source_worker.worker_id
                    and source_attempt.window_handle == source_binding.window_handle
                    and source_attempt.browser_pid == source_binding.browser_pid
                    and not source_attempt.metadata.get("window_recycled_to")
                    and not source_attempt.metadata.get("window_closed_at")
                ):
                    registry.annotate_child_spawn_attempt(
                        source_attempt.attempt_id,
                        metadata_updates={"window_recycled_to": attempt_id},
                        now=ts,
                    )
                    break
            registry.clear_worker_window_binding(source_worker.worker_id)
        return bound
    if result.side_effect_possible:
        return registry.update_child_spawn_attempt(
            attempt_id,
            state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
            last_error=result.detail,
            now=ts,
        )
    return registry.update_child_spawn_attempt(
        attempt_id,
        state=ChildSpawnAttemptState.FAILED,
        last_error=result.detail,
        now=ts,
    )


def execute_child_spawn_prompt(
    registry: Registry,
    *,
    attempt_id: str,
    transport: ChromeUiaChildSpawnTransport,
    lease_seconds: float = 7200.0,
    rate_limit_cooldown_s: float = _DEFAULT_RATE_LIMIT_COOLDOWN_S,
    now: float | None = None,
) -> ChildSpawnAttempt:
    ts = time.time() if now is None else float(now)
    attempt = registry.get_child_spawn_attempt(attempt_id)
    cooldown = registry.get_runtime_cooldown(_WEB_CHILD_DISPATCH_COOLDOWN)
    if cooldown is not None and float(cooldown["until_at"]) > ts:
        remaining = max(0.0, float(cooldown["until_at"]) - ts)
        return registry.update_child_spawn_attempt(
            attempt_id,
            state=attempt.state,
            last_error=f"global web child-dispatch cooldown active for {remaining:.1f}s: {cooldown['reason']}",
            metadata_updates={"cooldown_until": float(cooldown["until_at"])},
            now=ts,
        )
    submitted, prompt = submit_child_spawn_prompt(registry, attempt_id, now=ts)
    result = transport.send_authorized(submitted, prompt)
    if result.rate_limited:
        cooldown_s = max(5.0, min(float(rate_limit_cooldown_s), 900.0))
        until_at = ts + cooldown_s
        registry.set_runtime_cooldown(
            _WEB_CHILD_DISPATCH_COOLDOWN,
            until_at=until_at,
            reason="ChatGPT Too many requests modal",
            metadata={"modal_dismissed": bool(result.modal_dismissed)},
            now=ts,
        )
        detail = (
            f"rate-limit modal {'dismissed' if result.modal_dismissed else 'observed'}; "
            f"global child dispatch cooldown for {cooldown_s:.0f}s"
        )
        return registry.update_child_spawn_attempt(
            attempt_id,
            state=ChildSpawnAttemptState.WINDOW_BOUND,
            last_error=detail,
            metadata_updates={
                "cooldown_until": until_at,
                "rate_limit_modal_dismissed": bool(result.modal_dismissed),
            },
            now=ts,
        )
    if not result.submitted:
        if result.side_effect_possible:
            return registry.update_child_spawn_attempt(
                attempt_id,
                state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
                last_error=result.detail,
                now=ts,
            )
        return registry.update_child_spawn_attempt(
            attempt_id,
            state=ChildSpawnAttemptState.WINDOW_BOUND,
            last_error=result.detail,
            now=ts,
        )
    transition = transport.wait_for_conversation(submitted)
    if not (
        transition.url
        and web_conversation_project_id(transition.url) == submitted.project_id
    ):
        return registry.update_child_spawn_attempt(
            attempt_id,
            state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
            last_error=transition.detail,
            now=ts,
        )

    # Keep library compatibility with pre-delivery-gate test/custom transports. The
    # production Chrome transport implements wait_for_delivery and therefore never uses
    # this route-only compatibility branch.
    wait_for_delivery = getattr(transport, "wait_for_delivery", None)
    if wait_for_delivery is None:
        return _complete_from_bound_conversation(
            registry,
            attempt=submitted,
            conversation_url=transition.url,
            now=ts,
            lease_seconds=lease_seconds,
        )

    delivery = wait_for_delivery(submitted, prompt)
    if (
        delivery.url
        and web_conversation_project_id(delivery.url) == submitted.project_id
        and delivery.delivered
    ):
        return _complete_from_bound_conversation(
            registry,
            attempt=submitted,
            conversation_url=delivery.url,
            now=ts,
            lease_seconds=lease_seconds,
        )

    # ChatGPT may use the first Send only to create the /c/... route, leaving the exact
    # persisted draft untouched and still send-ready. This is not delivery. Permit at most
    # one second-stage invocation, and persist that authority before the external click so
    # a crash can never trigger a third automatic replay.
    if (
        delivery.url
        and web_conversation_project_id(delivery.url) == submitted.project_id
        and delivery.prompt_matches_draft
        and delivery.send_button_ready
        and not submitted.metadata.get("delivery_second_send_used")
    ):
        submitted = registry.update_child_spawn_attempt(
            attempt_id,
            state=ChildSpawnAttemptState.PROMPT_SUBMITTED,
            metadata_updates={
                "delivery_second_send_used": True,
                "delivery_second_send_url": delivery.url,
            },
            now=ts,
        )
        second = transport.invoke_existing_draft_authorized(
            submitted,
            prompt=prompt,
            conversation_url=delivery.url,
        )
        if not second.submitted:
            return registry.update_child_spawn_attempt(
                attempt_id,
                state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
                last_error=second.detail,
                now=ts,
            )
        delivery = transport.wait_for_delivery(submitted, prompt)
        if (
            delivery.url
            and web_conversation_project_id(delivery.url) == submitted.project_id
            and delivery.delivered
        ):
            return _complete_from_bound_conversation(
                registry,
                attempt=submitted,
                conversation_url=delivery.url,
                now=ts,
                lease_seconds=lease_seconds,
            )
    return registry.update_child_spawn_attempt(
        attempt_id,
        state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
        last_error=(
            delivery.detail
            or "conversation route exists but prompt delivery was not positively observed"
        ),
        now=ts,
    )


def reconcile_child_spawn(
    registry: Registry,
    *,
    attempt_id: str,
    transport: ChromeUiaChildSpawnTransport,
    lease_seconds: float = 7200.0,
    now: float | None = None,
) -> ChildSpawnAttempt:
    attempt = registry.get_child_spawn_attempt(attempt_id)
    ts = time.time() if now is None else float(now)
    if attempt.state == ChildSpawnAttemptState.COMPLETED:
        return attempt
    if attempt.state == ChildSpawnAttemptState.WINDOW_OPEN_SUBMITTED or (
        attempt.state == ChildSpawnAttemptState.RECONCILE_REQUIRED and not attempt.window_handle
    ):
        observed = transport.observe_owned_project(attempt)
        if observed.window_handle and observed.browser_pid and observed.url:
            return bind_child_spawn_window(
                registry,
                attempt_id=attempt_id,
                window_handle=observed.window_handle,
                browser_pid=observed.browser_pid,
                observed_url=observed.url,
                now=ts,
            )
        if attempt.state != ChildSpawnAttemptState.RECONCILE_REQUIRED:
            return registry.update_child_spawn_attempt(
                attempt_id,
                state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
                last_error=observed.detail,
                now=ts,
            )
        return attempt
    if attempt.window_handle and attempt.browser_pid:
        observed = transport._observe_bound_url(attempt)
        if observed.url and web_conversation_project_id(observed.url) == attempt.project_id:
            return _complete_from_bound_conversation(
                registry,
                attempt=attempt,
                conversation_url=observed.url,
                now=ts,
                lease_seconds=lease_seconds,
            )
        if observed.url and owned_project_root_matches(observed.url, attempt):
            # A project-root observation proves ownership/window identity, but if a prior
            # prompt side effect was ambiguous it does not prove that retrying Send is safe.
            return attempt
    return registry.update_child_spawn_attempt(
        attempt_id,
        state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
        last_error="child-spawn outcome remains ambiguous; do not open/send again",
        now=ts,
    )


def child_spawn_payload(attempt: ChildSpawnAttempt) -> dict:
    payload = asdict(attempt)
    payload["state"] = attempt.state.value
    return payload
