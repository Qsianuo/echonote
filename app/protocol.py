"""EchoNote wire protocol v3.

Every message the server emits is built here so the contract lives in one
place and can be asserted by the contract tests (PRD RG-04). The client is
allowed to receive unknown *warning* codes without crashing, but unknown
*error* codes are rejected here so we never ship a typo as a new code.

v2 -> v3
--------
``final`` is no longer necessarily the last word on an utterance: the live
caption model emits it the moment the pause is detected, and the accurate
model may replace it a few seconds later with a ``refine`` message that
carries the same ``id``. Clients that do not understand ``refine`` still show
the provisional text, so the change is additive in spirit — but the message
set changed, so the version string did too.
"""

from __future__ import annotations

import re
from typing import Any

PROTOCOL_VERSION = "3"

ERROR_CODES = frozenset(
    {
        "frame_too_large",
        "busy",
        "model_not_ready",
        "unsupported_protocol",
    }
)

WARNING_CODES = frozenset({"audio_backlog_dropped", "refine_backlog_dropped"})

# ``final.id`` is "epoch-sequence"; both start at 1 so the id is unique for
# the whole page lifetime (PRD FR-010/FR-040).
_FINAL_ID = re.compile(r"^[1-9]\d*-[1-9]\d*$")


class ProtocolError(ValueError):
    """Raised when an outgoing message would violate the protocol."""


def _number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{field} must be a number") from exc
    if number < 0:
        raise ProtocolError(f"{field} must be >= 0")
    return number


def status(
    *,
    ready: bool,
    model: str,
    error: str | None = None,
    retrying: bool = False,
    attempts: int = 0,
    auto_retry_exhausted: bool = False,
    retry_count: int = 0,
    model_label: str = "",
    model_key: str = "",
    offline_ready: bool = False,
    rtf: float | None = None,
    live_model: str = "",
    live_model_key: str = "",
    live_model_label: str = "",
    live_separate: bool = False,
    live_ready: bool = True,
    live_error: str | None = None,
    refine: bool = False,
    live_ms: int | None = None,
) -> dict:
    return {
        "type": "status",
        "protocol": PROTOCOL_VERSION,
        "ready": bool(ready),
        "model": str(model),
        "error": error if error is None else str(error),
        "retrying": bool(retrying),
        "attempts": int(attempts),
        "auto_retry_exhausted": bool(auto_retry_exhausted),
        "retry_count": int(retry_count),
        # Which model is actually loaded, and whether it can run with the
        # network unplugged (FR-046). The UI shows this instead of guessing
        # from a path basename.
        "model_label": str(model_label),
        "model_key": str(model_key),
        "offline_ready": bool(offline_ready),
        # Real-time factor of the last finals. >1 means captions fall behind.
        "rtf": None if rtf is None else round(float(rtf), 2),
        # Live caption role (v3). ``live_separate`` is what makes the "边说边
        # 出字" promise honest: it is True only when a second, faster model is
        # doing the live decode.
        "live_model": str(live_model),
        "live_model_key": str(live_model_key),
        "live_model_label": str(live_model_label),
        "live_separate": bool(live_separate),
        "live_ready": bool(live_ready),
        "live_error": live_error if live_error is None else str(live_error),
        "refine": bool(refine),
        # Median live decode time in ms — the number the user feels.
        "live_ms": None if live_ms is None else int(live_ms),
    }


def partial(*, text: str, epoch: int, start: float) -> dict:
    return {
        "type": "partial",
        "text": str(text or ""),
        "epoch": int(epoch),
        "start": round(_number(start, "start"), 2),
    }


def final(
    *,
    final_id: str,
    epoch: int,
    text: str,
    start: float,
    end: float,
    stable: bool = True,
) -> dict:
    """A completed utterance.

    ``stable=False`` means a more accurate pass is still running and may
    replace this text with :func:`refine` carrying the same ``id``.
    """
    return {
        "type": "final",
        "id": str(final_id),
        "epoch": int(epoch),
        "text": str(text),
        "start": round(_number(start, "start"), 2),
        "end": round(_number(end, "end"), 2),
        "stable": bool(stable),
    }


