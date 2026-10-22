"""Remote ASR backend for OpenAI-compatible ``/audio/transcriptions``.

Why this exists: ``large-v3-turbo`` on a 4-core CPU runs at rtf 2.48 — one
minute of audio costs two and a half minutes of a pegged CPU, and the live
and refine models compete for the same cores. A hosted Whisper on the same
audio answers in well under a second.

The cost is privacy: audio leaves the machine. That is a product decision, not
a technical one — ``config.ASR_REMOTE`` is only true when an endpoint and a key
are both configured, and the UI states which backend is active.

Design notes:

* Returns ``None`` on failure, not ``""``. An empty transcript and a failed
  request are different things: the caller falls back to the local model for
  the second, and must not treat a network blip as "no speech here".
* A circuit breaker stops a dead endpoint from costing a timeout on every
  sentence. After ``ASR_REMOTE_FAIL_THRESHOLD`` consecutive failures the
  backend reports failure immediately for ``ASR_REMOTE_COOLDOWN_S``.
"""

from __future__ import annotations

import logging
import struct
import threading
import time

import httpx
import numpy as np

from . import config

log = logging.getLogger("echonote.asr_remote")

# Circuit breaker state. Guarded by ``_breaker_lock`` because the live caption
# worker and the refine worker can hit a failing endpoint at the same time.
_breaker_lock = threading.Lock()
_fail_streak = 0
_open_until = 0.0


def enabled() -> bool:
    """True when an endpoint and a key are both configured."""
    return bool(config.ASR_REMOTE_URL and config.ASR_REMOTE_KEY)


def model_label() -> str:
    """Short label for the UI, e.g. ``remote:whisper-large-v3-turbo``."""
    return f"remote:{config.ASR_REMOTE_MODEL}"


def wav_bytes(audio: np.ndarray) -> bytes:
    """Encode float32 mono [-1, 1] at ``SAMPLE_RATE`` as a PCM16 WAV.

    The transcription endpoints take a container, not raw samples, and a WAV
    is the only format every provider accepts without a codec dependency.
    Samples are clipped before scaling: a peak above 1.0 would wrap around to
    the opposite sign and turn a loud syllable into a click.
    """
    pcm = np.clip(audio, -1.0, 1.0)
    data = (pcm * 32767.0).astype(np.int16).tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    # PCM, 1 channel, SAMPLE_RATE, byte rate, block align, 16 bits.
    header += struct.pack(
        "<IHHIIHH", 16, 1, 1, config.SAMPLE_RATE, config.SAMPLE_RATE * 2, 2, 16
    )
    header += b"data" + struct.pack("<I", len(data))
    return header + data


def _breaker_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _open_until


def _record(ok: bool) -> None:
    global _fail_streak, _open_until
    with _breaker_lock:
        if ok:
            if _fail_streak:
                log.info("Remote ASR recovered after %d failure(s)", _fail_streak)
            _fail_streak = 0
            _open_until = 0.0
            return
        _fail_streak += 1
        # Compare against ``_open_until`` directly: calling ``_breaker_open()``
        # here would take the same non-reentrant lock we already hold and
        # deadlock the worker on the first run of failures.
        now = time.monotonic()
        if _fail_streak >= config.ASR_REMOTE_FAIL_THRESHOLD and now >= _open_until:
            _open_until = now + config.ASR_REMOTE_COOLDOWN_S
            log.warning(
                "Remote ASR failing (%d in a row); falling back to local for %.0fs",
                _fail_streak,
                config.ASR_REMOTE_COOLDOWN_S,
            )


def status() -> dict:
    """Diagnostics for /api/ready."""
    with _breaker_lock:
        cooling = max(0.0, _open_until - time.monotonic())
        return {
            "remote": True,
            "remote_model": config.ASR_REMOTE_MODEL,
            "remote_fail_streak": _fail_streak,
            "remote_cooldown_s": round(cooling, 1),
        }


def transcribe(
    audio: np.ndarray,
    language: str | None = None,
    *,
    live: bool = False,
) -> str | None:
    """Transcribe one buffer remotely.

    Returns the text on success (possibly empty when the provider heard
    nothing), or ``None`` when the request failed or the breaker is open — the
    caller should then use the local model.
    """
    if not enabled():
        return None
    if _breaker_open():
        return None
    if audio is None or len(audio) == 0:
        return None

    timeout = (
        config.ASR_REMOTE_LIVE_TIMEOUT_S if live else config.ASR_REMOTE_TIMEOUT_S
    )
    data = {
        "model": config.ASR_REMOTE_MODEL,
        "response_format": "json",
        # Greedy: the same buffer must decode the same way twice, otherwise a
        # live caption can flip wording between two identical pushes.
        "temperature": "0",
    }
    if language:
        data["language"] = language
    if config.ASR_REMOTE_PROMPT:
        data["prompt"] = config.ASR_REMOTE_PROMPT

    files = {"file": ("audio.wav", wav_bytes(audio), "audio/wav")}
    headers = {"Authorization": f"Bearer {config.ASR_REMOTE_KEY}"}

    started = time.perf_counter()
    try:
        response = httpx.post(
            config.ASR_REMOTE_URL,
            headers=headers,
            data=data,
            files=files,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - any transport error means "use local"
        _record(False)
        log.warning("Remote ASR request failed: %s", exc)
        return None

    elapsed = time.perf_counter() - started
    _record(True)
    _calls.append(elapsed)
    log.debug("Remote ASR %.0f ms (%.1fs audio)", elapsed * 1000, len(audio) / config.SAMPLE_RATE)

    text = payload.get("text")
    if not isinstance(text, str):
        log.warning("Remote ASR returned no text field: %r", payload)
        return ""
    return text


# Rolling latency, surfaced as ``live_ms``/``rtf`` so the same numbers that
# described the local model now describe the remote one.
_calls: list[float] = []


def median_ms() -> int | None:
    """Median wall time of the recent remote calls, in milliseconds."""
    if not _calls:
        return None
    recent = sorted(_calls[-20:])
    return int(recent[len(recent) // 2] * 1000)


def reset() -> None:
    """Test hook: clear breaker state and latency history."""
    global _fail_streak, _open_until
    with _breaker_lock:
        _fail_streak = 0
        _open_until = 0.0
    _calls.clear()
