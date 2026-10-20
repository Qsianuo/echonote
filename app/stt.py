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
from collections import deque
from pathlib import Path

import numpy as np

from . import config, models

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
# Bumped on every switch so an in-flight load cannot overwrite a newer choice.
_generation = 0
_loaded_key: str | None = None
# Set when the failure needs a human (model not on disk while offline).
# Auto-retry would just repeat the same certain failure forever.
_retry_blocked = False
# Last few final-pass real-time factors. Median, not mean: one slow utterance
# caused by a background build should not relabel the model as unusable.
_rtf_samples: deque[float] = deque(maxlen=5)


def rtf() -> float | None:
    """Median real-time factor of recent finals, or None before the first."""
    if not _rtf_samples:
        return None
    values = sorted(_rtf_samples)
    return round(values[len(values) // 2], 2)

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
    global _loaded_key, _retry_blocked

    generation = _generation
    with _load_lock:
        if _model is not None:
            return True

        _loading = True
        _attempts += 1
        _error = None
        try:
            target = config.model_target()
            if target.path is None:
                # No usable model on disk. When offline we refuse immediately:
                # blocking for minutes on a download that cannot succeed is
                # the worst possible failure mode for an air-gapped machine.
                hint = models.missing_hint(config.MODEL_DIR, target)
                if config.offline_mode():
                    _error = hint
                    _last_failure_at = time.monotonic()
                    # Nothing changes until a human moves files, so stop the
                    # auto-retry loop from burning a tick every 60 s forever.
                    _retry_blocked = True
                    log.warning("Offline mode with no local model: %s", hint)
                    return False
                log.info("Model %s not on disk, one-time download allowed", target.key)

            # Blocks any accidental hub call from inside faster-whisper. Set
            # before the import so the library picks it up at module load.
            models.apply_offline_env()

            from faster_whisper import WhisperModel

            log.info(
                "Loading Whisper model '%s' (%s, %s)...",
                target.key,
                target.label,
                target.source,
            )
            model = WhisperModel(
                target.loader_value,
                device="cpu",
                compute_type="int8",
                download_root=config.MODEL_DIR,
                local_files_only=target.path is not None,
                cpu_threads=config.ASR_CPU_THREADS,
            )
            if generation != _generation:
                # A switch happened while this load was running; the newer
                # request owns the output, so do not resurrect the old model.
                log.info("Discarding stale model load (superseded by a switch)")
                return False

            _model = model
            _loaded_key = target.key
            _error = None
            _auto_retries = 0
            _ready.set()
            log.info("Whisper model ready: %s", target.key)
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
    global _auto_retries, _pending_loads, _retry_blocked

    if _ready.is_set():
        return status()

    _auto_retries = 0
    # A human asked, so allow the attempt even if the automatic policy gave up.
    _retry_blocked = False
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


def switch(model_key: str) -> dict:
    """Swap to another **installed** model without restarting the process.

    Switching from the UI must never start a download: someone in the middle
    of a meeting should not discover a 1.6 GB pull. Missing models are
    rejected with the folder they should be placed in.
    """
    global _model, _error, _pending_loads, _generation, _retry_blocked

    target = models.resolve(config.MODEL_DIR, model_key)
    if target.path is None:
        return {"ok": False, "error": models.missing_hint(config.MODEL_DIR, target)}

    current = config.model_target()
    if target.path == current.path and _ready.is_set() and _loaded_key == target.key:
        return {"ok": True, "switching": False, "key": target.key}

    # No lock here: an in-flight load can hold it for minutes, and the HTTP
    # request must answer immediately. The generation counter makes the old
    # load discard its own result instead.
    _generation += 1
    config.MODEL_CHOICE = model_key
    _model = None
    _ready.clear()
    _error = None
    _retry_blocked = False
    _pending_loads += 1
    threading.Thread(target=_load_tracked, name="whisper-switch", daemon=True).start()
    log.info("Switching model to %s", target.key)
    return {"ok": True, "switching": True, "key": target.key}


def maybe_auto_retry() -> bool:
    """One scheduling tick of the auto-retry policy (FR-036 AC4).

    Returns True when a retry was started. Stops after
    ``MAX_AUTO_RETRIES`` consecutive failures so an offline machine does not
    spin forever; the manual button keeps working afterwards.
    """
    global _auto_retries

    if _ready.is_set() or _error is None or _loading or _pending_loads:
        return False
    if _retry_blocked:
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
    target = config.model_target()
    return {
        "ready": _ready.is_set(),
        "model": target.loader_value,
        "error": _error,
        "retrying": bool(_loading or _pending_loads),
        "attempts": attempts,
        "auto_retry_exhausted": bool(_error is not None and _auto_retries >= MAX_AUTO_RETRIES),
        # Progress hint for the UI ("正在重试（第 n 次）").
        "retry_count": _auto_retries,
        "model_label": target.label,
        "model_key": target.key,
        # True means the weights sit on this disk, so recognition works with
        # the network unplugged (FR-046 AC1).
        "offline_ready": target.path is not None,
        # Measured, not estimated: see :func:`rtf`.
        "rtf": rtf(),
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
        # Content-neutral: a prompt with real sentences leaks its own wording
        # into the transcript, which is why this is an instruction, not an
        # example sentence.
        prompt = config.ASR_PROMPT_ZH or None

    with _infer_lock:
        started = time.perf_counter()
        segments, _info = model.transcribe(
            audio,
            language=lang,
            # Partials are thrown away as soon as the final lands, so they stay
            # greedy. The final pass pays for beam search because that is where
            # repeated / truncated Chinese sentences actually get fixed.
            beam_size=1 if fast else config.ASR_BEAM_SIZE,
            best_of=1 if fast else config.ASR_BEST_OF,
            patience=1.0 if fast else config.ASR_PATIENCE,
            temperature=0.0 if fast else config.ASR_TEMPERATURES,
            vad_filter=False,
            condition_on_previous_text=False if fast else config.ASR_CONDITION_ON_PREVIOUS,
            without_timestamps=True,
            initial_prompt=prompt,
            no_speech_threshold=config.ASR_NO_SPEECH_THRESHOLD,
            log_prob_threshold=config.ASR_LOG_PROB_THRESHOLD,
            compression_ratio_threshold=config.ASR_COMPRESSION_RATIO_THRESHOLD,
        )
        text = "".join(segment.text for segment in segments).strip()
        elapsed = time.perf_counter() - started

    if not fast:
        # Real-time factor of the final pass, measured on real audio. The UI
        # needs it to tell the truth when a bigger model cannot keep up on a
        # 4-core laptop, instead of the user guessing why captions lag.
        duration = len(audio) / config.SAMPLE_RATE
        if duration > 0:
            _rtf_samples.append(elapsed / duration)

    return _clean(text)


def _clean(text: str) -> str:
    # Whisper occasionally emits bracketed sound annotations; drop them.
    if text in ("[BLANK_AUDIO]", "(空白音频)", "[Silence]", "谢谢观看", "谢谢大家观看"):
        return ""
    return text
