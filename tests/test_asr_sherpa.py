"""sherpa-onnx backend: file checks and failure handling.

The model itself is 162 MB, so these tests never build it — they cover the
decision logic around it, which is what decides whether the app starts with a
working decoder or a clear error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import asr_sherpa, config


@pytest.fixture
def temp_model_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SHERPA_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(asr_sherpa, "_recognizer", None)
    return tmp_path


def test_missing_files_are_reported_as_not_installed(temp_model_dir):
    assert asr_sherpa.is_installed() is False


def test_a_partial_download_is_not_installed(temp_model_dir):
    """Three of four files is the shape of an interrupted download.

    Treating it as installed would fail at model-build time with an opaque
    ONNX error instead of a message naming the missing file.
    """
    for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx"):
        (temp_model_dir / name).write_bytes(b"stub")
    assert asr_sherpa.is_installed() is False

    (temp_model_dir / "tokens.txt").write_text("a 1\n", encoding="utf-8")
    assert asr_sherpa.is_installed() is True


def test_load_returns_false_when_files_are_missing(temp_model_dir):
    assert asr_sherpa.load() is False
    assert asr_sherpa._recognizer is None


def test_transcribe_returns_none_without_a_model(temp_model_dir):
    """None, not "": the caller must be able to tell "unavailable" from
    "silence", and only the former should surface as an error."""
    import numpy as np

    audio = np.zeros(config.SAMPLE_RATE, dtype=np.float32)
    assert asr_sherpa.transcribe(audio) is None


def test_model_dir_defaults_next_to_the_other_models():
    assert Path(config.SHERPA_MODEL_DIR).name == "sherpa-zipformer-zh"


def test_backend_choice_prefers_an_explicit_setting(monkeypatch):
    """ECHONOTE_ASR_BACKEND wins over the key-based default."""
    import importlib

    monkeypatch.setenv("ECHONOTE_ASR_BACKEND", "sherpa")
    monkeypatch.setenv("ECHONOTE_ASR_REMOTE_KEY", "some-key")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.ASR_BACKEND == "sherpa"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
