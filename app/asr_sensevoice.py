"""sherpa-onnx offline SenseVoice backend.

Why a second sherpa-onnx model when the zipformer already exists: the
zipformer is a *streaming* transducer — rtf 0.1, with accuracy traded away for
latency. SenseVoice-small is the opposite trade. It decodes a finished
utterance in one non-autoregressive pass, its Chinese accuracy is the reason
the FunASR family keeps coming up as the "Whisper 中文不行" alternative, and
int8 it costs about 230 MB. What it cannot do is stream: it needs the whole
utterance. So it takes the *final* role only — the live caption stays on
whatever ``stt.live_backend()`` picks, the same split the remote backend uses,
minus the network.

Model: ``sherpa-onnx-sense-voice-zh-en-ja-ko-yue`` (int8), fetched with
``python tools/fetch_model.py --model sensevoice``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from . import config, models

log = logging.getLogger("echonote.asr_sensevoice")

# One recognizer for the process: ~230 MB of weights, built once.
# ``_decode_lock`` serialises calls the same way ``asr_sherpa`` does.
_recognizer = None
_load_lock = threading.Lock()
_decode_lock = threading.Lock()

_FILES = models.SENSEVOICE_FILES


def model_dir() -> Path:
    return Path(config.SENSEVOICE_MODEL_DIR)


def is_installed() -> bool:
    """True when both model files are on disk."""
    directory = model_dir()
    return all((directory / name).is_file() for name in _FILES)


def is_loaded() -> bool:
    return _recognizer is not None


def label() -> str:
    return "sensevoice-small"


def load() -> bool:
    """Build the offline recognizer. Returns True when it is usable."""
    global _recognizer
    with _load_lock:
        if _recognizer is not None:
            return True
        directory = model_dir()
        if not is_installed():
            log.warning(
                "SenseVoice model files missing under %s; expected %s",
                directory,
                ", ".join(_FILES),
            )
            return False
        try:
            import sherpa_onnx

            started = time.perf_counter()
            _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(directory / "model.int8.onnx"),
                tokens=str(directory / "tokens.txt"),
                num_threads=config.SENSEVOICE_THREADS,
                sample_rate=config.SAMPLE_RATE,
                # Empty means the model decides per utterance. SenseVoice only
                # knows zh/en/ja/ko/yue, so for a language it cannot spell
                # (the UI also offers fr/de/es/ru) auto detection beats a
                # fixed wrong choice.
                language="",
                use_itn=config.SENSEVOICE_USE_ITN,
                provider="cpu",
            )
        except Exception:  # noqa: BLE001 - a broken model must not kill startup
            log.exception("Failed to build the SenseVoice recognizer")
            _recognizer = None
            return False
        log.info(
            "SenseVoice recognizer ready (%s, %.0f ms)",
            directory.name,
            (time.perf_counter() - started) * 1000,
        )
        return True


def transcribe(
    audio: np.ndarray,
    language: str | None = None,
    hotwords: str | None = None,
) -> str | None:
    """Decode one finished buffer. ``None`` means the backend is unavailable.

    ``language`` and ``hotwords`` are accepted for signature compatibility
    with the other backends but not applied: the onnx build fixes the
    language at construction (here: auto), and sherpa-onnx implements no
    hotword biasing for SenseVoice. The glossary keeps working on the live
    path, which is the path that runs while the user watches.
    """
    if audio is None or len(audio) == 0:
        return None
    if not load():
        return None

    recognizer = _recognizer
    if recognizer is None:
        return None

    with _decode_lock:
        stream = recognizer.create_stream()
        stream.accept_waveform(config.SAMPLE_RATE, audio.astype(np.float32))
        recognizer.decode_stream(stream)
        result = stream.result

    return (getattr(result, "text", "") or "").strip()