def refine(*, final_id: str, epoch: int, text: str, start: float, end: float) -> dict:
    """Replace the text of an earlier ``final`` (same ``id``).

    Only sent for finals that were emitted with ``stable=False``.
    """
    return {
        "type": "refine",
        "id": str(final_id),
        "epoch": int(epoch),
        "text": str(text),
        "start": round(_number(start, "start"), 2),
        "end": round(_number(end, "end"), 2),
    }


def error(*, code: str, message: str, retryable: bool = False) -> dict:
    if code not in ERROR_CODES:
        raise ProtocolError(f"unknown error code: {code}")
    return {"type": "error", "code": code, "message": str(message), "retryable": bool(retryable)}


def warning(*, code: str, dropped_ms: int = 0) -> dict:
    if code not in WARNING_CODES:
        raise ProtocolError(f"unknown warning code: {code}")
    return {"type": "warning", "code": str(code), "dropped_ms": int(dropped_ms)}


def done() -> dict:
    return {"type": "done"}


# --------------------------------------------------------------------------
# Validation (used by the contract tests and by hand-written fixtures)
# --------------------------------------------------------------------------

_REQUIRED: dict[str, dict[str, type | tuple[type, ...]]] = {
    "status": {
        "protocol": str,
        "ready": bool,
        "model": str,
        "retrying": bool,
    },
    "partial": {"text": str, "epoch": int, "start": (int, float)},
    "final": {
        "id": str,
        "epoch": int,
        "text": str,
        "start": (int, float),
        "end": (int, float),
        "stable": bool,
    },
    "refine": {
        "id": str,
        "epoch": int,
        "text": str,
        "start": (int, float),
        "end": (int, float),
    },
    "error": {"code": str, "message": str, "retryable": bool},
    "warning": {"code": str, "dropped_ms": int},
    "done": {},
}


def validate(message: dict) -> dict:
    """Assert that ``message`` is a valid server message."""
    if not isinstance(message, dict):
        raise ProtocolError("message must be an object")

    kind = message.get("type")
    if kind not in _REQUIRED:
        raise ProtocolError(f"unknown message type: {kind!r}")

    for field, expected in _REQUIRED[kind].items():
        if field not in message:
            raise ProtocolError(f"{kind} is missing {field!r}")
        # bool is a subclass of int; reject it where a number is expected.
        if isinstance(message[field], bool) and expected is not bool:
            raise ProtocolError(f"{kind}.{field} must not be a bool")
        if not isinstance(message[field], expected):
            raise ProtocolError(f"{kind}.{field} has the wrong type")

    if kind == "status":
        if message["protocol"] != PROTOCOL_VERSION:
            raise ProtocolError("status.protocol must equal the current protocol version")
    elif kind == "partial":
        if message["epoch"] < 1:
            raise ProtocolError("partial.epoch must be >= 1")
    elif kind in ("final", "refine"):
        if not _FINAL_ID.match(message["id"]):
            raise ProtocolError(f"{kind}.id must be 'epoch-sequence'")
        if int(message["id"].split("-")[0]) != message["epoch"]:
            raise ProtocolError(f"{kind}.id prefix must match {kind}.epoch")
        if not message["text"]:
            raise ProtocolError(f"{kind}.text must not be empty")
        if _number(message["end"], "end") < _number(message["start"], "start"):
            raise ProtocolError(f"{kind}.end must be >= {kind}.start")
    elif kind == "error":
        if message["code"] not in ERROR_CODES:
            raise ProtocolError(f"unknown error code: {message['code']}")
    elif kind == "warning":
        if message["code"] not in WARNING_CODES:
            raise ProtocolError(f"unknown warning code: {message['code']}")
        if message["dropped_ms"] < 0:
            raise ProtocolError("warning.dropped_ms must be >= 0")

    return message
