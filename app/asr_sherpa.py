"""sherpa-onnx streaming ASR backend.

Why this instead of Whisper: ``large-v3-turbo`` is a 1.6 GB general-purpose
model that runs at rtf 2.5 on a 4-core CPU. A zipformer transducer is a
purpose-built streaming ASR model — 161 MB int8, no language model, real-time
factor around 0.1 — which is the architecture phone keyboards use.

The trade is accuracy on unusual vocabulary: a large Whisper model has seen far
more text, so it handles names and jargon better. Pick this backend when CPU
time matters more than the last few points of character error rate.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger("echonote.asr_sherpa")

# Recognizer construction is expensive (162 MB of weights), so it is built
# once and shared. ``decode_lock`` serialises calls: one recognizer can serve
# many streams, but not the same stream from two threads.
_recognizer = None
_load_lock = threading.Lock()
_decode_lock = threading.Lock()

_FILES = ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")


def model_dir() -> Path:
    return Path(config.SHERPA_MODEL_DIR)


def is_installed() -> bool:
    """True when every model file is on disk."""
    directory = model_dir()
    return all((directory / name).is_file() for name in _FILES)


def load() -> bool:
    """Build the recognizer. Returns True when it is usable."""
    global _recognizer
    with _load_lock:
        if _recognizer is not None:
            return True
        if not is_installed():
            log.warning(
                "sherpa-onnx model files missing under %s; expected %s",
                model_dir(),
                ", ".join(_FILES),
            )
            return False
        try:
            import sherpa_onnx

            directory = model_dir()
            started = __import__("time").perf_counter()
            _recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(directory / "tokens.txt"),
                encoder=str(directory / "encoder.int8.onnx"),
                decoder=str(directory / "decoder.onnx"),
                joiner=str(directory / "joiner.int8.onnx"),
                num_threads=config.SHERPA_THREADS,
                sample_rate=config.SAMPLE_RATE,
                feature_dim=80,
                decoding_method="greedy_search",
                provider="cpu",
            )
        except Exception:  # noqa: BLE001 - a broken model must not kill startup
            log.exception("Failed to build the sherpa-onnx recognizer")
            _recognizer = None
            return False
        log.info(
            "sherpa-onnx recognizer ready (%s, %.0f ms)",
            model_dir().name,
            (__import__("time").perf_counter() - started) * 1000,
        )
        return True


def label() -> str:
    return "sherpa-zipformer-zh"


def transcribe(audio: np.ndarray, language: str | None = None) -> str | None:
    """Decode one buffer. ``None`` means the backend is unavailable.

    The streaming recognizer is fed the whole buffer and drained: same result
    as an offline decode, without needing a second model. Feeding it in real
    time would require keeping the stream alive across WebSocket frames, which
    is a separate change to the session loop.
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
        # ``input_finished`` tells the decoder no more audio is coming, so it
        # flushes anything still pending instead of waiting for the next chunk.
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = recognizer.get_result(stream)
    return (text or "").strip()
