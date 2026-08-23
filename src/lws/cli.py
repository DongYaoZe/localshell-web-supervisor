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
from .actions import ActionBlocked, apply_unresolved_action_gate
from .browser import observation_from_dom_payload, observation_from_lsm_snapshot
from .capabilities import (
    CapabilityProvenanceError,
    build_page_close_capabilities,
    capability_matches_context,
    detect_chrome_major,
    runtime_context,
)
from .dispatcher import DispatchDisabled, DispatchPolicy, build_dispatch_plan
from .dispatch_runtime import (
    DispatchExecutionPolicy,
    execute_current_worker_recovery,
    reconcile_action_with_uia,
)
from .doctor import DoctorStatus, run_doctor
from .cdp import CdpNetworkProbe, CdpProbeUnavailable
from .child_batch import advance_child_dispatch_batch, default_transport_factory
from .child_spawn import (
    ChildSpawnBlocked,
    ChromeUiaChildSpawnTransport,
    arm_child_spawn,
    child_spawn_payload,
    execute_child_spawn_open,
    execute_child_spawn_prompt,
    reconcile_child_spawn,
)
from .lifecycle import PageCloseEvidence, evaluate_page_close_evidence
from .lsm import FileLsmTelemetry, UnsupportedLsmState, detect_lsm_state_dir
from .orchestrator import BrowserPoolPolicy, plan_browser_pool
from .models import Assessment, PageCapabilityKind, RecoveryRecommendation, SupervisorState, WorkerStatus
from .overrun_continuation import (
    DEFAULT_OVERRUN_AFTER_S,
    OVERRUN_CONTINUE_PROMPT,
    OVERRUN_TRIGGER_KIND,
    OverrunContinuationPolicy,
    build_overrun_dispatch_plan,
    overrun_clock,
)
from .probe_operator import (
    classify_probe_operation,
    load_probe_reconciliation_evidence,
    probe_operation_payload,
)
from .ram import MemoryProbeUnavailable, observe_system_memory, observe_windows_process_group
from .reconcile import build_reconciliation_record
from .replacement import (
    ReplacementBlocked,
    arm_replacement,
    complete_replacement,
    lsm_takeover_request,
    replacement_payload,
    submit_replacement,
)
from .recovery import recommend
from .registry import Registry
from .runtime_state import resolve_default_registry_path
from .scheduler import attention_queue
from .timeout_recovery import (
    TimeoutRecoveryPolicy,
    gate_timeout_dispatch_plan,
    is_recoverable_delivery_error,
)
from .uia import ChromeUiaProbe, UiaProbeUnavailable, conversation_id_from_url, normalize_url
from .uia_actions import ChromeUiaAckObserver, ChromeUiaActionTransport, UiaActionUnavailable
from .watchdog_host import (
    inspect_watchdog_host,
    launch_detached_watchdog,
    stop_watchdog_host,
)
from .watcher import WatchPolicy, assess
from .workspace import WorkspaceProbe


