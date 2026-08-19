from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .actions import apply_unresolved_action_gate
from .browser import observation_from_dom_payload, observation_from_lsm_snapshot
from .dispatcher import DispatchPolicy, build_dispatch_plan
from .doctor import DoctorStatus, run_doctor
from .cdp import CdpNetworkProbe, CdpProbeUnavailable
from .lifecycle import PageCloseEvidence, evaluate_page_close_evidence
from .lsm import FileLsmTelemetry, UnsupportedLsmState, detect_lsm_state_dir
from .orchestrator import BrowserPoolPolicy, plan_browser_pool
from .models import SupervisorState, WorkerStatus
from .ram import MemoryProbeUnavailable, observe_system_memory, observe_windows_process_group
from .reconcile import build_reconciliation_record
from .recovery import recommend
from .registry import Registry
from .scheduler import attention_queue
from .uia import ChromeUiaProbe, UiaProbeUnavailable
from .watchdog_host import (
    inspect_watchdog_host,
    launch_detached_watchdog,
    pid_exists,
    request_cooperative_stop,
)
from .watcher import WatchPolicy, assess
from .workspace import WorkspaceProbe


def default_db_path() -> Path:
    value = os.getenv("CWS_DB")
    if value:
        return Path(value)
    return Path.cwd() / ".cws" / "registry.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cws", description="ChatGPT Web task supervisor (safe 0.5 control plane)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--db", default=None, help="registry sqlite path (default: .cws/registry.sqlite3)")
    p.add_argument("--lsm-state-dir", default=None, help="Local Shell MCP durable state directory")
    p.add_argument("--git-bin", default=None, help="git executable used for read-only workspace reconciliation")
    sub = p.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="register a durable task")
    register.add_argument("--task-id")
    register.add_argument("--project", required=True)
    register.add_argument("--objective", required=True)
    register.add_argument("--cwd", required=True)
    register.add_argument("--session-id")
    register.add_argument("--conversation-url")
    register.add_argument("--conversation-id")

    status = sub.add_parser("status", help="show registered task state")
    status.add_argument("--json", action="store_true")

    inspect = sub.add_parser("inspect", help="reconcile one task from durable evidence")
    inspect.add_argument("task_id")
    inspect.add_argument("--json", action="store_true")
    inspect.add_argument(
        "--uia",
        action="store_true",
        help="refresh the current worker from the existing Chrome UI Automation tree first",
    )

    watch = sub.add_parser("watch", help="scan all tasks and print only attention-worthy items")
    watch.add_argument("--json", action="store_true")
    watch.add_argument("--once", action="store_true", help="scan once instead of staying resident")
    watch.add_argument("--interval", type=float, default=30.0, help="seconds between scans")
    watch.add_argument("--browser-suspect-after", type=float, default=120.0)
    watch.add_argument("--network-suspect-after", type=float, default=180.0)
    watch.add_argument("--lsm-suspect-after", type=float, default=180.0)
    watch.add_argument("--hard-stall-after", type=float, default=600.0)
    watch.add_argument("--lease-owner", help=argparse.SUPPRESS)
    watch.add_argument(
        "--uia",
        action="store_true",
        help="refresh matching workers from existing Chrome via read-only UI Automation",
    )

    watchdog_status = sub.add_parser(
        "watchdog-status",
        help="inspect the resident watchdog lease/PID without changing it",
    )
    watchdog_status.add_argument("--json", action="store_true")

    watchdog_start = sub.add_parser(
        "watchdog-start",
        help="launch an independent detached resident watchdog; installs no OS service",
    )
    watchdog_start.add_argument("--interval", type=float, default=30.0)
    watchdog_start.add_argument("--uia", action="store_true")
    watchdog_start.add_argument("--log")
    watchdog_start.add_argument("--ready-timeout", type=float, default=8.0)
    watchdog_start.add_argument("--json", action="store_true")

    watchdog_stop = sub.add_parser(
        "watchdog-stop",
        help="request cooperative lease-based watchdog stop; sends no process-tree kill",
    )
    watchdog_stop.add_argument("--grace", type=float, default=90.0)
    watchdog_stop.add_argument("--wait", type=float, default=35.0)
    watchdog_stop.add_argument("--json", action="store_true")

    worker = sub.add_parser("add-worker", help="attach a new conversation worker lease")
    worker.add_argument("task_id")
    worker.add_argument("--conversation-url", required=True)
    worker.add_argument("--conversation-id")

    worker_status = sub.add_parser(
        "worker-status",
        help="update worker bookkeeping only; does not open or close a browser page",
    )
    worker_status.add_argument("worker_id")
    worker_status.add_argument("status", choices=[status.value for status in WorkerStatus])

    job = sub.add_parser("track-job", help="associate an LSM job id with a task")
    job.add_argument("task_id")
    job.add_argument("job_id")

    checkpoint = sub.add_parser("checkpoint", help="store a durable semantic/workspace checkpoint")
    checkpoint.add_argument("task_id")
    checkpoint_input = checkpoint.add_mutually_exclusive_group(required=True)
    checkpoint_input.add_argument("--json", dest="json_text")
    checkpoint_input.add_argument("--file", dest="json_file")

    history = sub.add_parser("recovery-history", help="show recorded recovery recommendations")
    history.add_argument("task_id")
    history.add_argument("--limit", type=int, default=20)

    reconcile = sub.add_parser(
        "reconcile",
        help="refresh evidence and persist a sanitized recovery fence for one task",
    )
    reconcile.add_argument("task_id")
    reconcile.add_argument("--uia", action="store_true", help="refresh exact-URL Chrome UIA first")
    reconcile.add_argument("--json", action="store_true")

    reconcile_history = sub.add_parser(
        "reconciliation-history",
        help="show durable sanitized reconciliation/fence records",
    )
    reconcile_history.add_argument("task_id")
    reconcile_history.add_argument("--limit", type=int, default=20)

    dom = sub.add_parser("observe-dom", help="ingest a DOM probe JSON object/file")
    dom.add_argument("worker_id")
    group = dom.add_mutually_exclusive_group(required=True)
    group.add_argument("--json", dest="json_text")
    group.add_argument("--file")

    snapshot = sub.add_parser(
        "observe-snapshot",
        help="ingest a Local Shell MCP high-level browser snapshot JSON object/file",
    )
    snapshot.add_argument("worker_id")
    snapshot_group = snapshot.add_mutually_exclusive_group(required=True)
    snapshot_group.add_argument("--json", dest="json_text")
    snapshot_group.add_argument("--file")

    uia = sub.add_parser(
        "probe-uia",
        help="read the current task's existing Chrome tab through Windows UI Automation",
    )
    uia.add_argument("task_id")
    uia.add_argument("--json", action="store_true")
    uia.add_argument("--timeout", type=float, default=8.0)

    cdp = sub.add_parser(
        "probe-cdp",
        help="sample network lifecycle events from an explicitly exposed CDP browser",
    )
    cdp.add_argument("task_id")
    cdp.add_argument("--endpoint", required=True)
    cdp.add_argument("--sample", type=float, default=2.0)
    cdp.add_argument(
        "--allow-remote",
        action="store_true",
        help="explicitly allow a non-loopback CDP endpoint; loopback is the safe default",
    )
    cdp.add_argument("--json", action="store_true")

    pool = sub.add_parser(
        "pool-plan",
        help="compute a read-only low-memory worker/page plan; never closes pages",
    )
    pool.add_argument("--max-active", type=int, default=4)
    pool.add_argument("--min-free-mib", type=int, default=1024)
    pool.add_argument("--high-memory-fraction", type=float, default=0.85)
    pool.add_argument(
        "--page-close-evidence",
        help=(
            "optional local PageCloseEvidence JSON; only a passing generation gate enables "
            "close_allowed advice for already non-live park candidates"
        ),
    )
    pool.add_argument("--json", action="store_true")

    ram = sub.add_parser("ram-status", help="show system and aggregate Chrome working-set telemetry")
    ram.add_argument("--json", action="store_true")

    doctor = sub.add_parser(
        "doctor",
        help="run read-only operational checks; never repairs or changes browser/task state",
    )
    doctor.add_argument("--task", dest="task_id")
    doctor.add_argument(
        "--uia",
        action="store_true",
        help="also read the exact registered Chrome URL through read-only UI Automation",
    )
    doctor.add_argument("--json", action="store_true")

    action_status = sub.add_parser(
        "action-status",
        help="show durable write-ahead action attempts; never sends or acknowledges a message",
    )
    action_status.add_argument("task_id")
    action_status.add_argument("--limit", type=int, default=20)
    action_status.add_argument("--json", action="store_true")

    action_cancel = sub.add_parser(
        "action-cancel",
        help="cancel a local unresolved action lock; does not undo any external side effect",
    )
    action_cancel.add_argument("attempt_id")
    action_cancel.add_argument("--reason", required=True)
    action_cancel.add_argument("--json", action="store_true")

    lifecycle = sub.add_parser(
        "evaluate-page-close",
        help="evaluate JSON evidence for isolated ChatGPT page-close/reopen parking safety",
    )
    lifecycle.add_argument("--file", required=True)
    lifecycle.add_argument(
        "--require-tool",
        action="store_true",
        help="require the stronger live Local Shell tool-execution parking gate",
    )
    lifecycle.add_argument("--json", action="store_true")

    dispatch = sub.add_parser(
        "dispatch-plan",
        help="dry-run a fenced recovery action; 0.5 production dispatch remains disabled",
    )
    dispatch.add_argument("task_id")
    dispatch.add_argument("--uia", action="store_true", help="refresh exact-URL Chrome UIA first")
    dispatch.add_argument("--max-fence-age", type=float, default=120.0)
    dispatch.add_argument("--min-fence-separation", type=float, default=3.0)
    dispatch.add_argument("--min-dom-quiet", type=float, default=5.0)
    dispatch.add_argument("--min-network-quiet", type=float, default=5.0)
    dispatch.add_argument("--json", action="store_true")

    rec = sub.add_parser("recommend", help="print safe recovery recommendation for a task")
    rec.add_argument("task_id")
    rec.add_argument("--uia", action="store_true", help="refresh exact-URL Chrome UIA first")
    rec.add_argument("--json", action="store_true")
    return p


