from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from .models import BrowserObservation

_ERROR_PATTERNS = (
    "message delivery timed out",
    "error in message stream",
    "there was an error generating a response",
    "something went wrong",
)
_STOP_LABELS = (
    "stop generating",
    "stop response",
    "stop streaming",
    "停止生成",
)
_SEND_LABELS = (
    "send",
    "send prompt",
    "send message",
    "发送",
    "发送消息",
)

# This contract is intentionally transport-neutral. A future direct Playwright/CDP
# adapter can evaluate an equivalent script; an LSM browser_run_script probe can emit
# the same payload. The important property is that text_tail is taken from the END of
# the body, unlike LSM v4.0.1's bounded snapshot body prefix.
WEB_CHAT_DOM_PROBE_JS = r"""() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const bodyText = document.body?.innerText || '';
  const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
    .filter(visible)
    .slice(-200)
    .map((el) => ({
      text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 300),
      disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
      testid: el.getAttribute('data-testid'),
    }));
  return {
    url: location.href,
    text_tail: bodyText.slice(-30000),
    text_length: bodyText.length,
    buttons,
  };
}"""


def observation_from_dom_payload(
    worker_id: str,
    payload: dict[str, Any],
    *,
    previous: BrowserObservation | None = None,
) -> BrowserObservation:
    """Normalize a DOM probe payload into deterministic supervisor telemetry.

    V0 accepts observations rather than owning an execution transport. A Local Shell
    browser script, a direct Playwright/CDP probe, or tests can emit this shape. If the
    producer supplies a stable message signature (or reliable tail text), LWS derives
    DOM-silence time across observations instead of trusting the web chat Send/Stop button.
    """
    observed_at = float(payload.get("observed_at") or time.time())
    signature = _optional_text(payload.get("message_signature"))
    signature_reliable = payload.get("signature_reliable") is not False
    if signature is None and signature_reliable:
        text_tail = _optional_text(payload.get("text_tail"))
        if text_tail is not None:
            signature = _digest_signature(text_tail, payload.get("buttons"))

    explicit_change = _optional_float(payload.get("last_dom_change_at"))
    if explicit_change is not None:
        last_dom_change_at = explicit_change
    elif signature is None:
        last_dom_change_at = None
    elif previous and previous.message_signature == signature:
        last_dom_change_at = previous.last_dom_change_at or previous.observed_at
    else:
        last_dom_change_at = observed_at

    return BrowserObservation(
        worker_id=worker_id,
        observed_at=observed_at,
        url=payload.get("url"),
        generating=_optional_bool(payload.get("generating")),
        send_button_ready=_optional_bool(payload.get("send_button_ready")),
        pending_tool_calls=_optional_int(payload.get("pending_tool_calls")),
        visible_error=_optional_text(payload.get("visible_error")),
        last_dom_change_at=last_dom_change_at,
        message_signature=signature,
        raw=dict(payload.get("raw") or {}),
    )


def dom_payload_from_lsm_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Convert an LSM v4.0.1 high-level browser snapshot to the V0 DOM contract.

    LSM snapshots truncate body text from the *front*. When `text_truncated` is true,
    that text is not a reliable latest-message heartbeat, so this adapter refuses to
    derive a message signature from it. Interactive button state and any visible error
    present in the captured prefix remain weak UI evidence.
    """
    data = snapshot
    if isinstance(snapshot.get("data"), dict) and "url" not in snapshot:
        data = snapshot["data"]
    text = str(data.get("text") or "")
    elements = [item for item in (data.get("interactive_elements") or []) if isinstance(item, dict)]
    button_rows = [
        {
            "text": str(item.get("text") or ""),
            "disabled": bool(item.get("disabled")),
            "tag": item.get("tag"),
            "role": item.get("role"),
            "type": item.get("type"),
        }
        for item in elements
        if item.get("tag") == "button" or item.get("role") == "button"
    ]
    generating = _infer_generating(button_rows)
    send_ready = _infer_send_ready(button_rows)
    error = _find_visible_error(text)
    truncated = bool(data.get("text_truncated"))
    payload: dict[str, Any] = {
        "url": data.get("url"),
        "generating": generating,
        "send_button_ready": send_ready,
        "pending_tool_calls": None,
        "visible_error": error,
        "buttons": button_rows,
        "signature_reliable": not truncated,
        "raw": {
            "source": "lsm_browser_snapshot",
            "page_id": data.get("page_id"),
            "text_truncated": truncated,
            "snapshot_error_count": len(data.get("errors") or []),
            "snapshot_network_event_count": len(data.get("network") or []),
        },
    }
    if not truncated:
        payload["text_tail"] = text
    return payload


def observation_from_lsm_snapshot(
    worker_id: str,
    snapshot: dict[str, Any],
    *,
    previous: BrowserObservation | None = None,
) -> BrowserObservation:
    return observation_from_dom_payload(
        worker_id,
        dom_payload_from_lsm_snapshot(snapshot),
        previous=previous,
    )


def _infer_generating(buttons: list[dict[str, Any]]) -> bool | None:
    labels = [_normalized_label(row.get("text")) for row in buttons]
    if any(any(marker in label for marker in _STOP_LABELS) for label in labels if label):
        return True
    # Absence of a stop control is weak evidence; do not assert False from a generic snapshot.
    return None


def _infer_send_ready(buttons: list[dict[str, Any]]) -> bool | None:
    for row in buttons:
        label = _normalized_label(row.get("text"))
        if not label:
            continue
        if any(label == marker or label.startswith(marker + " ") for marker in _SEND_LABELS):
            return not bool(row.get("disabled"))
    return None


def _find_visible_error(text: str) -> str | None:
    lowered = text.lower()
    for pattern in _ERROR_PATTERNS:
        index = lowered.rfind(pattern)
        if index >= 0:
            return text[index : index + max(160, len(pattern))].splitlines()[0].strip()
    return None


def _normalized_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _digest_signature(text_tail: str, buttons: Any = None) -> str:
    normalized_text = re.sub(r"\s+", " ", text_tail).strip()
    canonical_buttons = json.dumps(buttons or [], ensure_ascii=False, sort_keys=True, default=str)
    material = (normalized_text + "\n" + canonical_buttons).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
