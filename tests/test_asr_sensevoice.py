"""SenseVoice backend: file checks, engine selection, dispatch.

The model itself is ~230 MB, so these tests never build it — they cover the
decision logic around it, which is what decides whether finals run on
SenseVoice, which engine draws the caption, and whether a missing model
surfaces as a clear error instead of silence.
"""

from __future__ import annotations

import numpy as np
import pytest

from app import asr_sensevoice, asr_sherpa, config, stt


@pytest.fixture
def temp_model_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SENSEVOICE_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(asr_sensevoice, "_recognizer", None)
    return tmp_path


def test_missing_files_are_reported_as_not_installed(temp_model_dir):
    assert asr_sensevoice.is_installed() is False


def test_a_partial_download_is_not_installed(temp_model_dir):
    """Tokens without weights is the shape of an interrupted download.

    Treating it as installed would fail at recognizer-build time with an
    opaque ONNX error instead of a message naming the missing file.
    """
    (temp_model_dir / "tokens.txt").write_text("你 1\n", encoding="utf-8")
    assert asr_sensevoice.is_installed() is False

    (temp_model_dir / "model.int8.onnx").write_bytes(b"stub")
    assert asr_sensevoice.is_installed() is True


def test_load_returns_false_when_files_are_missing(temp_model_dir):
    assert asr_sensevoice.load() is False
    assert asr_sensevoice._recognizer is None


def test_transcribe_returns_none_without_a_model(temp_model_dir):
    """None, not "": the caller must be able to tell "unavailable" from
    "silence", and only the former should surface as an error."""
    audio = np.zeros(config.SAMPLE_RATE, dtype=np.float32)
    assert asr_sensevoice.transcribe(audio) is None


def test_model_dir_defaults_next_to_the_other_models():
    assert asr_sensevoice.model_dir().name == "sherpa-sense-voice-small"


# --------------------------------------------------------------------------
# Engine selection with the sensevoice backend
# --------------------------------------------------------------------------


@pytest.fixture
def sensevoice_backend(monkeypatch):
    monkeypatch.setattr(config, "ASR_BACKEND", "sensevoice")
    return monkeypatch


def test_live_falls_back_to_zipformer_when_installed(sensevoice_backend, monkeypatch):
    monkeypatch.setattr(asr_sherpa, "is_installed", lambda: True)
    assert stt.live_backend() == "sherpa"


def test_live_falls_back_to_whisper_without_a_streaming_model(sensevoice_backend, monkeypatch):
    monkeypatch.setattr(asr_sherpa, "is_installed", lambda: False)
    assert stt.live_backend() == "whisper"


def test_live_is_always_a_separate_engine(sensevoice_backend, monkeypatch):
    """SenseVoice cannot stream, so whatever draws the caption is another
    engine — and the refine pass is genuinely a second pass."""
    monkeypatch.setattr(asr_sherpa, "is_installed", lambda: False)
    assert stt.live_is_separate() is True
    assert stt.refine_blocked_reason() is None
    assert stt.refine_enabled() is True


def test_status_reports_the_sensevoice_backend(sensevoice_backend, monkeypatch, temp_model_dir):
    monkeypatch.setattr(asr_sherpa, "is_installed", lambda: False)
    status = stt.status()
    assert status["backend"] == "sensevoice"
    assert status["model_key"] == "sensevoice"
    assert status["offline_ready"] is False  # nothing on disk in the temp dir


def test_final_dispatch_routes_to_sensevoice(sensevoice_backend, monkeypatch):
    seen: dict[str, object] = {}

    def fake_transcribe(audio, language=None, hotwords=None):
        seen["language"] = language
        seen["hotwords"] = hotwords
        return "你好，世界。"

    monkeypatch.setattr(asr_sensevoice, "transcribe", fake_transcribe)
    audio = np.zeros(config.SAMPLE_RATE, dtype=np.float32)
    assert stt.transcribe(audio, "zh", hotwords="热词") == "你好，世界。"
    assert seen["hotwords"] == "热词"


def test_final_dispatch_maps_unavailable_to_empty(sensevoice_backend, monkeypatch):
    """``None`` (backend down) becomes "", not a crash: the caller surfaces
    ``model_not_ready`` separately via ``stt.is_ready``."""
    monkeypatch.setattr(asr_sensevoice, "transcribe", lambda *a, **k: None)
    audio = np.zeros(config.SAMPLE_RATE, dtype=np.float32)
    assert stt.transcribe(audio, "zh") == ""
