"""Local speech-to-text engine backed by faster-whisper.

The model is loaded once and shared by all sessions. Loading is
**recoverable**: a failure stores a sanitised reason, clears itself on the
next successful attempt, and can be retried without restarting the process
(PRD FR-036).

The engine is intentionally defensive at import time — ``faster_whisper`` is
imported inside :func:`load` so that API tests, linting and the version
matrix (RG-14) do not need the model stack installed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger("echonote.stt")

MAX_AUTO_RETRIES = 3
AUTO_RETRY_DELAY_S = 60.0

_model = None
_load_lock = threading.Lock()
_infer_lock = threading.Lock()
_ready = threading.Event()
_error: str | None = None
_loading = False
_pending_loads = 0
_attempts = 0
_auto_retries = 0
_last_failure_at = 0.0
_scheduler_started = False
_shutdown = threading.Event()

# Absolute paths deeper than two levels leak the user's directory layout
# into the UI (FR-020 AC2). URLs keep their host because they are useful.
_ABS_PATH = re.compile(
    r"(?:[A-Za-z]:\\(?:[^\s\\\"'<>|]+\\){2,}[^\s\\\"'<>|]+"
    r"|/(?:[\w.\-]+/){2,}[\w.\-]+)"
)
_QUERY = re.compile(r"\?[^\s\"']*")


def _basename(path: str) -> str:
    """Last path segment, for both separators, on every platform.

    ``pathlib.Path(...).name`` is wrong here: on Linux a backslash is an
    ordinary filename character, so ``Path("C:\\Users\\alice\\model.bin").name``
    returns the whole string and the username leaks into the UI. That passed
    on Windows and failed on Linux CI.
    """
    tail = re.split(r"[\\/]", path)
    return tail[-1] if tail and tail[-1] else "…"


def sanitize_error(message: object) -> str:
    """Make an exception message safe to show in the UI.

    Removes query strings, collapses absolute paths to their basename and
    caps the length. Never returns a traceback.
    """
    text = str(message or "").strip()
    if not text:
        return "未知错误"
    text = _QUERY.sub("", text)
    text = _ABS_PATH.sub(lambda match: _basename(match.group(0)), text)
    text = " ".join(text.split())
    return text[:300]


def load() -> bool:
    """Load the model once. Returns True when the model is usable.

    Safe to call from any thread; concurrent callers serialise on the lock.
    """
    global _model, _error, _loading, _attempts, _auto_retries, _last_failure_at

    with _load_lock:
        if _model is not None:
            return True

        _loading = True
        _attempts += 1
        _error = None
        try:
            from faster_whisper import WhisperModel

            log.info(
                "Loading Whisper model '%s' (this may take a while on first run)...",
                config.MODEL_NAME,
            )
            model = WhisperModel(
                config.MODEL_NAME,
                device="cpu",
                compute_type="int8",
                download_root=config.MODEL_DIR,
            )
            _model = model
            _error = None
            _auto_retries = 0
            _ready.set()
            log.info("Whisper model ready.")
            return True
        except Exception as exc:  # noqa: BLE001 - the message is the product surface
            _error = sanitize_error(exc)
            _last_failure_at = time.monotonic()
            log.exception("Failed to load Whisper model")
            return False
        finally:
            _loading = False


def retry() -> dict:
    """Manual retry entry point (``POST /api/retry``).

    Resets the automatic-retry budget so a user who fixed their environment
    gets a fresh set of attempts. Returns immediately; poll ``status()``.
    """
    global _auto_retries, _pending_loads

    if _ready.is_set():
        return status()

    _auto_retries = 0
    # Never block on _load_lock here: an in-flight load can take minutes and
    # the UI needs an immediate answer ("正在重试").
    if not _loading:
        _pending_loads += 1
        threading.Thread(target=_load_tracked, name="whisper-retry", daemon=True).start()
    return status()


def _load_tracked() -> None:
    global _pending_loads
    try:
        load()
    finally:
        _pending_loads = max(0, _pending_loads - 1)


def maybe_auto_retry() -> bool:
    """One scheduling tick of the auto-retry policy (FR-036 AC4).

    Returns True when a retry was started. Stops after
    ``MAX_AUTO_RETRIES`` consecutive failures so an offline machine does not
    spin forever; the manual button keeps working afterwards.
    """
    global _auto_retries

    if _ready.is_set() or _error is None or _loading or _pending_loads:
        return False
    if _auto_retries >= MAX_AUTO_RETRIES:
        return False
    if time.monotonic() - _last_failure_at < AUTO_RETRY_DELAY_S:
        return False

    _auto_retries += 1
    log.info("Auto-retrying model load (%d/%d)", _auto_retries, MAX_AUTO_RETRIES)
    _load_tracked()
    return True


def _retry_scheduler() -> None:
    """Background loop that calls :func:`maybe_auto_retry` every second."""
    while not _shutdown.wait(1.0):
        try:
            maybe_auto_retry()
        except Exception:  # pragma: no cover - the scheduler must never die
            log.exception("Auto-retry tick failed")


def preload() -> None:
    """Warm up the model in the background at server startup."""
    global _scheduler_started, _pending_loads

    _pending_loads += 1
    threading.Thread(target=_load_tracked, name="whisper-preload", daemon=True).start()

    if not _scheduler_started:
        _scheduler_started = True
        threading.Thread(target=_retry_scheduler, name="whisper-retry-loop", daemon=True).start()


def shutdown() -> None:
    _shutdown.set()


def is_ready() -> bool:
    return _ready.is_set()


def status() -> dict:
    attempts = _attempts
    return {
        "ready": _ready.is_set(),
        "model": config.MODEL_NAME,
        "error": _error,
        "retrying": bool(_loading or _pending_loads),
        "attempts": attempts,
        "auto_retry_exhausted": bool(_error is not None and _auto_retries >= MAX_AUTO_RETRIES),
        # Progress hint for the UI ("正在重试（第 n 次）").
        "retry_count": _auto_retries,
    }


def transcribe(audio: np.ndarray, language: str | None = None, fast: bool = False) -> str:
    """Transcribe mono float32 audio in [-1, 1] sampled at 16 kHz.

    ``fast=True`` is used for live partial captions (lower latency); the final
    transcript uses a wider beam search for better accuracy.

    Returns an empty string when the model is unavailable. Callers should
    gate on :func:`is_ready` and surface ``model_not_ready`` themselves.
    """
    if audio is None or len(audio) < config.SAMPLE_RATE * 0.2:
        return ""

    model = _model
    if model is None or not _ready.is_set():
        return ""

    lang = None if not language or language == "auto" else language
    prompt = None
    if lang == "zh":
        # Bias towards Simplified Chinese with punctuation.
        # Short, content-neutral prompt. Longer instruction prompts bias the
        # model's word choice (e.g. "实时" -> "时时") and hurt accuracy.
        prompt = "这是一段普通话的录音。"

    with _infer_lock:
        segments, _info = model.transcribe(
            audio,
            language=lang,
            beam_size=1 if fast else 5,
            best_of=1,
            temperature=0.0,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=prompt,
        )
        text = "".join(segment.text for segment in segments).strip()

    return _clean(text)


def _clean(text: str) -> str:
    # Whisper occasionally emits bracketed sound annotations; drop them.
    if text in ("[BLANK_AUDIO]", "(空白音频)", "[Silence]", "谢谢观看", "谢谢大家观看"):
        return ""
    return text
