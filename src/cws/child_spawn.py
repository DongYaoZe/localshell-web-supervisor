from __future__ import annotations

import base64
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit, urlunsplit

from .models import ChildSpawnAttempt, ChildSpawnAttemptState, WorkspaceObservation
from .page_runtime import _FIND_SCRIPT, _run_ps, ChromeUiaProbeWindowTransport
from .registry import Registry
from .uia_actions import _POWERSHELL_ACTION, _run_powershell_json


_PROJECT_TOKEN = re.compile(r"^(g-p-([0-9a-fA-F]{32}))(?:-[^/]+)?$")
_CONVERSATION_SEGMENT = re.compile(r"^[0-9a-fA-F-]{8,}$")


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

    @property
    def submitted(self) -> bool:
        return self.changed


_WINDOW_URL_SCRIPT = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
function Get-Value($e){try{$p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern);if($p){return [string]$p.Current.Value}}catch{};return $null}
$hwnd=[int64]$env:CWS_EXPECTED_HWND; $pidExpected=[int]$env:CWS_EXPECTED_BROWSER_PID
$expectedExe=[string]$env:CWS_EXPECTED_CHROME_EXE
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


def _project_parts(url: str) -> tuple[str, bool]:
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError as exc:
        raise ValueError("invalid ChatGPT project URL") from exc
    if parsed.scheme.lower() != "https" or parsed.hostname != "chatgpt.com":
        raise ValueError("ChatGPT project URL must use https://chatgpt.com")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("ChatGPT project URL must not contain credentials or a custom port")
    if parsed.query or parsed.fragment:
        raise ValueError("ChatGPT project URL must not contain query parameters or a fragment")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) not in {2, 3} or segments[0] != "g":
        raise ValueError("ChatGPT project URL must point to one project root")
    match = _PROJECT_TOKEN.fullmatch(segments[1])
    if match is None:
        raise ValueError("ChatGPT project URL does not contain a supported g-p project id")
    if len(segments) == 3 and segments[2] != "project":
        raise ValueError("ChatGPT project URL must not point to an existing conversation")
    return match.group(2).lower(), len(segments) == 3


def chatgpt_project_id(url: str) -> str:
    project_id, _ = _project_parts(url)
    return project_id


def chatgpt_conversation_project_id(url: str) -> str | None:
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


