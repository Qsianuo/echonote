"""Local speech-to-text engine backed by faster-whisper.

The model is loaded once (lazily / at startup) and shared by all sessions.
faster-whisper runs fully offline after the first model download.
"""

import logging
import threading

import numpy as np

from . import config

log = logging.getLogger("echonote.stt")

_model = None
_load_lock = threading.Lock()
_infer_lock = threading.Lock()
_ready = threading.Event()
_error: str | None = None


def _load() -> None:
    global _model, _error
    with _load_lock:
        if _model is not None:
            return
        try:
            from faster_whisper import WhisperModel

            log.info("Loading Whisper model '%s' (this may take a while on first run)...",
                     config.MODEL_NAME)
            _model = WhisperModel(
                config.MODEL_NAME,
                device="cpu",
                compute_type="int8",
                download_root=config.MODEL_DIR,
            )
            log.info("Whisper model ready.")
            _ready.set()
        except Exception as exc:  # noqa: BLE001 - surface the message to the UI
            _error = str(exc)
            log.exception("Failed to load Whisper model")


def preload() -> None:
    """Warm up the model in the background at server startup."""
    threading.Thread(target=_load, name="whisper-preload", daemon=True).start()


def status() -> dict:
    return {
        "ready": _ready.is_set(),
        "model": config.MODEL_NAME,
        "error": _error,
    }


def transcribe(audio: np.ndarray, language: str | None = None, fast: bool = False) -> str:
    """Transcribe mono float32 audio in [-1, 1] sampled at 16 kHz.

    ``fast=True`` is used for live partial captions (lower latency); the final
    transcript uses a wider beam search for better accuracy.
    """
    if audio is None or len(audio) < config.SAMPLE_RATE * 0.2:
        return ""

    _load()  # blocks until the model is available
    if _error:
        return ""

    lang = None if not language or language == "auto" else language
    prompt = None
    if lang == "zh":
        # Bias towards Simplified Chinese with punctuation.
        # Short, content-neutral prompt. Longer instruction prompts bias the
        # model's word choice (e.g. "实时" -> "时时") and hurt accuracy.
        prompt = "这是一段普通话的录音。"

    with _infer_lock:
        segments, _info = _model.transcribe(
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