def default_db_path() -> Path:
    return resolve_default_registry_path()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lws",
        description="LocalShell Web Supervisor: durable browser-worker orchestration",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--db",
        default=None,
        help="registry sqlite path (default: per-user durable state; LWS_DB also overrides)",
    )
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

    child_create = sub.add_parser(
        "child-create",
        help="persist one durable child assignment; does not open a browser or mutate Git",
    )
    child_create.add_argument("parent_task_id")
    contract_group = child_create.add_mutually_exclusive_group()
    contract_group.add_argument(
        "--contract-file",
        help="UTF-8/UTF-8-BOM JSON child contract; preferred for non-ASCII automation",
    )
    contract_group.add_argument(
        "--contract-b64",
        help="base64-encoded UTF-8 JSON child contract; ASCII-safe automation transport",
    )
    child_create.add_argument("--child-key")
    child_create.add_argument("--child-task-id")
    child_create.add_argument("--project")
    child_create.add_argument("--objective")
    child_create.add_argument("--cwd")
    prompt_group = child_create.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    child_create.add_argument("--expected-branch")
    child_create.add_argument("--base-ref")
    child_create.add_argument(
        "--web-project-url",
        help="explicit supported web-project root used only by gated child-spawn",
    )
    child_create.add_argument("--metadata-json")
    child_create.add_argument("--json", action="store_true")

    child_prompt = sub.add_parser("child-prompt", help="print the exact persisted child prompt")
    child_prompt.add_argument("child_task_id")

    child_bind_session = sub.add_parser(
        "child-bind-session",
        help="bind a child to its durable LSM logical session; replacement workers reuse it",
    )
    child_bind_session.add_argument("child_task_id")
    child_bind_session.add_argument("--session-id", required=True)

    child_complete = sub.add_parser(
        "child-complete",
        help="finish the current child worker and durable child task with a completion reference",
    )
    child_complete.add_argument("child_task_id")
    child_complete.add_argument("--completion-ref", required=True)
    child_complete.add_argument("--json", action="store_true")

    child_status = sub.add_parser(
        "child-status", help="show durable child assignments and worker authority for one parent"
    )
    child_status.add_argument("parent_task_id")
    child_status.add_argument("--json", action="store_true")

    child_adopt = sub.add_parser(
        "child-adopt",
        help="adopt an exact existing web chat conversation into a child task; opens no page",
    )
    child_adopt.add_argument("child_task_id")
    child_adopt.add_argument("--conversation-url", required=True)
    child_adopt.add_argument("--worker-id")
    child_adopt.add_argument("--lease-seconds", type=float, default=7200.0)
    child_adopt.add_argument("--json", action="store_true")

    child_spawn_arm = sub.add_parser(
        "child-spawn-arm",
        help="reconcile and write-ahead arm initial child conversation creation; no browser mutation",
    )
    child_spawn_arm.add_argument("child_task_id")
    child_spawn_arm.add_argument("--chrome-executable")
    child_spawn_arm.add_argument("--max-evidence-age", type=float, default=60.0)
    child_spawn_arm.add_argument("--json", action="store_true")

    child_spawn_open = sub.add_parser(
        "child-spawn-open",
        help="explicitly open the one LWS-tagged project window for an ARMED child spawn",
    )
    child_spawn_open.add_argument("attempt_id")
    child_spawn_open.add_argument("--enable-normal-browser-mutation", action="store_true")
    child_spawn_open.add_argument("--confirm-child", required=True)
    child_spawn_open.add_argument("--json", action="store_true")

    child_spawn_send = sub.add_parser(
        "child-spawn-send",
        help="explicitly send the persisted child prompt in the exact LWS-owned project window",
    )
    child_spawn_send.add_argument("attempt_id")
    child_spawn_send.add_argument("--enable-normal-browser-mutation", action="store_true")
    child_spawn_send.add_argument("--confirm-child", required=True)
    child_spawn_send.add_argument("--lease-seconds", type=float, default=7200.0)
    child_spawn_send.add_argument(
        "--rate-limit-cooldown",
        type=float,
        default=120.0,
        help="seconds to pause global child dispatch after a Too many requests modal",
    )
    child_spawn_send.add_argument(
        "--wait-cooldown",
        action="store_true",
        help="wait through one persisted rate-limit cooldown and retry the same pre-send child safely",
    )
    child_spawn_send.add_argument(
        "--max-cooldown-wait",
        type=float,
        default=600.0,
        help="maximum seconds child-spawn-send may wait when --wait-cooldown is enabled",
    )
    child_spawn_send.add_argument("--json", action="store_true")

    child_spawn_reconcile = sub.add_parser(
        "child-spawn-reconcile",
        help="read-only reconcile an interrupted child-spawn; never opens a window or sends",
    )
    child_spawn_reconcile.add_argument("attempt_id")
    child_spawn_reconcile.add_argument("--lease-seconds", type=float, default=7200.0)
    child_spawn_reconcile.add_argument("--json", action="store_true")

    child_spawn_status = sub.add_parser(
        "child-spawn-status", help="show durable child-spawn write-ahead history"
    )
    child_spawn_status.add_argument("child_task_id")
    child_spawn_status.add_argument("--limit", type=int, default=20)
    child_spawn_status.add_argument("--json", action="store_true")

    child_dispatch_batch = sub.add_parser(
        "child-dispatch-batch",
        help=(
            "advance persisted children through a bounded exact-window dispatcher pool; "
            "stops before replaying ambiguous browser outcomes"
        ),
    )
    child_dispatch_batch.add_argument("parent_task_id")
    child_dispatch_batch.add_argument("--max-windows", type=int, default=2)
    child_dispatch_batch.add_argument("--chrome-executable")
    child_dispatch_batch.add_argument("--max-evidence-age", type=float, default=60.0)
    child_dispatch_batch.add_argument("--lease-seconds", type=float, default=7200.0)
    child_dispatch_batch.add_argument("--enable-normal-browser-mutation", action="store_true")
    child_dispatch_batch.add_argument("--confirm-parent", required=True)
    child_dispatch_batch.add_argument(
        "--keep-terminal-pages",
        action="store_true",
        help="do not close exact terminal child pages after all children have been dispatched",
    )
    child_dispatch_batch.add_argument("--json", action="store_true")

    replacement_register = sub.add_parser(
        "replacement-register",
        help="register an exact replacement conversation as a non-authoritative candidate",
    )
    replacement_register.add_argument("child_task_id")
    replacement_register.add_argument("--conversation-url", required=True)
    replacement_register.add_argument("--worker-id")
    replacement_register.add_argument("--json", action="store_true")

    replacement_arm = sub.add_parser(
        "replacement-arm",
        help="reconcile LSM/workspace and durably arm one replacement before LSM takeover",
    )
    replacement_arm.add_argument("child_task_id")
    replacement_arm.add_argument("--candidate-worker-id", required=True)
    replacement_arm.add_argument("--max-evidence-age", type=float, default=60.0)
    replacement_arm.add_argument("--json", action="store_true")

    replacement_submit = sub.add_parser(
        "replacement-submit",
        help="persist one-time LSM takeover authority and print the supported MCP request",
    )
    replacement_submit.add_argument("attempt_id")
    replacement_submit.add_argument("--json", action="store_true")

    replacement_complete = sub.add_parser(
        "replacement-complete",
        help="publish replacement worker authority after the LSM takeover is observable",
    )
    replacement_complete.add_argument("attempt_id")
    replacement_complete.add_argument("--new-run-id", required=True)
    replacement_complete.add_argument("--lease-seconds", type=float, default=7200.0)
    replacement_complete.add_argument("--max-evidence-age", type=float, default=60.0)
    replacement_complete.add_argument("--json", action="store_true")

    replacement_status = sub.add_parser(
        "replacement-status", help="show replacement write-ahead history for one child task"
    )
    replacement_status.add_argument("child_task_id")
    replacement_status.add_argument("--limit", type=int, default=20)
    replacement_status.add_argument("--json", action="store_true")

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
    watch.add_argument(
        "--auto-recover-timeouts",
        action="store_true",
        help=(
            "opt in to resident recovery only for explicit web chat delivery errors; "
            "all two-sample/LSM/workspace/exact-window/action fences still apply"
        ),
    )
    watch.add_argument(
        "--auto-continue-overruns",
        action="store_true",
        help="send a fenced continue when a managed work turn exceeds the hard wall-clock limit",
    )
    watch.add_argument(
        "--overrun-after",
        type=float,
        default=float(DEFAULT_OVERRUN_AFTER_S),
        help="seconds before hard-overrun auto-continue (default: 1520 = 25m20s)",
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
    watchdog_start.add_argument(
        "--auto-recover-timeouts",
        action="store_true",
        help="explicitly enable fenced resident recovery for delivery-timeout errors",
    )
    watchdog_start.add_argument("--auto-continue-overruns", action="store_true")
    watchdog_start.add_argument(
        "--overrun-after", type=float, default=float(DEFAULT_OVERRUN_AFTER_S)
    )
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

    watchdog_restart = sub.add_parser(
        "watchdog-restart",
        help="cooperatively stop the resident watchdog, prove shutdown, then launch a replacement",
    )
    watchdog_restart.add_argument("--interval", type=float, default=30.0)
    watchdog_restart.add_argument("--uia", action="store_true")
    watchdog_restart.add_argument(
        "--auto-recover-timeouts",
        action="store_true",
        help="explicitly enable fenced resident recovery for delivery-timeout errors",
    )
    watchdog_restart.add_argument("--auto-continue-overruns", action="store_true")
    watchdog_restart.add_argument(
        "--overrun-after", type=float, default=float(DEFAULT_OVERRUN_AFTER_S)
    )
    watchdog_restart.add_argument("--log")
    watchdog_restart.add_argument("--ready-timeout", type=float, default=8.0)
    watchdog_restart.add_argument("--grace", type=float, default=90.0)
    watchdog_restart.add_argument("--wait", type=float, default=35.0)
    watchdog_restart.add_argument("--json", action="store_true")

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
    pool_capability = pool.add_mutually_exclusive_group()
    pool_capability.add_argument(
        "--page-close-evidence",
        help=(
            "legacy one-shot PageCloseEvidence JSON; prefer a durable capability id"
        ),
    )
    pool_capability.add_argument(
        "--page-close-capability",
        help=(
            "explicit durable generation capability id, or 'latest'; default remains fail-closed"
        ),
    )
    pool.add_argument(
        "--browser-major",
        type=int,
        help="override local Chrome major used only for capability-context validation",
    )
    pool.add_argument("--json", action="store_true")

    capability_import = sub.add_parser(
        "capability-import",
        help="validate page-close evidence and store versioned deployment-scoped capability records",
    )
    capability_import.add_argument("--file", required=True)
    capability_import.add_argument("--observed-at", type=float)
    capability_import.add_argument("--scope-host")
    capability_import.add_argument("--browser-family")
    capability_import.add_argument("--browser-major", type=int)
    capability_import.add_argument("--platform")
    capability_import.add_argument("--surface")
    capability_import.add_argument("--ttl-hours", type=float, default=24.0)
    capability_import.add_argument("--json", action="store_true")

    capability_status = sub.add_parser(
        "capability-status",
        help="show durable page-close capability provenance and current-context usability",
    )
    capability_status.add_argument(
        "--kind",
        choices=[kind.value for kind in PageCapabilityKind],
    )
    capability_status.add_argument("--browser-major", type=int)
    capability_status.add_argument("--limit", type=int, default=50)
    capability_status.add_argument("--json", action="store_true")

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

    probe_op_status = sub.add_parser(
        "probe-op-status",
        help="show a durable probe mutation operation without observing or changing a browser",
    )
    probe_op_status.add_argument("operation_id", nargs="?")
    probe_op_status.add_argument(
        "--latest",
        action="store_true",
        help="show the latest operation even when it is terminal; default selects unresolved",
    )
    probe_op_status.add_argument("--json", action="store_true")

    probe_op_reconcile = sub.add_parser(
        "probe-op-reconcile",
        help="reconcile one durable probe mutation from bounded previously-observed local evidence",
    )
    probe_op_reconcile.add_argument("operation_id")
    probe_op_reconcile.add_argument("--file", required=True)
    probe_op_reconcile.add_argument("--json", action="store_true")

    lifecycle = sub.add_parser(
        "evaluate-page-close",
        help="evaluate JSON evidence for isolated web-page close/reopen/reopen parking safety",
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
        help="dry-run a fenced recovery action; execution remains explicit and opt-in",
    )
    dispatch.add_argument("task_id")
    dispatch.add_argument("--uia", action="store_true", help="refresh exact-URL Chrome UIA first")
    dispatch.add_argument("--max-fence-age", type=float, default=120.0)
    dispatch.add_argument("--min-fence-separation", type=float, default=3.0)
    dispatch.add_argument("--min-dom-quiet", type=float, default=5.0)
    dispatch.add_argument("--min-network-quiet", type=float, default=5.0)
    dispatch.add_argument("--json", action="store_true")

    dispatch_execute = sub.add_parser(
        "dispatch-execute",
        help="explicit one-shot fenced current-worker recovery; disabled unless fully opted in",
    )
    dispatch_execute.add_argument("task_id")
    dispatch_execute.add_argument(
        "--enable-experimental-uia",
        action="store_true",
        help="explicitly enable the gated exact-window UIA transport for this invocation only",
    )
    dispatch_execute.add_argument(
        "--confirm-task",
        required=True,
        help="must exactly equal task_id; prevents accidental cross-task execution",
    )
    dispatch_execute.add_argument("--max-fence-age", type=float, default=120.0)
    dispatch_execute.add_argument("--min-fence-separation", type=float, default=3.0)
    dispatch_execute.add_argument("--min-dom-quiet", type=float, default=5.0)
    dispatch_execute.add_argument("--min-network-quiet", type=float, default=5.0)
    dispatch_execute.add_argument("--json", action="store_true")

    action_reconcile_uia = sub.add_parser(
        "action-reconcile-uia",
        help="observe one unresolved action and acknowledge only exact single-turn completion evidence",
    )
    action_reconcile_uia.add_argument("attempt_id")
    action_reconcile_uia.add_argument("--json", action="store_true")

    rec = sub.add_parser("recommend", help="print safe recovery recommendation for a task")
    rec.add_argument("task_id")
    rec.add_argument("--uia", action="store_true", help="refresh exact-URL Chrome UIA first")
    rec.add_argument("--json", action="store_true")
    return p


def _runtime_browser_major(explicit: int | None = None) -> int:
    if explicit is not None:
        if int(explicit) <= 0:
            raise CapabilityProvenanceError("browser major override must be positive")
        return int(explicit)
    probe = ChromeUiaProbe()
    if not probe.chrome_executable:
        raise CapabilityProvenanceError("local Google Chrome executable was not found")
    return detect_chrome_major(probe.chrome_executable)


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
    if registry.worker_protocol_exists(task_id):
        protocol = registry.load_worker_protocol(task_id)
    else:
        protocol = None
    if protocol is not None and protocol.task_status.value == "completed":
        if task.state != SupervisorState.COMPLETED:
            registry.update_state(task_id, SupervisorState.COMPLETED)
            task = registry.get_task(task_id)
        browser = registry.latest_browser_observation(task.current_worker_id)
        return (
            task,
            browser,
            None,
            None,
            Assessment(
                SupervisorState.COMPLETED,
                "durable worker-protocol task is already completed",
                "high",
                ["worker_protocol.task_status=completed"],
            ),
        )
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
    if (
        protocol is None
        and result.state != task.state
        and result.confidence in {"high", "medium"}
    ):
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


def _auto_recover_timeout_cycle(
    registry: Registry,
    adapter,
    workspace_probe: WorkspaceProbe,
    queue,
    *,
    policy: TimeoutRecoveryPolicy,
) -> list[dict]:
    """Run one bounded resident timeout-recovery cycle.

    The mode is intentionally narrow: it only targets explicit web chat delivery errors,
    reuses the exact current worker window, and still requires the ordinary two-sample
    semantic fence plus durable LSM/workspace/action/recovery-budget checks. At most one
    external send with a possible side effect is attempted per watchdog cycle.
    """

    if not policy.enabled:
        return []

    task_ids = list(dict.fromkeys(item.task_id for item in queue))
    results: list[dict] = []
    uia_probe = ChromeUiaProbe()

    # First reconcile any ambiguous/previously-submitted action. This is read-only and can
    # release the per-task send lock only from positive nonce/hash completion evidence.
    for task_id in task_ids:
        attempt = registry.unresolved_action_attempt(task_id)
        if attempt is None:
            continue
        try:
            _refresh_uia(registry, task_id, uia_probe)
            reconciled = reconcile_action_with_uia(
                registry,
                attempt_id=attempt.attempt_id,
                observer_factory=lambda chrome: ChromeUiaAckObserver(
                    chrome_executable=chrome
                ),
            )
            detail = reconciled.detail
            if reconciled.acknowledged:
                browser_after_ack = registry.latest_browser_observation(
                    registry.get_task(task_id).current_worker_id
                )
                if browser_after_ack is not None and browser_after_ack.message_signature:
                    registry.record_action_ack_browser_signature(
                        attempt.attempt_id,
                        message_signature=browser_after_ack.message_signature,
                    )
            results.append(
                {
                    "task_id": task_id,
                    "kind": "ack",
                    "attempt_id": attempt.attempt_id,
                    "state": reconciled.state,
                    "acknowledged": reconciled.acknowledged,
                    "detail": detail,
                }
            )
            registry.record_recovery_event(
                task_id,
                action=f"watchdog_ack:{reconciled.state}",
                safe_to_dispatch=False,
                reason=detail,
                payload={"attempt_id": attempt.attempt_id},
            )
        except (ActionBlocked, UiaActionUnavailable, UiaProbeUnavailable) as exc:
            results.append(
                {
                    "task_id": task_id,
                    "kind": "ack",
                    "attempt_id": attempt.attempt_id,
                    "state": attempt.state.value,
                    "acknowledged": False,
                    "detail": f"ack reconciliation blocked: {exc}",
                }
            )

    for task_id in task_ids:
        if registry.unresolved_action_attempt(task_id) is not None:
            continue

        # Autorecovery requires a fresh positive exact-window observation in this cycle.
        try:
            _refresh_uia(registry, task_id, uia_probe)
        except UiaProbeUnavailable as exc:
            results.append(
                {
                    "task_id": task_id,
                    "kind": "dispatch",
                    "submitted": False,
                    "detail": f"fresh exact-window observation unavailable: {exc}",
                }
            )
            continue

        previous = registry.latest_reconciliation(task_id)
        task, browser, lsm, workspace, assessment = _assessment(
            registry,
            task_id,
            adapter,
            workspace_probe,
            policy=None,
            reconcile_workspace=True,
            refresh_uia=False,
        )
        if not is_recoverable_delivery_error(browser):
            continue

        current = _record_current_reconciliation(
            registry,
            task,
            browser,
            lsm,
            workspace,
            assessment,
        )
        recommendation = recommend(task, assessment, lsm, workspace)
        plan = build_dispatch_plan(
            task,
            recommendation,
            previous=previous,
            current=current,
            policy=DispatchPolicy(transport_enabled=True),
        )
        plan = apply_unresolved_action_gate(
            plan,
            registry.unresolved_action_attempt(task.task_id),
        )
        plan = gate_timeout_dispatch_plan(
            registry,
            plan,
            browser=browser,
            policy=policy,
        )
        if not plan.would_dispatch:
            results.append(
                {
                    "task_id": task_id,
                    "kind": "dispatch",
                    "submitted": False,
                    "detail": plan.reason,
                    "blockers": list(plan.blockers),
                }
            )
            continue

        try:
            execution = execute_current_worker_recovery(
                registry,
                plan=plan,
                recommendation=recommendation,
                policy=DispatchExecutionPolicy(
                    enabled=True,
                    confirmed_task_id=task.task_id,
                ),
                transport_factory=lambda binding: ChromeUiaActionTransport.from_binding(
                    binding,
                    enabled=True,
                ),
            )
        except (ActionBlocked, DispatchDisabled, UiaActionUnavailable) as exc:
            registry.record_recovery_event(
                task.task_id,
                action="watchdog_timeout_blocked",
                safe_to_dispatch=False,
                reason=str(exc),
                payload={"dispatch_plan": asdict(plan)},
            )
            results.append(
                {
                    "task_id": task_id,
                    "kind": "dispatch",
                    "submitted": False,
                    "detail": str(exc),
                }
            )
            continue

        registry.record_recovery_event(
            task.task_id,
            action=f"watchdog_timeout:{execution.state}",
            safe_to_dispatch=execution.submitted,
            reason=execution.detail,
            payload={
                "attempt_id": execution.attempt_id,
                "side_effect_possible": execution.side_effect_possible,
                "dispatch_plan": asdict(plan),
            },
        )
        results.append(
            {
                "task_id": task_id,
                "kind": "dispatch",
                "attempt_id": execution.attempt_id,
                "state": execution.state,
                "submitted": execution.submitted,
                "side_effect_possible": execution.side_effect_possible,
                "detail": execution.detail,
            }
        )
        if execution.submitted or execution.side_effect_possible:
            break

    return results


def _auto_continue_overrun_cycle(
    registry: Registry,
    adapter,
    workspace_probe: WorkspaceProbe,
    *,
    policy: OverrunContinuationPolicy,
    now: float | None = None,
) -> list[dict]:
    """Run one bounded hard-overrun continuation cycle across all managed tasks.

    Unlike delivery-error recovery, eligibility is wall-clock based and therefore scans all
    managed tasks, not only the attention queue. At most one browser side effect is attempted
    per cycle. Ambiguous/previously-submitted overrun actions are reconciled before any new send.
    """

    if not policy.enabled:
        return []
    ts = time.time() if now is None else float(now)
    results: list[dict] = []
    uia_probe = ChromeUiaProbe()
    if policy.auto_discover_visible_conversations:
        _sync_visible_conversation_watchers(registry, uia_probe, policy=policy)
    tasks = _canonical_overrun_tasks(registry)

    for task in tasks:
        attempt = registry.unresolved_action_attempt(task.task_id)
        if attempt is None or attempt.metadata.get("trigger_kind") != OVERRUN_TRIGGER_KIND:
            continue
        try:
            _refresh_uia(registry, task.task_id, uia_probe)
            ack_now = time.time() if now is None else ts
            reconciled = reconcile_action_with_uia(
                registry,
                attempt_id=attempt.attempt_id,
                observer_factory=lambda chrome: ChromeUiaAckObserver(
                    chrome_executable=chrome
                ),
                now=ack_now,
            )
            if reconciled.acknowledged:
                current_task = registry.get_task(task.task_id)
                browser_after_ack = registry.latest_browser_observation(
                    current_task.current_worker_id
                )
                if browser_after_ack is not None and browser_after_ack.message_signature:
                    registry.record_action_ack_browser_signature(
                        attempt.attempt_id,
                        message_signature=browser_after_ack.message_signature,
                    )
            detail = reconciled.detail
            results.append(
                {
                    "task_id": task.task_id,
                    "kind": "overrun_ack",
                    "attempt_id": attempt.attempt_id,
                    "state": reconciled.state,
                    "acknowledged": reconciled.acknowledged,
                    "detail": detail,
                }
            )
            registry.record_recovery_event(
                task.task_id,
                action=f"watchdog_overrun_ack:{reconciled.state}",
                safe_to_dispatch=False,
                reason=detail,
                payload={"attempt_id": attempt.attempt_id},
            )
        except (ActionBlocked, UiaActionUnavailable, UiaProbeUnavailable) as exc:
            results.append(
                {
                    "task_id": task.task_id,
                    "kind": "overrun_ack",
                    "attempt_id": attempt.attempt_id,
                    "state": attempt.state.value,
                    "acknowledged": False,
                    "detail": f"overrun acknowledgement blocked: {exc}",
                }
            )

    for original_task in tasks:
        clock = overrun_clock(
            registry,
            original_task.task_id,
            policy=policy,
            now=ts,
        )
        if clock is None or not clock.sample_due:
            continue
        if registry.unresolved_action_attempt(original_task.task_id) is not None:
            continue

        try:
            _refresh_uia(registry, original_task.task_id, uia_probe)
        except UiaProbeUnavailable as exc:
            if clock.due:
                results.append(
                    {
                        "task_id": original_task.task_id,
                        "kind": "overrun_dispatch",
                        "submitted": False,
                        "detail": f"fresh exact-window observation unavailable: {exc}",
                    }
                )
            continue

        previous = registry.latest_reconciliation(original_task.task_id)
        task, browser, lsm, workspace, assessment = _assessment(
            registry,
            original_task.task_id,
            adapter,
            workspace_probe,
            policy=None,
            reconcile_workspace=True,
            refresh_uia=False,
        )
        current = _record_current_reconciliation(
            registry,
            task,
            browser,
            lsm,
            workspace,
            assessment,
        )
        # Fresh UIA/reconciliation evidence is collected during this cycle. Never compare
        # those timestamps against a wall-clock sample captured before the evidence existed:
        # that makes valid live evidence look future-dated and permanently blocks dispatch.
        action_now = time.time() if now is None else ts
        action_now = max(
            action_now,
            float(current.created_at),
            float(browser.observed_at) if browser is not None else action_now,
        )
        clock = overrun_clock(registry, task.task_id, policy=policy, now=action_now)
        if clock is None or not clock.due:
            continue

        plan = build_overrun_dispatch_plan(
            registry,
            task,
            clock=clock,
            previous=previous,
            current=current,
            browser=browser,
            policy=policy,
            transport_enabled=True,
            now=action_now,
        )
        plan = apply_unresolved_action_gate(
            plan,
            registry.unresolved_action_attempt(task.task_id),
        )
        if not plan.would_dispatch:
            results.append(
                {
                    "task_id": task.task_id,
                    "kind": "overrun_dispatch",
                    "submitted": False,
                    "detail": plan.reason,
                    "elapsed_s": clock.elapsed_s,
                    "anchor_at": clock.anchor_at,
                    "blockers": list(plan.blockers),
                }
            )
            continue

        recommendation = RecoveryRecommendation(
            task_id=task.task_id,
            action="reconcile_then_continue",
            safe_to_dispatch=False,
            reason="hard-overrun wall clock exceeded; continue the same durable task",
            prompt=OVERRUN_CONTINUE_PROMPT,
            evidence=list(assessment.evidence),
        )
        try:
            execution = execute_current_worker_recovery(
                registry,
                plan=plan,
                recommendation=recommendation,
                policy=DispatchExecutionPolicy(
                    enabled=True,
                    confirmed_task_id=task.task_id,
                    consume_recovery_budget=False,
                    attempt_metadata={
                        "trigger_kind": OVERRUN_TRIGGER_KIND,
                        "overrun_after_s": float(policy.overrun_after_s),
                        "overrun_anchor_at": clock.anchor_at,
                        "overrun_due_at": clock.due_at,
                    },
                ),
                transport_factory=lambda binding: ChromeUiaActionTransport.from_binding(
                    binding,
                    enabled=True,
                ),
                now=action_now,
            )
        except (ActionBlocked, DispatchDisabled, UiaActionUnavailable) as exc:
            registry.record_recovery_event(
                task.task_id,
                action="watchdog_overrun_blocked",
                safe_to_dispatch=False,
                reason=str(exc),
                payload={"dispatch_plan": asdict(plan)},
            )
            results.append(
                {
                    "task_id": task.task_id,
                    "kind": "overrun_dispatch",
                    "submitted": False,
                    "detail": str(exc),
                }
            )
            continue

        registry.record_recovery_event(
            task.task_id,
            action=f"watchdog_overrun:{execution.state}",
            safe_to_dispatch=execution.submitted,
            reason=execution.detail,
            payload={
                "attempt_id": execution.attempt_id,
                "side_effect_possible": execution.side_effect_possible,
                "elapsed_s": clock.elapsed_s,
                "anchor_at": clock.anchor_at,
                "dispatch_plan": asdict(plan),
            },
        )
        results.append(
            {
                "task_id": task.task_id,
                "kind": "overrun_dispatch",
                "attempt_id": execution.attempt_id,
                "state": execution.state,
                "submitted": execution.submitted,
                "side_effect_possible": execution.side_effect_possible,
                "elapsed_s": clock.elapsed_s,
                "anchor_at": clock.anchor_at,
                "detail": execution.detail,
            }
        )
        if execution.submitted or execution.side_effect_possible:
            break

    return results


AUTO_WATCH_TASK_PREFIX = "watch-chat-"


def _sync_visible_conversation_watchers(
    registry: Registry,
    uia_probe: ChromeUiaProbe,
    *,
    policy: OverrunContinuationPolicy,
) -> list[str]:
    """Discover visible normal-Chrome conversations and give each one one durable watch owner.

    This is intentionally conversation-level rather than project/task-level. Existing LWS task
    aliases may point at the same web conversation; the synthetic owner prevents those aliases
    from producing duplicate overrun sends.
    """
    discovered: list[str] = []
    try:
        rows = uia_probe.discover_conversations()
    except UiaProbeUnavailable:
        return discovered
    for row in rows:
        url = str(row.get("url") or "").strip()
        conversation_id = conversation_id_from_url(url)
        if not conversation_id:
            continue
        task_id = f"{AUTO_WATCH_TASK_PREFIX}{conversation_id.lower()}"
        try:
            task = registry.get_task(task_id)
        except KeyError:
            task = registry.register_task(
                task_id=task_id,
                project="chatgpt-autowatch",
                objective="automatic 25m20 visible-conversation watchdog",
                cwd=str(Path.cwd()),
                conversation_url=url,
                conversation_id=conversation_id,
            )
            registry.set_checkpoint(
                task_id,
                {"auto_watch": True, "conversation_id": conversation_id},
            )
        if not task.current_worker_id:
            worker = registry.add_worker(
                task_id,
                url,
                conversation_id=conversation_id,
                make_current=True,
            )
            task = registry.get_task(task_id)
        else:
            worker = registry.get_worker(task.current_worker_id)
            if normalize_url(worker.conversation_url) != normalize_url(url):
                worker = registry.add_worker(
                    task_id,
                    url,
                    conversation_id=conversation_id,
                    make_current=True,
                )
        registry.bind_worker_window(
            worker.worker_id,
            window_handle=int(row["window_handle"]),
            browser_pid=int(row["browser_pid"]),
            chrome_executable=uia_probe.chrome_executable or "",
            conversation_url=url,
            source="windows_uia_autodiscovery",
            observed_at=float(row.get("observed_at") or time.time()),
            ttl_s=max(60.0, float(policy.max_browser_observation_age_s) + 30.0),
        )
        discovered.append(task_id)
    return discovered


def _canonical_overrun_tasks(registry: Registry) -> list:
    """Choose at most one current task for each conversation URL.

    A synthetic auto-watch owner wins over legacy task aliases. Without a synthetic owner,
    choose the most recently updated task deterministically so one URL can never be nudged twice
    in the same or later cycle merely because multiple durable task records reference it.
    """
    groups: dict[str, list] = {}
    for task in registry.list_tasks():
        if task.state in {SupervisorState.COMPLETED, SupervisorState.ABANDONED}:
            continue
        if not task.current_worker_id:
            continue
        try:
            worker = registry.get_worker(task.current_worker_id)
        except KeyError:
            continue
        if worker.status != WorkerStatus.ACTIVE or not worker.conversation_url:
            continue
        conversation_id = conversation_id_from_url(worker.conversation_url)
        key = f"conversation:{conversation_id.lower()}" if conversation_id else normalize_url(worker.conversation_url)
        groups.setdefault(key, []).append(task)

    selected = []
    for candidates in groups.values():
        auto = [task for task in candidates if task.task_id.startswith(AUTO_WATCH_TASK_PREFIX)]
        pool = auto or candidates
        selected.append(max(pool, key=lambda task: (float(task.updated_at), task.task_id)))
    return selected


def _print_json(data) -> None:
    # Keep machine-readable stdout ASCII-only so Windows PowerShell/OEM code pages
    # cannot corrupt non-ASCII text while capturing a native process. JSON consumers
    # reconstruct the original Unicode from escapes.
    print(json.dumps(data, indent=2, ensure_ascii=True, default=str))


def _read_json_file(path: str | Path):
    # PowerShell 5.1's `Set-Content -Encoding UTF8` writes a UTF-8 BOM.
    # utf-8-sig accepts both BOM-prefixed and ordinary UTF-8 JSON.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _decode_utf8_json_b64(value: str) -> dict:
    import base64

    try:
        raw = base64.b64decode(str(value), validate=True)
        payload = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("--contract-b64 must contain base64-encoded UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("child contract must be a JSON object")
    return payload


def _reject_likely_cli_text_corruption(field: str, value: str | None) -> None:
    if value is None:
        return
    text = str(value)
    # U+FFFD is conclusive evidence that decoding already lost bytes. A '?' inside a
    # Windows path is impossible and is the common PowerShell/code-page failure mode
    # seen during child fan-out, including paths embedded inside objective/prompt text.
    broken_windows_path = bool(
        __import__("re").search(r"(?i)\b[A-Z]:\\[^\r\n]*\?", text)
    )
    if "\ufffd" in text or (field == "cwd" and "?" in text) or broken_windows_path:
        raise ValueError(
            f"{field} appears encoding-corrupted before LWS ingestion; "
            "use child-create --contract-file or --contract-b64 with UTF-8"
        )


def _child_create_contract(args) -> dict:
    if args.contract_file or args.contract_b64:
        individual = {
            "child_key": args.child_key,
            "child_task_id": args.child_task_id,
            "project": args.project,
            "objective": args.objective,
            "cwd": args.cwd,
            "prompt": args.prompt,
            "prompt_file": args.prompt_file,
            "expected_branch": args.expected_branch,
            "base_ref": args.base_ref,
            "web_project_url": args.web_project_url,
            "metadata_json": args.metadata_json,
        }
        if any(value is not None for value in individual.values()):
            raise ValueError(
                "--contract-file/--contract-b64 cannot be combined with individual child-create fields"
            )
        payload = (
            _read_json_file(args.contract_file)
            if args.contract_file
            else _decode_utf8_json_b64(args.contract_b64)
        )
        if not isinstance(payload, dict):
            raise ValueError("child contract must be a JSON object")
        allowed = {
            "child_key", "child_task_id", "project", "objective", "cwd", "prompt_text",
            "expected_branch", "base_ref", "web_project_url", "metadata",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown child contract fields: {', '.join(unknown)}")
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("child contract metadata must be a JSON object")
        result = dict(payload)
        result["metadata"] = metadata
    else:
        prompt_text = (
            args.prompt
            if args.prompt is not None
            else (
                Path(args.prompt_file).read_text(encoding="utf-8-sig")
                if args.prompt_file
                else None
            )
        )
        metadata = json.loads(args.metadata_json) if args.metadata_json else {}
        if not isinstance(metadata, dict):
            raise ValueError("--metadata-json must be a JSON object")
        result = {
            "child_key": args.child_key,
            "child_task_id": args.child_task_id,
            "project": args.project,
            "objective": args.objective,
            "cwd": args.cwd,
            "prompt_text": prompt_text,
            "expected_branch": args.expected_branch,
            "base_ref": args.base_ref,
            "web_project_url": args.web_project_url,
            "metadata": metadata,
        }

    for required in ("child_key", "project", "objective", "cwd", "prompt_text"):
        if result.get(required) is None or not str(result[required]).strip():
            raise ValueError(f"child-create requires {required}")
    for field in ("project", "objective", "cwd", "prompt_text"):
        _reject_likely_cli_text_corruption(field, result.get(field))
    return result


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

        if args.command == "child-create":
            contract = _child_create_contract(args)
            dispatch = registry.create_child_dispatch(
                args.parent_task_id,
                child_key=contract["child_key"],
                child_task_id=contract.get("child_task_id"),
                project=contract["project"],
                objective=contract["objective"],
                cwd=contract["cwd"],
                prompt_text=contract["prompt_text"],
                expected_branch=contract.get("expected_branch"),
                base_ref=contract.get("base_ref"),
                web_project_url=contract.get("web_project_url"),
                metadata=contract.get("metadata") or {},
            )
            if args.json:
                _print_json(asdict(dispatch))
            else:
                print(
                    f"{dispatch.child_task_id} dispatch={dispatch.dispatch_id} "
                    f"prompt_sha256={dispatch.prompt_sha256}"
                )
            return 0

        if args.command == "child-prompt":
            print(registry.get_child_dispatch(args.child_task_id).prompt_text, end="")
            return 0

        if args.command == "child-bind-session":
            task = registry.bind_child_lsm_session(args.child_task_id, args.session_id)
            print(f"{task.task_id} session={task.lsm_session_id}")
            return 0

        if args.command == "child-complete":
            state = registry.complete_child_dispatch(
                args.child_task_id,
                completion_ref=args.completion_ref,
            )
            payload = {
                "child_task_id": args.child_task_id,
                "durable_task_status": state.task_status.value,
                "completion_ref": state.completion_ref,
                "generation": state.generation,
                "protocol_revision": state.revision,
                "legacy_task_state": registry.get_task(args.child_task_id).state.value,
            }
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"{args.child_task_id} status={state.task_status.value} "
                    f"completion_ref={state.completion_ref}"
                )
            return 0

        if args.command == "child-status":
            rows = []
            for dispatch in registry.child_dispatches_for_parent(args.parent_task_id):
                task = registry.get_task(dispatch.child_task_id)
                protocol = registry.load_worker_protocol(dispatch.child_task_id)
                rows.append(
                    {
                        "dispatch": asdict(dispatch),
                        "task_state": task.state.value,
                        "lsm_session_id": task.lsm_session_id,
                        "protocol_revision": protocol.revision,
                        "generation": protocol.generation,
                        "durable_task_status": protocol.task_status.value,
                        "current_worker_id": protocol.current_worker_id,
                        "workers": [
                            {
                                "worker_id": worker.worker_id,
                                "conversation_ref": worker.conversation_ref,
                                "status": worker.status.value,
                                "generation": worker.generation,
                                "lease_expires_at": worker.lease_expires_at,
                            }
                            for worker in protocol.workers
                        ],
                    }
                )
            if args.json:
                _print_json(rows)
            else:
                for row in rows:
                    dispatch = row["dispatch"]
                    print(
                        f"{dispatch['child_key']}: {dispatch['child_task_id']} "
                        f"status={row['durable_task_status']} generation={row['generation']} "
                        f"worker={row['current_worker_id'] or '-'}"
                    )
            return 0

        if args.command == "child-adopt":
            state = registry.adopt_child_worker(
                args.child_task_id,
                args.conversation_url,
                lease_seconds=args.lease_seconds,
                worker_id=args.worker_id,
            )
            worker = next(w for w in state.workers if w.worker_id == state.current_worker_id)
            payload = {
                "child_task_id": args.child_task_id,
                "worker_id": worker.worker_id,
                "generation": state.generation,
                "revision": state.revision,
                "conversation_url": worker.conversation_ref,
                "lease_expires_at": worker.lease_expires_at,
            }
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"{worker.worker_id} generation={state.generation}; "
                    "conversation adopted, no browser page was opened"
                )
            return 0

        if args.command == "child-spawn-arm":
            workspace = _refresh_workspace(registry, args.child_task_id, workspace_probe)
            chrome_executable = args.chrome_executable or ChromeUiaProbe().chrome_executable
            attempt = arm_child_spawn(
                registry,
                child_task_id=args.child_task_id,
                chrome_executable=chrome_executable or "",
                workspace=workspace,
                max_evidence_age_s=args.max_evidence_age,
            )
            if args.json:
                _print_json(child_spawn_payload(attempt))
            else:
                print(
                    f"{attempt.attempt_id} state={attempt.state.value} project={attempt.project_id}; "
                    "no browser mutation performed"
                )
            return 0

        if args.command in {"child-spawn-open", "child-spawn-send"}:
            attempt = registry.get_child_spawn_attempt(args.attempt_id)
            if args.confirm_child != attempt.child_task_id:
                raise ChildSpawnBlocked(
                    ["--confirm-child must exactly match the durable child task id"]
                )
            if not args.enable_normal_browser_mutation:
                raise ChildSpawnBlocked(
                    ["normal-browser child-spawn mutation is disabled without explicit opt-in"]
                )
            transport = ChromeUiaChildSpawnTransport(
                chrome_executable=attempt.chrome_executable,
                enabled=True,
            )
            if args.command == "child-spawn-open":
                result = execute_child_spawn_open(
                    registry,
                    attempt_id=args.attempt_id,
                    transport=transport,
                )
                success = result.state.value == "WINDOW_BOUND"
            else:
                wait_deadline = time.time() + max(0.0, float(args.max_cooldown_wait))
                while True:
                    result = execute_child_spawn_prompt(
                        registry,
                        attempt_id=args.attempt_id,
                        transport=transport,
                        lease_seconds=args.lease_seconds,
                        rate_limit_cooldown_s=args.rate_limit_cooldown,
                    )
                    cooldown_until = float(result.metadata.get("cooldown_until") or 0.0)
                    remaining = cooldown_until - time.time()
                    if (
                        result.state.value == "WINDOW_BOUND"
                        and remaining > 0
                        and args.wait_cooldown
                        and time.time() + remaining <= wait_deadline
                    ):
                        time.sleep(remaining + 0.25)
                        continue
                    break
                cooldown_handled = (
                    result.state.value == "WINDOW_BOUND"
                    and float(result.metadata.get("cooldown_until") or 0.0) > time.time()
                )
                success = result.state.value == "COMPLETED" or cooldown_handled
            if args.json:
                _print_json(child_spawn_payload(result))
            else:
                print(
                    f"{result.attempt_id} state={result.state.value} "
                    f"conversation={result.conversation_url or '-'}"
                )
            return 0 if success else 14

        if args.command == "child-spawn-reconcile":
            attempt = registry.get_child_spawn_attempt(args.attempt_id)
            transport = ChromeUiaChildSpawnTransport(
                chrome_executable=attempt.chrome_executable,
                enabled=False,
            )
            result = reconcile_child_spawn(
                registry,
                attempt_id=args.attempt_id,
                transport=transport,
                lease_seconds=args.lease_seconds,
            )
            if args.json:
                _print_json(child_spawn_payload(result))
            else:
                print(
                    f"{result.attempt_id} state={result.state.value} "
                    f"conversation={result.conversation_url or '-'}"
                )
            return 0 if result.state.value in {"WINDOW_BOUND", "COMPLETED"} else 14

        if args.command == "child-spawn-status":
            history = registry.child_spawn_attempts(args.child_task_id, limit=args.limit)
            if args.json:
                _print_json([child_spawn_payload(attempt) for attempt in history])
            else:
                for attempt in history:
                    print(
                        f"{attempt.attempt_id} {attempt.state.value:24} "
                        f"project={attempt.project_id} conversation={attempt.conversation_url or '-'}"
                    )
            return 0

        if args.command == "child-dispatch-batch":
            if args.confirm_parent != args.parent_task_id:
                raise ChildSpawnBlocked(
                    ["--confirm-parent must exactly match the durable parent task id"]
                )
            if not args.enable_normal_browser_mutation:
                raise ChildSpawnBlocked(
                    ["normal-browser batch dispatch is disabled without explicit opt-in"]
                )
            if args.max_windows <= 0 or args.max_windows > 16:
                raise ValueError("--max-windows must be between 1 and 16")
            chrome_executable = args.chrome_executable or ChromeUiaProbe().chrome_executable
            if not chrome_executable:
                raise ChildSpawnBlocked(
                    ["Google Chrome executable is required for child batch dispatch"]
                )
            report = advance_child_dispatch_batch(
                registry,
                parent_task_id=args.parent_task_id,
                max_windows=args.max_windows,
                chrome_executable=chrome_executable,
                workspace_loader=lambda child_task_id: _refresh_workspace(
                    registry, child_task_id, workspace_probe
                ),
                transport_factory=default_transport_factory,
                lease_seconds=args.lease_seconds,
                max_evidence_age_s=args.max_evidence_age,
                close_terminal_pages=not args.keep_terminal_pages,
            )
            if args.json:
                _print_json(report.payload())
            else:
                print(
                    f"parent={report.parent_task_id} dispatched={report.dispatched_children}/"
                    f"{report.total_children} sessions={report.bound_session_children} "
                    f"pool={report.pool_windows}/{report.max_windows} pending={report.pending_children}"
                )
                for event in report.events:
                    source = (
                        f" source={event.source_child_task_id}"
                        if event.source_child_task_id else ""
                    )
                    print(
                        f"{event.child_task_id}: {event.action}; {event.detail}"
                        f"{source}"
                    )
                if report.waiting_for_binding:
                    print("waiting_lsm=" + ",".join(report.waiting_for_binding))
                if report.stopped:
                    print(f"STOP: {report.stop_reason}", file=sys.stderr)
            return 14 if report.stopped else 0

        if args.command == "replacement-register":
            state = registry.register_replacement_candidate(
                args.child_task_id,
                args.conversation_url,
                worker_id=args.worker_id,
            )
            candidate = next(
                worker for worker in state.workers if worker.conversation_ref == args.conversation_url
            )
            payload = {
                "child_task_id": args.child_task_id,
                "worker_id": candidate.worker_id,
                "status": candidate.status.value,
                "protocol_revision": state.revision,
                "conversation_url": candidate.conversation_ref,
            }
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"{candidate.worker_id} status={candidate.status.value}; "
                    "candidate registered, no execution authority granted"
                )
            return 0

        if args.command == "replacement-arm":
            lsm = _refresh_lsm(registry, args.child_task_id, adapter)
            workspace = _refresh_workspace(registry, args.child_task_id, workspace_probe)
            attempt = arm_replacement(
                registry,
                task_id=args.child_task_id,
                candidate_worker_id=args.candidate_worker_id,
                lsm=lsm,
                workspace=workspace,
                max_evidence_age_s=args.max_evidence_age,
            )
            if args.json:
                _print_json(replacement_payload(attempt))
            else:
                print(
                    f"{attempt.attempt_id} state={attempt.state.value} mode={attempt.mode}; "
                    "run replacement-submit before exactly one LSM takeover call"
                )
            return 0

        if args.command == "replacement-submit":
            attempt = submit_replacement(registry, args.attempt_id)
            payload = {
                "attempt": replacement_payload(attempt),
                "lsm_takeover_request": lsm_takeover_request(attempt),
            }
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"{attempt.attempt_id} state={attempt.state.value}; "
                    "LSM takeover is authorized exactly once; do not replay an ambiguous call"
                )
                _print_json(payload["lsm_takeover_request"])
            return 0

        if args.command == "replacement-complete":
            attempt = registry.get_replacement_attempt(args.attempt_id)
            lsm = _refresh_lsm(registry, attempt.task_id, adapter)
            workspace = _refresh_workspace(registry, attempt.task_id, workspace_probe)
            completed = complete_replacement(
                registry,
                attempt_id=args.attempt_id,
                new_active_run_id=args.new_run_id,
                lsm=lsm,
                workspace=workspace,
                lease_seconds=args.lease_seconds,
                max_evidence_age_s=args.max_evidence_age,
            )
            if args.json:
                _print_json(replacement_payload(completed))
            else:
                state = registry.load_worker_protocol(completed.task_id)
                print(
                    f"{completed.attempt_id} state={completed.state.value} "
                    f"worker={state.current_worker_id} generation={state.generation}"
                )
            return 0

        if args.command == "replacement-status":
            history = registry.replacement_attempts(args.child_task_id, limit=args.limit)
            if args.json:
                _print_json([replacement_payload(attempt) for attempt in history])
            else:
                for attempt in history:
                    print(
                        f"{attempt.attempt_id} {attempt.state.value:24} "
                        f"candidate={attempt.candidate_worker_id} mode={attempt.mode}"
                    )
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
                auto_recover_timeouts=args.auto_recover_timeouts,
                auto_continue_overruns=args.auto_continue_overruns,
                overrun_after_s=max(1.0, float(args.overrun_after)),
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
            stopped = stop_watchdog_host(
                registry,
                grace_s=max(5.0, float(args.grace)),
                wait_s=max(0.0, float(args.wait)),
            )
            if args.json:
                _print_json(asdict(stopped))
            else:
                print(stopped.detail)
            return 0 if stopped.stopped else 11

        if args.command == "watchdog-restart":
            stopped = stop_watchdog_host(
                registry,
                grace_s=max(5.0, float(args.grace)),
                wait_s=max(0.0, float(args.wait)),
            )
            if not stopped.stopped:
                payload = {"stop": asdict(stopped), "launch": None}
                if args.json:
                    _print_json(payload)
                else:
                    print(stopped.detail)
                return 11
            launch = launch_detached_watchdog(
                registry,
                repo_root=Path.cwd(),
                db_path=registry.db_path,
                interval_s=max(1.0, float(args.interval)),
                use_uia=args.uia,
                auto_recover_timeouts=args.auto_recover_timeouts,
                auto_continue_overruns=args.auto_continue_overruns,
                overrun_after_s=max(1.0, float(args.overrun_after)),
                lsm_state_dir=args.lsm_state_dir,
                git_bin=args.git_bin,
                log_path=args.log,
                ready_timeout_s=max(1.0, float(args.ready_timeout)),
            )
            payload = {"stop": asdict(stopped), "launch": asdict(launch)}
            if args.json:
                _print_json(payload)
            else:
                print(stopped.detail)
                print(
                    f"watchdog spawn_pid={launch.spawn_pid} lease_pid={launch.lease_pid} "
                    f"lease_ready={str(launch.lease_ready).lower()} log={launch.log_path}"
                )
                print(launch.detail)
            return 0 if launch.lease_ready else 10

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
                print(f"LWS doctor {report.version}: {report.overall.value}")
                for check in report.checks:
                    print(f"{check.status.value:4} {check.name:24} {check.detail}")
            return 1 if report.overall == DoctorStatus.FAIL else 0

        if args.command == "capability-import":
            payload = _read_json_file(args.file)
            if not isinstance(payload, dict):
                raise ValueError("page-close evidence JSON must be an object")
            evidence = PageCloseEvidence(**payload)
            if args.observed_at is not None:
                if evidence.observed_at is not None and abs(float(evidence.observed_at) - float(args.observed_at)) > 1e-6:
                    raise CapabilityProvenanceError(
                        "--observed-at conflicts with the timestamp already embedded in evidence"
                    )
                evidence.observed_at = float(args.observed_at)
            explicit_provenance = {
                "scope_host": args.scope_host,
                "browser_family": args.browser_family,
                "browser_major": args.browser_major,
                "platform": args.platform,
                "surface": args.surface,
            }
            for field_name, value in explicit_provenance.items():
                if value is None:
                    continue
                existing = getattr(evidence, field_name)
                if existing is not None and str(existing).lower() != str(value).lower():
                    raise CapabilityProvenanceError(
                        f"--{field_name.replace('_', '-')} conflicts with provenance already embedded in evidence"
                    )
                setattr(
                    evidence,
                    field_name,
                    int(value) if field_name == "browser_major" else str(value),
                )
            records = build_page_close_capabilities(
                evidence,
                ttl_s=max(1.0 / 60.0, float(args.ttl_hours)) * 3600.0,
            )
            saved = [registry.record_page_capability(row) for row in records]
            if args.json:
                _print_json([asdict(row) for row in saved])
            else:
                for row in saved:
                    print(
                        f"{row.capability_id} kind={row.kind.value} browser={row.browser_family}"
                        f"/{row.browser_major} expires_at={row.expires_at:.3f}"
                    )
            return 0

        if args.command == "capability-status":
            kind = PageCapabilityKind(args.kind) if args.kind else None
            rows = registry.page_capabilities(kind=kind, limit=args.limit)
            context = None
            context_error = None
            if rows:
                try:
                    context = runtime_context(
                        browser_major=_runtime_browser_major(args.browser_major)
                    )
                except CapabilityProvenanceError as exc:
                    context_error = str(exc)
            output = []
            for row in rows:
                if context is None:
                    usable = False
                    blockers = ["runtime_context_unavailable"]
                else:
                    usable, blockers = capability_matches_context(
                        row,
                        context,
                        expected_kind=row.kind,
                    )
                item = asdict(row)
                item["usable_now"] = usable
                item["blockers"] = blockers
                if context_error:
                    item["context_error"] = context_error
                output.append(item)
            if args.json:
                _print_json(output)
            elif not output:
                print("No durable page-close capabilities.")
            else:
                for item in output:
                    print(
                        f"{item['capability_id']} {item['kind']} usable={str(item['usable_now']).lower()} "
                        f"browser={item['browser_family']}/{item['browser_major']} "
                        f"expires_at={item['expires_at']:.3f}"
                    )
                    if item["blockers"]:
                        print("  blockers: " + ", ".join(item["blockers"]))
            return 0

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
            selected_page_close_capability_id = None
            if args.page_close_capability:
                context = runtime_context(
                    browser_major=_runtime_browser_major(args.browser_major)
                )
                if args.page_close_capability == "latest":
                    candidates = registry.page_capabilities(
                        kind=PageCapabilityKind.GENERATION,
                        limit=100,
                    )
                else:
                    candidates = [registry.get_page_capability(args.page_close_capability)]
                candidate_blockers = []
                for candidate in candidates:
                    usable, blockers = capability_matches_context(
                        candidate,
                        context,
                        expected_kind=PageCapabilityKind.GENERATION,
                    )
                    if usable:
                        selected_page_close_capability_id = candidate.capability_id
                        page_close_capability = True
                        break
                    candidate_blockers.append(
                        f"{candidate.capability_id}:" + ",".join(blockers)
                    )
                if not page_close_capability:
                    raise CapabilityProvenanceError(
                        "no requested durable generation capability is usable in the current context"
                        + (" (" + "; ".join(candidate_blockers) + ")" if candidate_blockers else "")
                    )
            elif args.page_close_evidence:
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
                plan_payload = asdict(plan)
                plan_payload["page_close_capability_id"] = selected_page_close_capability_id
                plan_payload["legacy_page_close_evidence_used"] = bool(args.page_close_evidence)
                _print_json(plan_payload)
            else:
                print(
                    f"memory={plan.memory_pressure} workers={plan.active_worker_count} "
                    f"pinned={plan.pinned_worker_count} "
                    f"park_candidates={plan.park_candidate_count}"
                )
                if selected_page_close_capability_id:
                    print(f"capability: {selected_page_close_capability_id}")
                elif args.page_close_evidence:
                    print("capability: legacy one-shot evidence (not durable provenance)")
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

        if args.command == "probe-op-status":
            if args.operation_id and args.latest:
                raise ValueError("operation_id and --latest are mutually exclusive")
            if args.operation_id:
                operation = registry.get_probe_mutation_operation(args.operation_id)
                selection = "exact"
            elif args.latest:
                rows = registry.probe_mutation_operations(limit=1)
                operation = rows[0] if rows else None
                selection = "latest"
            else:
                operation = registry.unresolved_probe_mutation_operation()
                selection = "unresolved"
            payload = probe_operation_payload(operation, selection=selection)
            if args.json:
                _print_json(payload)
            elif operation is None:
                print(f"probe operation: none (selection={selection})")
            else:
                print(
                    f"{operation.operation_id} kind={operation.kind.value} "
                    f"state={operation.state.value} classification={payload['classification']} "
                    f"task={operation.target_task_id} worker={operation.target_worker_id}"
                )
                if operation.last_outcome:
                    print(f"  last_outcome: {operation.last_outcome}")
                if operation.last_error:
                    print(f"  detail: {operation.last_error}")
            return 0

        if args.command == "probe-op-reconcile":
            before = registry.get_probe_mutation_operation(args.operation_id)
            before_classification = classify_probe_operation(before)
            if before_classification in {"COMPLETED", "TERMINAL"}:
                raise ValueError(
                    f"probe operation {before.operation_id} is terminal in state {before.state.value}"
                )
            observation = load_probe_reconciliation_evidence(args.file, before)
            after = registry.reconcile_probe_mutation_operation(
                before.operation_id,
                observation,
            )
            after_classification = classify_probe_operation(after)
            payload = {
                "operation_id": after.operation_id,
                "before_state": before.state.value,
                "state": after.state.value,
                "classification": after_classification,
                "last_outcome": after.last_outcome,
                "reconcile_attempts": after.reconcile_attempts,
                "completed": after_classification == "COMPLETED",
                "unresolved": after_classification in {"UNRESOLVED", "BLOCKED"},
                "blocked": after_classification == "BLOCKED",
                "operation": asdict(after),
            }
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"{after.operation_id} state={after.state.value} "
                    f"classification={after_classification} "
                    f"outcome={after.last_outcome or 'none'}"
                )
                if after_classification == "BLOCKED" and after.last_error:
                    print(f"  reconcile_required: {after.last_error}")
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

        if args.command == "dispatch-execute":
            if not args.enable_experimental_uia:
                raise DispatchDisabled(
                    "dispatch execution remains disabled unless --enable-experimental-uia is explicit"
                )
            if args.confirm_task != args.task_id:
                raise DispatchDisabled("--confirm-task must exactly match task_id")
            previous = registry.latest_reconciliation(args.task_id)
            task, browser, lsm, workspace, result = _assessment(
                registry,
                args.task_id,
                adapter,
                workspace_probe,
                ChromeUiaProbe(),
                reconcile_workspace=True,
                refresh_uia=True,
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
                    transport_enabled=True,
                ),
            )
            plan = apply_unresolved_action_gate(
                plan,
                registry.unresolved_action_attempt(task.task_id),
            )
            execution = execute_current_worker_recovery(
                registry,
                plan=plan,
                recommendation=rec,
                policy=DispatchExecutionPolicy(
                    enabled=True,
                    confirmed_task_id=args.confirm_task,
                ),
                transport_factory=lambda binding: ChromeUiaActionTransport.from_binding(
                    binding,
                    enabled=True,
                ),
            )
            registry.record_recovery_event(
                task.task_id,
                action=f"dispatch_execute:{execution.state}",
                safe_to_dispatch=execution.submitted,
                reason=execution.detail,
                payload={
                    "attempt_id": execution.attempt_id,
                    "dispatch_plan": asdict(plan),
                    "side_effect_possible": execution.side_effect_possible,
                },
            )
            if args.json:
                _print_json(asdict(execution))
            else:
                print(
                    f"attempt={execution.attempt_id} state={execution.state} "
                    f"submitted={str(execution.submitted).lower()}"
                )
                print(execution.detail)
            return 0 if execution.submitted or execution.side_effect_possible else 12

        if args.command == "action-reconcile-uia":
            attempt = registry.get_action_attempt(args.attempt_id)
            task = registry.get_task(attempt.task_id)
            _refresh_uia(registry, task.task_id, ChromeUiaProbe())
            reconciled = reconcile_action_with_uia(
                registry,
                attempt_id=attempt.attempt_id,
                observer_factory=lambda chrome: ChromeUiaAckObserver(
                    chrome_executable=chrome
                ),
            )
            if reconciled.acknowledged:
                browser_after_ack = registry.latest_browser_observation(task.current_worker_id)
                if browser_after_ack is not None and browser_after_ack.message_signature:
                    registry.record_action_ack_browser_signature(
                        attempt.attempt_id,
                        message_signature=browser_after_ack.message_signature,
                    )
            if args.json:
                _print_json(asdict(reconciled))
            else:
                print(
                    f"{reconciled.attempt_id} state={reconciled.state} "
                    f"acknowledged={str(reconciled.acknowledged).lower()}"
                )
                print(reconciled.detail)
            return 0 if reconciled.acknowledged else 9

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
            if args.auto_recover_timeouts and not args.uia:
                raise DispatchDisabled(
                    "--auto-recover-timeouts requires --uia exact-window observation"
                )
            if args.auto_continue_overruns and not args.uia:
                raise DispatchDisabled(
                    "--auto-continue-overruns requires --uia exact-window observation"
                )
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
                    overrun_results = []
                    if args.auto_continue_overruns:
                        overrun_results = _auto_continue_overrun_cycle(
                            registry,
                            adapter,
                            workspace_probe,
                            policy=OverrunContinuationPolicy(
                                enabled=True,
                                overrun_after_s=max(1.0, float(args.overrun_after)),
                                auto_discover_visible_conversations=True,
                            ),
                        )
                    overrun_side_effect = any(
                        bool(item.get("submitted")) or bool(item.get("side_effect_possible"))
                        for item in overrun_results
                    )
                    if args.auto_recover_timeouts and not overrun_side_effect:
                        _auto_recover_timeout_cycle(
                            registry,
                            adapter,
                            workspace_probe,
                            queue,
                            policy=TimeoutRecoveryPolicy(enabled=True),
                        )
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
    except ChildSpawnBlocked as exc:
        print("child spawn blocked:", file=sys.stderr)
        for blocker in exc.blockers:
            print(f"- {blocker}", file=sys.stderr)
        return 14
    except ReplacementBlocked as exc:
        print("replacement blocked:", file=sys.stderr)
        for blocker in exc.blockers:
            print(f"- {blocker}", file=sys.stderr)
        return 13
    except (DispatchDisabled, ActionBlocked, UiaActionUnavailable) as exc:
        print(f"dispatch blocked: {exc}", file=sys.stderr)
        return 12
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