def chatgpt_route_project_id(url: str) -> str | None:
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
    chatgpt_project_id(project_url)
    owner = str(owner_token).strip()
    if not owner or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", owner):
        raise ValueError("child-spawn owner token is invalid")
    parsed = urlsplit(project_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", f"cws-child={owner}"))


def owned_project_root_matches(url: str, attempt: ChildSpawnAttempt) -> bool:
    """Accept ChatGPT's root canonicalization only when the CWS owner tag is unchanged."""

    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return False
    if parsed.query or parsed.fragment != f"cws-child={attempt.owner_token}":
        return False
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    try:
        return chatgpt_project_id(base) == attempt.project_id
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
    project_url = dispatch.chatgpt_project_url
    if not project_url:
        blockers.append("child dispatch has no explicit ChatGPT project URL")
        project_id = ""
    else:
        try:
            project_id = chatgpt_project_id(project_url)
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
    if observed_url.rstrip("/") != attempt.tagged_project_url.rstrip("/"):
        raise ChildSpawnBlocked(["observed project window URL does not match the CWS ownership tag"])
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
    observed_project = chatgpt_conversation_project_id(conversation_url)
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


class ChromeUiaChildSpawnTransport:
    """Explicitly gated normal-Chrome transport for one CWS-owned project-root window."""

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
        env["CWS_EXPECTED_ACTUAL_URL"] = url
        env["CWS_EXPECTED_CHROME_EXE"] = self.chrome_executable
        found = _run_ps(
            _FIND_SCRIPT,
            powershell=self.powershell,
            timeout_s=self.timeout_s,
            env=env,
        )
        return ChromeUiaProbeWindowTransport._matches(found)

    def observe_owned_project(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        try:
            matches = self._find(attempt.tagged_project_url)
        except BaseException as exc:
            return ChildSpawnExecution(False, False, f"owned project observation failed: {type(exc).__name__}")
        if len(matches) != 1:
            return ChildSpawnExecution(
                False,
                False,
                f"expected exactly one CWS-tagged project window, observed {len(matches)}",
            )
        match = matches[0]
        if match.chrome_executable.casefold() != attempt.chrome_executable.casefold():
            return ChildSpawnExecution(False, False, "owned project window executable changed")
        return ChildSpawnExecution(
            False,
            False,
            "single CWS-tagged project window observed",
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
                observed.detail = "single CWS-owned project window opened"
                return observed
            if "observed 0" not in observed.detail:
                return ChildSpawnExecution(False, True, observed.detail)
            time.sleep(0.1)
        return ChildSpawnExecution(
            False,
            True,
            "Chrome launch returned but the unique tagged project window was not observed in time",
        )

    def _observe_bound_url(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        if not attempt.window_handle or not attempt.browser_pid:
            return ChildSpawnExecution(False, False, "spawn attempt has no durable HWND/PID binding")
        env = os.environ.copy()
        env.update(
            {
                "CWS_EXPECTED_HWND": str(attempt.window_handle),
                "CWS_EXPECTED_BROWSER_PID": str(attempt.browser_pid),
                "CWS_EXPECTED_CHROME_EXE": attempt.chrome_executable,
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
                "bound HWND is no longer the same CWS-owned project root",
                window_handle=attempt.window_handle,
                browser_pid=attempt.browser_pid,
                url=current.url,
            )
        env = os.environ.copy()
        # The UIA sender still requires literal exact-URL equality. We merely derive
        # that exact value from the same bound HWND after project-id + owner-token checks.
        env["CWS_EXPECTED_URL"] = current.url
        env["CWS_EXPECTED_HWND"] = str(attempt.window_handle)
        env["CWS_EXPECTED_BROWSER_PID"] = str(attempt.browser_pid)
        env["CWS_EXPECTED_CHROME_EXE"] = attempt.chrome_executable
        env["CWS_PROMPT_B64"] = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
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
        )

    def wait_for_conversation(self, attempt: ChildSpawnAttempt) -> ChildSpawnExecution:
        deadline = time.time() + self.conversation_timeout_s
        last = ChildSpawnExecution(False, False, "conversation transition not observed")
        while time.time() < deadline:
            try:
                last = self._observe_bound_url(attempt)
            except BaseException as exc:
                return ChildSpawnExecution(False, False, f"bound spawn observation failed: {type(exc).__name__}")
            if last.url and chatgpt_conversation_project_id(last.url) == attempt.project_id:
                last.changed = True
                last.detail = "same CWS-owned HWND transitioned to an expected-project conversation"
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
                route_project_id = chatgpt_route_project_id(last.url)
                if route_project_id is not None and route_project_id != attempt.project_id:
                    return ChildSpawnExecution(
                        False,
                        False,
                        "owned HWND navigated into a different ChatGPT project",
                        url=last.url,
                    )
                # ChatGPT may briefly route through generic/root/project URLs between
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
    now: float | None = None,
) -> ChildSpawnAttempt:
    submitted = submit_child_spawn_open(registry, attempt_id, now=now)
    result = transport.open_authorized(submitted)
    ts = time.time() if now is None else float(now)
    if result.window_handle and result.browser_pid and result.url:
        return bind_child_spawn_window(
            registry,
            attempt_id=attempt_id,
            window_handle=result.window_handle,
            browser_pid=result.browser_pid,
            observed_url=result.url,
            now=ts,
        )
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
    now: float | None = None,
) -> ChildSpawnAttempt:
    submitted, prompt = submit_child_spawn_prompt(registry, attempt_id, now=now)
    result = transport.send_authorized(submitted, prompt)
    ts = time.time() if now is None else float(now)
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
    if transition.url and chatgpt_conversation_project_id(transition.url) == submitted.project_id:
        return _complete_from_bound_conversation(
            registry,
            attempt=submitted,
            conversation_url=transition.url,
            now=ts,
            lease_seconds=lease_seconds,
        )
    return registry.update_child_spawn_attempt(
        attempt_id,
        state=ChildSpawnAttemptState.RECONCILE_REQUIRED,
        last_error=transition.detail,
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
        if observed.url and chatgpt_conversation_project_id(observed.url) == attempt.project_id:
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
