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

from .browser import observation_from_dom_payload, observation_from_lsm_snapshot
from .lsm import FileLsmTelemetry, UnsupportedLsmState, detect_lsm_state_dir
from .models import SupervisorState
from .recovery import recommend
from .registry import Registry
from .scheduler import attention_queue
from .watcher import WatchPolicy, assess
from .workspace import WorkspaceProbe


def default_db_path() -> Path:
    value = os.getenv("CWS_DB")
    if value:
        return Path(value)
    return Path.cwd() / ".cws" / "registry.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cws", description="ChatGPT Web task supervisor (safe V0)")
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

    watch = sub.add_parser("watch", help="scan all tasks and print only attention-worthy items")
    watch.add_argument("--json", action="store_true")
    watch.add_argument("--once", action="store_true", help="scan once instead of staying resident")
    watch.add_argument("--interval", type=float, default=30.0, help="seconds between scans")
    watch.add_argument("--browser-suspect-after", type=float, default=120.0)
    watch.add_argument("--lsm-suspect-after", type=float, default=180.0)
    watch.add_argument("--hard-stall-after", type=float, default=600.0)

    worker = sub.add_parser("add-worker", help="attach a new conversation worker lease")
    worker.add_argument("task_id")
    worker.add_argument("--conversation-url", required=True)
    worker.add_argument("--conversation-id")

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

    rec = sub.add_parser("recommend", help="print safe recovery recommendation for a task")
    rec.add_argument("task_id")
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
    policy: WatchPolicy | None = None,
    *,
    reconcile_workspace: bool = False,
):
    task = registry.get_task(task_id)
    lsm = _refresh_lsm(registry, task_id, adapter)
    browser = registry.latest_browser_observation(task.current_worker_id)
    result = assess(task, browser, lsm, policy=policy)
    workspace = None
    if reconcile_workspace or result.requires_reconcile:
        workspace = _refresh_workspace(registry, task_id, workspace_probe)
        result = assess(task, browser, lsm, workspace=workspace, policy=policy)
    if result.state != task.state and result.confidence in {"high", "medium"}:
        registry.update_state(task_id, result.state)
        task = registry.get_task(task_id)
    return task, browser, lsm, workspace, result


def _scan_attention(registry: Registry, adapter, workspace_probe: WorkspaceProbe, policy: WatchPolicy):
    assessed = []
    for task in registry.list_tasks():
        refreshed, _browser, _lsm, _workspace, result = _assessment(
            registry, task.task_id, adapter, workspace_probe, policy
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
                reconcile_workspace=True,
            )
            payload = {
                "task": asdict(task),
                "browser": asdict(browser) if browser else None,
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

        if args.command == "recommend":
            task, _browser, lsm, workspace, result = _assessment(
                registry,
                args.task_id,
                adapter,
                workspace_probe,
                reconcile_workspace=True,
            )
            rec = recommend(task, result, lsm, workspace)
            registry.record_recovery_event(
                task.task_id,
                action=rec.action,
                safe_to_dispatch=rec.safe_to_dispatch,
                reason=rec.reason,
                payload={"assessment": asdict(result), "prompt": rec.prompt},
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
                lsm_suspect_after_s=args.lsm_suspect_after,
                hard_stall_after_s=args.hard_stall_after,
            )
            interval = max(1.0, float(args.interval))
            last_signature = None
            lease_name = "default"
            lease_owner = uuid.uuid4().hex
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
                    queue = _scan_attention(registry, adapter, workspace_probe, policy)
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
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    finally:
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