def _adapter(args: argparse.Namespace) -> FileLsmTelemetry | None:
    root = detect_lsm_state_dir(args.lsm_state_dir)
    return FileLsmTelemetry(root) if root else None


def _refresh_lsm(registry: Registry, task_id: str, adapter: FileLsmTelemetry | None):
    task = registry.get_task(task_id)
    if adapter and task.lsm_session_id:
        previous = registry.latest_lsm_observation(task_id)
        try:
            obs = adapter.observe(
                task_id=task_id,
                session_id=task.lsm_session_id,
                tracked_job_ids=registry.tracked_jobs(task_id),
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return previous
        if previous is None or _lsm_semantic_signature(previous) != _lsm_semantic_signature(obs):
            registry.record_lsm_observation(obs)
        return obs
    return registry.latest_lsm_observation(task_id)


def _lsm_semantic_signature(obs) -> str:
    payload = asdict(obs)
    payload.pop("observed_at", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _workspace_semantic_signature(obs) -> str:
    payload = asdict(obs)
    payload.pop("observed_at", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _refresh_uia(registry: Registry, task_id: str, probe: ChromeUiaProbe):
    task = registry.get_task(task_id)
    if not task.current_worker_id:
        raise UiaProbeUnavailable("task has no current conversation worker")
    worker = registry.get_worker(task.current_worker_id)
    previous = registry.latest_browser_observation(worker.worker_id)
    binding = registry.get_worker_window_binding(worker.worker_id, require_fresh=True)
    obs = probe.observe(
        worker,
        previous=previous,
        expected_hwnd=binding.window_handle if binding else None,
    )
    registry.record_browser_observation(obs)
    raw = obs.raw or {}
    window_handle = raw.get("window_handle")
    browser_pid = raw.get("browser_pid")
    if (
        probe.chrome_executable
        and obs.url
        and window_handle is not None
        and browser_pid is not None
    ):
        registry.bind_worker_window(
            worker.worker_id,
            window_handle=int(window_handle),
            browser_pid=int(browser_pid),
            chrome_executable=probe.chrome_executable,
            conversation_url=obs.url,
            source=str(raw.get("source") or "windows_uia_chrome"),
            observed_at=obs.observed_at,
            ttl_s=max(15.0, probe.timeout_s * 4.0),
        )
    return obs


def _refresh_workspace(registry: Registry, task_id: str, probe: WorkspaceProbe):
    task = registry.get_task(task_id)
    previous = registry.latest_workspace_observation(task_id)
    obs = probe.observe(task_id=task_id, cwd=task.cwd)
    if (
        previous is None
        or _workspace_semantic_signature(previous) != _workspace_semantic_signature(obs)
    ):
        registry.record_workspace_observation(obs)
    return obs


def _assessment(
    registry: Registry,
    task_id: str,
    adapter,
    workspace_probe: WorkspaceProbe,
    uia_probe: ChromeUiaProbe | None = None,
    policy: WatchPolicy | None = None,
    *,
    reconcile_workspace: bool = False,
    refresh_uia: bool = False,
):
    task = registry.get_task(task_id)
    lsm = _refresh_lsm(registry, task_id, adapter)
    if refresh_uia and uia_probe is not None:
        try:
            _refresh_uia(registry, task_id, uia_probe)
        except UiaProbeUnavailable:
            pass
    browser = registry.latest_browser_observation(task.current_worker_id)
    network = registry.latest_network_observation(task.current_worker_id)
    result = assess(task, browser, lsm, network=network, policy=policy)
    workspace = None
    if reconcile_workspace or result.requires_reconcile:
        workspace = _refresh_workspace(registry, task_id, workspace_probe)
        result = assess(
            task,
            browser,
            lsm,
            workspace=workspace,
            network=network,
            policy=policy,
        )
    if result.state != task.state and result.confidence in {"high", "medium"}:
        registry.update_state(task_id, result.state)
        task = registry.get_task(task_id)
    return task, browser, lsm, workspace, result


def _record_current_reconciliation(
    registry: Registry,
    task,
    browser,
    lsm,
    workspace,
    result,
):
    network = registry.latest_network_observation(task.current_worker_id)
    worker = registry.get_worker(task.current_worker_id) if task.current_worker_id else None
    record = build_reconciliation_record(
        task,
        result,
        worker=worker,
        browser=browser,
        network=network,
        lsm=lsm,
        workspace=workspace,
    )
    registry.record_reconciliation(record)
    return record


def _scan_attention(
    registry: Registry,
    adapter,
    workspace_probe: WorkspaceProbe,
    policy: WatchPolicy,
    *,
    uia_probe: ChromeUiaProbe | None = None,
    refresh_uia: bool = False,
):
    assessed = []
    for task in registry.list_tasks():
        refreshed, _browser, _lsm, _workspace, result = _assessment(
            registry,
            task.task_id,
            adapter,
            workspace_probe,
            uia_probe,
            policy,
            refresh_uia=refresh_uia,
        )
        assessed.append((refreshed, result))
    return attention_queue(assessed)


def _attention_signature(queue) -> tuple:
    return tuple((item.task_id, item.state.value, item.priority, item.reason) for item in queue)


def _emit_attention(queue, *, as_json: bool) -> None:
    if as_json:
        _print_json([asdict(item) for item in queue])
    elif not queue:
        print("No task currently requires supervisor attention.", flush=True)
    else:
        for item in queue:
            print(
                f"P{item.priority} {item.task_id} {item.state.value}: {item.reason}",
                flush=True,
            )


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _read_json_file(path: str | Path):
    # PowerShell 5.1's `Set-Content -Encoding UTF8` writes a UTF-8 BOM.
    # utf-8-sig accepts both BOM-prefixed and ordinary UTF-8 JSON.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = Registry(args.db or default_db_path())
    adapter = _adapter(args)
    workspace_probe = WorkspaceProbe(git_bin=args.git_bin)
    try:
        if args.command == "register":
            task = registry.register_task(
                task_id=args.task_id,
                project=args.project,
                objective=args.objective,
                cwd=args.cwd,
                lsm_session_id=args.session_id,
                conversation_url=args.conversation_url,
                conversation_id=args.conversation_id,
            )
            print(task.task_id)
            return 0

        if args.command == "add-worker":
            worker = registry.add_worker(
                args.task_id,
                args.conversation_url,
                conversation_id=args.conversation_id,
                make_current=True,
            )
            print(worker.worker_id)
            return 0

        if args.command == "worker-status":
            worker = registry.set_worker_status(args.worker_id, WorkerStatus(args.status))
            print(
                f"{worker.worker_id} status={worker.status.value}; "
                "registry bookkeeping only, no browser page was opened or closed"
            )
            return 0

        if args.command == "track-job":
            registry.track_job(args.task_id, args.job_id)
            print(f"tracked {args.job_id} for {args.task_id}")
            return 0

        if args.command == "checkpoint":
            checkpoint = (
                json.loads(args.json_text)
                if args.json_text is not None
                else _read_json_file(args.json_file)
            )
            if not isinstance(checkpoint, dict):
                raise ValueError("checkpoint JSON must be an object")
            registry.set_checkpoint(args.task_id, checkpoint)
            print("checkpoint recorded")
            return 0

        if args.command == "recovery-history":
            _print_json(registry.recovery_history(args.task_id, args.limit))
            return 0

        if args.command == "reconciliation-history":
            _print_json(
                [asdict(row) for row in registry.reconciliation_history(args.task_id, args.limit)]
            )
            return 0

        if args.command == "observe-dom":
            payload = (
                json.loads(args.json_text)
                if args.json_text is not None
                else _read_json_file(args.file)
            )
            registry.get_worker(args.worker_id)
            previous = registry.latest_browser_observation(args.worker_id)
            registry.record_browser_observation(
                observation_from_dom_payload(args.worker_id, payload, previous=previous)
            )
            print("recorded")
            return 0

        if args.command == "observe-snapshot":
            payload = (
                json.loads(args.json_text)
                if args.json_text is not None
                else _read_json_file(args.file)
            )
            registry.get_worker(args.worker_id)
            previous = registry.latest_browser_observation(args.worker_id)
            registry.record_browser_observation(
                observation_from_lsm_snapshot(args.worker_id, payload, previous=previous)
            )
            print("recorded")
            return 0

        if args.command == "probe-uia":
            obs = _refresh_uia(
                registry,
                args.task_id,
                ChromeUiaProbe(timeout_s=args.timeout),
            )
            if args.json:
                _print_json(asdict(obs))
            else:
                print(
                    f"recorded UIA observation for {args.task_id}: "
                    f"generating={obs.generating} send_ready={obs.send_button_ready} "
                    f"url={obs.url}"
                )
            return 0

        if args.command == "probe-cdp":
            task = registry.get_task(args.task_id)
            if not task.current_worker_id:
                raise CdpProbeUnavailable("task has no current conversation worker")
            worker = registry.get_worker(task.current_worker_id)
            previous = registry.latest_network_observation(worker.worker_id)
            obs = CdpNetworkProbe(
                args.endpoint,
                sample_s=args.sample,
                allow_remote=args.allow_remote,
            ).sample(worker, previous=previous)
            registry.record_network_observation(obs)
            if args.json:
                _print_json(asdict(obs))
            else:
                print(
                    f"recorded CDP sample for {args.task_id}: events={obs.event_count} "
                    f"data_bytes={obs.encoded_data_bytes} failures={obs.loading_failed}"
                )
            return 0

        if args.command == "reconcile":
            task, browser, lsm, workspace, result = _assessment(
                registry,
                args.task_id,
                adapter,
                workspace_probe,
                ChromeUiaProbe() if args.uia else None,
                reconcile_workspace=True,
                refresh_uia=args.uia,
            )
            record = _record_current_reconciliation(
                registry,
                task,
                browser,
                lsm,
                workspace,
                result,
            )
            if args.json:
                _print_json(asdict(record))
            else:
                print(
                    f"{record.reconcile_id} {record.state} [{record.confidence}] "
                    f"fence={record.fence_token[:16]} {record.reason}"
                )
            return 0

        if args.command == "ram-status":
            system_memory = observe_system_memory()
            browser_memory = (
                observe_windows_process_group("chrome") if os.name == "nt" else None
            )
            payload = {
                "system": asdict(system_memory),
                "chrome": asdict(browser_memory) if browser_memory else None,
            }
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"system used={system_memory.used_fraction:.1%} "
                    f"available={system_memory.available_bytes / (1024**3):.2f} GiB"
                )
                if browser_memory:
                    print(
                        f"chrome processes={browser_memory.process_count} "
                        f"working_set={browser_memory.total_working_set_bytes / (1024**3):.2f} GiB"
                    )
            return 0

        if args.command == "watchdog-status":
            host_status = inspect_watchdog_host(registry)
            if args.json:
                _print_json(asdict(host_status))
            else:
                print(
                    f"watchdog lease_present={str(host_status.lease_present).lower()} "
                    f"fresh={str(host_status.fresh).lower()} "
                    f"pid={host_status.pid} alive={host_status.pid_alive}"
                )
                print(host_status.detail)
            return 0

        if args.command == "watchdog-start":
            launch = launch_detached_watchdog(
                registry,
                repo_root=Path.cwd(),
                db_path=registry.db_path,
                interval_s=max(1.0, float(args.interval)),
                use_uia=args.uia,
                lsm_state_dir=args.lsm_state_dir,
                git_bin=args.git_bin,
                log_path=args.log,
                ready_timeout_s=max(1.0, float(args.ready_timeout)),
            )
            if args.json:
                _print_json(asdict(launch))
            else:
                print(
                    f"watchdog spawn_pid={launch.spawn_pid} lease_pid={launch.lease_pid} "
                    f"lease_ready={str(launch.lease_ready).lower()} log={launch.log_path}"
                )
                print(launch.detail)
            return 0 if launch.lease_ready else 10

        if args.command == "watchdog-stop":
            requested = request_cooperative_stop(
                registry,
                grace_s=max(5.0, float(args.grace)),
            )
            if requested is None:
                payload = {"requested": False, "stopped": True, "detail": "no watchdog lease"}
                if args.json:
                    _print_json(payload)
                else:
                    print("no watchdog lease")
                return 0
            pid = int(requested.get("pid") or 0)
            stop_owner = str(requested.get("owner_id") or "")
            deadline = time.time() + max(0.0, float(args.wait))
            while pid and pid_exists(pid) and time.time() < deadline:
                time.sleep(0.1)
            stopped = not pid or not pid_exists(pid)
            cleared = False
            if stopped and stop_owner.startswith("stop:"):
                cleared = registry.clear_watchdog_stop(
                    name="default",
                    stop_owner_id=stop_owner,
                )
            payload = {
                "requested": True,
                "pid": pid or None,
                "stopped": stopped,
                "stop_lease_cleared": cleared,
                "detail": (
                    "watchdog exited cooperatively"
                    if stopped
                    else "watchdog has not exited yet; stop lease remains fresh and blocks replacement"
                ),
            }
            if args.json:
                _print_json(payload)
            else:
                print(payload["detail"])
            return 0 if stopped else 11

        if args.command == "doctor":
            report = run_doctor(
                registry,
                lsm_state_dir=args.lsm_state_dir,
                task_id=args.task_id,
                probe_uia=args.uia,
            )
            if args.json:
                _print_json(asdict(report))
            else:
                print(f"CWS doctor {report.version}: {report.overall.value}")
                for check in report.checks:
                    print(f"{check.status.value:4} {check.name:24} {check.detail}")
            return 1 if report.overall == DoctorStatus.FAIL else 0

        if args.command == "pool-plan":
            try:
                system_memory = observe_system_memory()
            except MemoryProbeUnavailable:
                system_memory = None
            try:
                browser_memory = (
                    observe_windows_process_group("chrome") if os.name == "nt" else None
                )
            except MemoryProbeUnavailable:
                browser_memory = None
            pool_rows = []
            for task in registry.list_tasks():
                worker = (
                    registry.get_worker(task.current_worker_id)
                    if task.current_worker_id
                    else None
                )
                browser = registry.latest_browser_observation(task.current_worker_id)
                lsm = _refresh_lsm(registry, task.task_id, adapter)
                pool_rows.append((task, worker, browser, lsm))
            page_close_capability = False
            if args.page_close_evidence:
                evidence_payload = _read_json_file(args.page_close_evidence)
                if not isinstance(evidence_payload, dict):
                    raise ValueError("page-close evidence JSON must be an object")
                capability = evaluate_page_close_evidence(PageCloseEvidence(**evidence_payload))
                if not capability.generation_parking_safe:
                    raise ValueError(
                        "page-close evidence does not satisfy the generation gate: "
                        + ", ".join(capability.blockers)
                    )
                page_close_capability = True
            policy = BrowserPoolPolicy(
                max_active_workers=max(1, int(args.max_active)),
                min_available_bytes=max(0, int(args.min_free_mib)) * 1024 * 1024,
                high_memory_fraction=min(1.0, max(0.0, float(args.high_memory_fraction))),
                # Capability is explicit deployment evidence, never a release-wide constant.
                # It affects already non-live PARK_CANDIDATE advice only; live/ambiguous work
                # remains pinned by classify_worker_for_pool regardless of this flag.
                page_close_experiment_passed=page_close_capability,
            )
            plan = plan_browser_pool(
                pool_rows,
                system_memory=system_memory,
                browser_memory=browser_memory,
                policy=policy,
            )
            if args.json:
                _print_json(asdict(plan))
            else:
                print(
                    f"memory={plan.memory_pressure} workers={plan.active_worker_count} "
                    f"pinned={plan.pinned_worker_count} "
                    f"park_candidates={plan.park_candidate_count}"
                )
                for item in plan.items:
                    print(
                        f"{item.disposition.value:14} {item.task_id:20} "
                        f"close_allowed={str(item.close_allowed).lower()}  {item.reason}"
                    )
                for note in plan.recommendations:
                    print(f"recommendation: {note}")
                for warning in plan.warnings:
                    print(f"warning: {warning}")
            return 0

        if args.command == "status":
            rows = registry.list_tasks()
            if args.json:
                _print_json([asdict(row) for row in rows])
            elif not rows:
                print("No registered tasks.")
            else:
                for row in rows:
                    print(f"{row.task_id:20} {row.state.value:12} {row.project}  {row.objective}")
            return 0

        if args.command == "inspect":
            task, browser, lsm, workspace, result = _assessment(
                registry,
                args.task_id,
                adapter,
                workspace_probe,
                ChromeUiaProbe() if args.uia else None,
                reconcile_workspace=True,
                refresh_uia=args.uia,
            )
            payload = {
                "task": asdict(task),
                "browser": asdict(browser) if browser else None,
                "network": (
                    asdict(registry.latest_network_observation(task.current_worker_id))
                    if registry.latest_network_observation(task.current_worker_id)
                    else None
                ),
                "lsm": asdict(lsm) if lsm else None,
                "workspace": asdict(workspace) if workspace else None,
                "assessment": asdict(result),
            }
            if args.json:
                _print_json(payload)
            else:
                print(f"{task.task_id}: {result.state.value} [{result.confidence}] - {result.reason}")
                for item in result.evidence:
                    print(f"  - {item}")
            return 0

        if args.command == "action-status":
            unresolved = registry.unresolved_action_attempt(args.task_id)
            history = registry.action_attempts(args.task_id, args.limit)
            payload = {
                "task_id": args.task_id,
                "unresolved": asdict(unresolved) if unresolved else None,
                "history": [asdict(row) for row in history],
            }
            if args.json:
                _print_json(payload)
            else:
                if unresolved is None:
                    print(f"{args.task_id}: no unresolved action attempt")
                else:
                    print(
                        f"{args.task_id}: unresolved {unresolved.attempt_id} "
                        f"state={unresolved.state.value} worker={unresolved.worker_id}"
                    )
                for row in history:
                    print(
                        f"{row.attempt_id:22} {row.state.value:20} "
                        f"worker={row.worker_id} fence={row.fence_token[:12]}"
                    )
            return 0

        if args.command == "action-cancel":
            updated = registry.cancel_action_attempt(args.attempt_id, reason=args.reason)
            if args.json:
                _print_json(asdict(updated))
            else:
                print(
                    f"{updated.attempt_id} state={updated.state.value}; local duplicate-send lock "
                    "released. This does not undo any external side effect."
                )
            return 0

        if args.command == "evaluate-page-close":
            payload = _read_json_file(args.file)
            if not isinstance(payload, dict):
                raise ValueError("page-close evidence JSON must be an object")
            evaluation = evaluate_page_close_evidence(PageCloseEvidence(**payload))
            required_safe = (
                evaluation.tool_execution_parking_safe
                if args.require_tool
                else evaluation.generation_parking_safe
            )
            if args.json:
                output = asdict(evaluation)
                output["required_gate"] = "tool_execution" if args.require_tool else "generation"
                output["required_gate_safe"] = required_safe
                _print_json(output)
            else:
                print(
                    f"generation_parking_safe={str(evaluation.generation_parking_safe).lower()}  "
                    f"tool_execution_parking_safe={str(evaluation.tool_execution_parking_safe).lower()}  "
                    f"required={'tool_execution' if args.require_tool else 'generation'}  "
                    f"{evaluation.conclusion}"
                )
                selected_blockers = (
                    evaluation.tool_blockers if args.require_tool else evaluation.blockers
                )
                for blocker in selected_blockers:
                    print(f"blocker: {blocker}")
            return 0 if required_safe else 8

        if args.command == "dispatch-plan":
            previous = registry.latest_reconciliation(args.task_id)
            task, browser, lsm, workspace, result = _assessment(
                registry,
                args.task_id,
                adapter,
                workspace_probe,
                ChromeUiaProbe() if args.uia else None,
                reconcile_workspace=True,
                refresh_uia=args.uia,
            )
            current = _record_current_reconciliation(
                registry,
                task,
                browser,
                lsm,
                workspace,
                result,
            )
            rec = recommend(task, result, lsm, workspace)
            plan = build_dispatch_plan(
                task,
                rec,
                previous=previous,
                current=current,
                policy=DispatchPolicy(
                    max_reconciliation_age_s=max(1.0, float(args.max_fence_age)),
                    min_reconciliation_separation_s=max(
                        0.0, float(args.min_fence_separation)
                    ),
                    min_dom_quiet_s=max(0.0, float(args.min_dom_quiet)),
                    min_network_quiet_s=max(0.0, float(args.min_network_quiet)),
                    transport_enabled=False,
                ),
            )
            plan = apply_unresolved_action_gate(
                plan,
                registry.unresolved_action_attempt(task.task_id),
            )
            registry.record_recovery_event(
                task.task_id,
                action=f"dispatch_plan:{plan.action.value}",
                safe_to_dispatch=False,
                reason=plan.reason,
                payload={"dispatch_plan": asdict(plan)},
            )
            if args.json:
                _print_json(asdict(plan))
            else:
                print(
                    f"action={plan.action.value} candidate_ready={str(plan.candidate_ready).lower()} "
                    f"would_dispatch={str(plan.would_dispatch).lower()}"
                )
                print(f"reason: {plan.reason}")
                if plan.fence_token:
                    print(f"fence: {plan.fence_token}")
                for blocker in plan.blockers:
                    print(f"blocker: {blocker}")
            return 0

        if args.command == "recommend":
            task, browser, lsm, workspace, result = _assessment(
                registry,
                args.task_id,
                adapter,
                workspace_probe,
                ChromeUiaProbe() if args.uia else None,
                reconcile_workspace=True,
                refresh_uia=args.uia,
            )
            reconciliation = _record_current_reconciliation(
                registry,
                task,
                browser,
                lsm,
                workspace,
                result,
            )
            rec = recommend(task, result, lsm, workspace)
            registry.record_recovery_event(
                task.task_id,
                action=rec.action,
                safe_to_dispatch=rec.safe_to_dispatch,
                reason=rec.reason,
                payload={
                    "assessment": asdict(result),
                    "prompt": rec.prompt,
                    "reconcile_id": reconciliation.reconcile_id,
                    "fence_token": reconciliation.fence_token,
                },
            )
            if args.json:
                _print_json(asdict(rec))
            else:
                print(f"action: {rec.action}")
                print(f"safe_to_dispatch: {str(rec.safe_to_dispatch).lower()}")
                print(f"reason: {rec.reason}")
                if rec.prompt:
                    print("\n--- recovery prompt ---\n" + rec.prompt.rstrip())
            return 0

        if args.command == "watch":
            policy = WatchPolicy(
                browser_suspect_after_s=args.browser_suspect_after,
                network_suspect_after_s=args.network_suspect_after,
                lsm_suspect_after_s=args.lsm_suspect_after,
                hard_stall_after_s=args.hard_stall_after,
            )
            interval = max(1.0, float(args.interval))
            last_signature = None
            lease_name = "default"
            lease_owner = args.lease_owner or uuid.uuid4().hex
            lease_ttl = max(60.0, interval * 3.0)
            lease_acquired = False
            if not args.once:
                lease_acquired, holder = registry.acquire_watchdog_lease(
                    name=lease_name,
                    owner_id=lease_owner,
                    pid=os.getpid(),
                    host=socket.gethostname(),
                    ttl_s=lease_ttl,
                )
                if not lease_acquired:
                    print(
                        "watchdog already active: "
                        f"host={holder['host']} pid={holder['pid']} "
                        f"heartbeat_at={holder['heartbeat_at']:.3f} "
                        f"expires_at={holder['expires_at']:.3f}",
                        file=sys.stderr,
                    )
                    return 4
            try:
                while True:
                    if lease_acquired and not registry.heartbeat_watchdog_lease(
                        name=lease_name,
                        owner_id=lease_owner,
                        ttl_s=lease_ttl,
                    ):
                        print("watchdog lease lost; exiting to prevent duplicate control", file=sys.stderr)
                        return 5
                    queue = _scan_attention(
                        registry,
                        adapter,
                        workspace_probe,
                        policy,
                        uia_probe=ChromeUiaProbe() if args.uia else None,
                        refresh_uia=args.uia,
                    )
                    signature = _attention_signature(queue)
                    if args.once or signature != last_signature:
                        _emit_attention(queue, as_json=args.json)
                        last_signature = signature
                    if args.once:
                        return 0
                    time.sleep(interval)
            except KeyboardInterrupt:
                return 0
            finally:
                if lease_acquired:
                    registry.release_watchdog_lease(name=lease_name, owner_id=lease_owner)

        raise AssertionError(args.command)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except UnsupportedLsmState as exc:
        print(f"Local Shell MCP state is incompatible: {exc}", file=sys.stderr)
        print("Refusing to classify or recover from an unknown durable schema.", file=sys.stderr)
        return 3
    except UiaProbeUnavailable as exc:
        print(f"UI Automation probe unavailable: {exc}", file=sys.stderr)
        return 6
    except CdpProbeUnavailable as exc:
        print(f"CDP probe unavailable: {exc}", file=sys.stderr)
        return 7
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
