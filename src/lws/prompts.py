from __future__ import annotations

SUPERVISOR_SAFETY_PREAMBLE = """This is a reliability/recovery task for a user-authorized web chat + Local Shell MCP workflow.
Work read-only first. Do not bypass authentication or platform controls. Do not access, copy, or move cookies, tokens, passwords, session secrets, or private API credentials. Do not call, reconstruct, or automate private web chat backend endpoints. Do not click or type in unrelated browser tabs.

Before any action with side effects, reconcile the durable Local Shell logical session, Goal plan, tracked jobs/in-flight leases, and actual workspace/Git state. The previous web chat message may have failed after side effects already completed, so never blindly replay the prior operation. If evidence is ambiguous, stop at a recommendation or request human review rather than guessing.
"""


def with_safety_preamble(instruction: str) -> str:
    return SUPERVISOR_SAFETY_PREAMBLE.rstrip() + "\n\n" + instruction.lstrip()
