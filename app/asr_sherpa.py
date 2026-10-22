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
#
# Two recognisers exist because hotwords and greedy search are incompatible:
# ``create_stream(hotwords=...)`` on a greedy decoder calls an interface the
# greedy decoder does not implement and aborts the process (sherpa-onnx
# ``online-transducer-decoder.h``: "This interface is for
# OnlineTransducerModifiedBeamSearchDecoder."). So the beam-search decoder is
# built lazily, and only when a usable hotword actually exists.
_recognizer = None
_beam_recognizer = None
_load_lock = threading.Lock()
_decode_lock = threading.Lock()

_FILES = ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")

# U+FFFD in the output means the decoder emitted a byte sequence that is not
# valid UTF-8 (the byte-fallback path can produce one) and the C++ layer
# replaced it with the replacement character. It carries no information and
# looks like corruption in the transcript, so it is stripped.
_REPLACEMENT = "\ufffd"

# A hotword is only usable when every one of its characters exists in the
# model vocabulary. The vocabulary of this model holds bare CJK characters
# (no multi-character Chinese words), so anything else -- an ASCII letter,
# a digit, a punctuation mark, or a rare surname -- fails to encode, and the
# sherpa C++ side then drops the **whole** hotword string
# ("Encode hotwords failed, skipping"). Filtering here keeps one bad
# character from silently disabling the rest of the glossary.
_vocab: set[str] | None = None
_vocab_lock = threading.Lock()


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
            _recognizer = _build("greedy_search")
        except Exception:  # noqa: BLE001 - a broken model must not kill startup
            log.exception("Failed to build the sherpa-onnx recognizer")
            _recognizer = None
            return False
        return True


def label() -> str:
    return "sherpa-zipformer-zh"


# --------------------------------------------------------------------------
# Vocabulary and hotwords
# --------------------------------------------------------------------------


def vocabulary() -> set[str]:
    """The set of tokens the model can emit.

    Parsed from ``tokens.txt`` in the model directory. The format is one
    ``token id`` pair per line, not one bare token per line -- reading it as
    bare tokens makes every character look out-of-vocabulary.
    """
    global _vocab
    with _vocab_lock:
        if _vocab is not None:
            return _vocab
        tokens: set[str] = set()
        path = model_dir() / "tokens.txt"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                token, _, _id = line.rpartition(" ")
                if not token:
                    token = line
                # Byte-fallback entries (<0x00>..<0xFF>) are not spellable
                # text; they exist so unknown bytes can survive a round trip.
                if token.startswith("<0x") and token.endswith(">"):
                    continue
                tokens.add(token)
        except OSError:
            log.warning("Cannot read %s; hotwords will be ignored", path)
        _vocab = tokens
        return _vocab


def _spellable(text: str, vocab: set[str]) -> bool:
    """True when every character of ``text`` has a token in the vocabulary."""
    if not text:
        return False
    for char in text:
        if char.isspace():
            continue
        if char in vocab or ("\u2581" + char) in vocab:
            continue
        return False
    return True


def usable_hotwords(hotwords: str | None) -> list[str]:
    """Hotwords that this model can actually encode, one string per word.

    Anything the vocabulary cannot spell is dropped: the C++ layer refuses to
    encode an unknown token and then discards the *entire* hotword string, so
    passing one unspellable word would silently disable all of them.
    """
    if not hotwords:
        return []
    vocab = vocabulary()
    if not vocab:
        return []
    out: list[str] = []
    for word in hotwords.split():
        if _spellable(word, vocab):
            out.append(word)
    return out


def _clean(text: str) -> str:
    """Remove the replacement characters the byte path can produce."""
    return text.replace(_REPLACEMENT, "")


def _build(decoding_method: str):
    import sherpa_onnx

    directory = model_dir()
    started = __import__("time").perf_counter()
    recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(directory / "tokens.txt"),
        encoder=str(directory / "encoder.int8.onnx"),
        decoder=str(directory / "decoder.onnx"),
        joiner=str(directory / "joiner.int8.onnx"),
        num_threads=config.SHERPA_THREADS,
        sample_rate=config.SAMPLE_RATE,
        feature_dim=80,
        decoding_method=decoding_method,
        provider="cpu",
    )
    log.info(
        "sherpa-onnx recognizer ready (%s, %s, %.0f ms)",
        directory.name,
        decoding_method,
        (__import__("time").perf_counter() - started) * 1000,
    )
    return recognizer


def _beam_recognizer_or_none():
    """The modified-beam-search recogniser, built on first use.

    Hotwords are only implemented for this decoder. Building it costs another
    162 MB of weights, so it is only created when a usable hotword exists.
    """
    global _beam_recognizer
    if _beam_recognizer is not None:
        return _beam_recognizer
    with _load_lock:
        if _beam_recognizer is None:
            try:
                _beam_recognizer = _build("modified_beam_search")
            except Exception:  # noqa: BLE001 - fall back to plain decoding
                log.exception("Failed to build the beam-search recognizer")
                _beam_recognizer = None
    return _beam_recognizer


def transcribe(
    audio: np.ndarray,
    language: str | None = None,
    hotwords: str | None = None,
) -> str | None:
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

    # Hotwords need the beam-search decoder; the greedy decoder aborts the
    # process when a stream carries them. The beam recogniser is only built
    # when at least one hotword is spellable by this vocabulary.
    words = usable_hotwords(hotwords or config.HOTWORDS)
    hotword_arg = None
    if words:
        beam = _beam_recognizer_or_none()
        if beam is not None:
            recognizer = beam
            hotword_arg = " ".join(words)

    with _decode_lock:
        stream = recognizer.create_stream(hotwords=hotword_arg) if hotword_arg else recognizer.create_stream()
        stream.accept_waveform(config.SAMPLE_RATE, audio.astype(np.float32))
        # ``input_finished`` tells the decoder no more audio is coming, so it
        # flushes anything still pending instead of waiting for the next chunk.
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = recognizer.get_result(stream)
        if isinstance(text, str):
            raw = text
        else:  # newer builds return an object
            raw = getattr(text, "text", "") or ""
    return _clean(raw).strip()
