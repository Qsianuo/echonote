"""RG-02: transcript cleaning and transcribe parameters.

A fake model stands in for faster-whisper so the suite stays offline and
fast, while still asserting the exact kwargs the product depends on
(beam size, language, prompt).
"""

from __future__ import annotations

import sys
import threading
import time
import types
from types import SimpleNamespace

import numpy as np
import pytest

from app import stt


class FakeModel:
    def __init__(self, text: str = " 你好 ") -> None:
        self.text = text
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return iter([SimpleNamespace(text=self.text)]), SimpleNamespace(language="zh")


@pytest.fixture()
def fake(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(stt, "_model", model, raising=False)
    monkeypatch.setattr(stt, "_error", None, raising=False)
    stt._ready.set()
    try:
        yield model
    finally:
        stt._ready.clear()
        monkeypatch.setattr(stt, "_model", None, raising=False)


def audio(seconds: float = 1.0) -> np.ndarray:
    return np.zeros(int(16000 * seconds), dtype=np.float32)


@pytest.mark.parametrize(
    "text",
    ["[BLANK_AUDIO]", "(空白音频)", "[Silence]", "谢谢观看", "谢谢大家观看"],
)
def test_clean_drops_known_annotations(text: str):
    assert stt._clean(text) == ""


def test_clean_keeps_real_text():
    assert stt._clean("这是一段真实的话。") == "这是一段真实的话。"


def test_zh_gets_the_simplified_chinese_prompt(fake):
    assert stt.transcribe(audio(), "zh") == "你好"
    assert fake.calls[0]["initial_prompt"] == "这是一段普通话的录音。"
    assert fake.calls[0]["language"] == "zh"


@pytest.mark.parametrize("language", ["auto", None, ""])
def test_auto_language_passes_none(fake, language):
    stt.transcribe(audio(), language)
    assert fake.calls[0]["language"] is None
    assert fake.calls[0]["initial_prompt"] is None


def test_fast_uses_beam_one_and_final_uses_beam_five(fake):
    stt.transcribe(audio(), "zh", fast=True)
    stt.transcribe(audio(), "zh", fast=False)

    assert fake.calls[0]["beam_size"] == 1
    assert fake.calls[1]["beam_size"] == 5
    for call in fake.calls:
        assert call["vad_filter"] is False
        assert call["condition_on_previous_text"] is False
        assert call["without_timestamps"] is True
        assert call["temperature"] == 0.0


def test_short_audio_is_ignored_without_calling_the_model(fake):
    assert stt.transcribe(audio(0.1), "zh") == ""
    assert fake.calls == []


def test_missing_model_returns_empty(monkeypatch):
    monkeypatch.setattr(stt, "_model", None, raising=False)
    stt._ready.clear()
    assert stt.transcribe(audio(1.0), "zh") == ""


def test_sanitize_error_hides_windows_path():
    cleaned = stt.sanitize_error(r"Failed to open C:\Users\alice\secret\model.bin")
    assert "alice" not in cleaned
    assert "secret" not in cleaned
    assert "model.bin" in cleaned


def test_sanitize_error_hides_posix_path():
    cleaned = stt.sanitize_error("no such file /home/alice/private/models/blob")
    assert "alice" not in cleaned
    assert "blob" in cleaned


def test_sanitize_error_strips_query_string():
    cleaned = stt.sanitize_error("GET https://huggingface.co/x?token=abc failed")
    assert "token=abc" not in cleaned


def test_sanitize_error_is_short():
    assert len(stt.sanitize_error("x" * 5000)) <= 300


def test_status_shape(monkeypatch):
    monkeypatch.setattr(stt, "_attempts", 2, raising=False)
    data = stt.status()
    for key in ("ready", "model", "error", "retrying", "attempts"):
        assert key in data


# --------------------------------------------------------------------------
# FR-036: a failed load must be recoverable without restarting the process
# --------------------------------------------------------------------------


@pytest.fixture()
def whisper_module(monkeypatch):
    """Install a fake faster_whisper module and reset the engine state."""
    state = {"fail": False, "calls": 0}

    module = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, name, **kwargs):
            state["calls"] += 1
            if state["fail"]:
                raise RuntimeError(
                    r"Unable to download C:\Users\bob\.cache\model.bin?token=zzz"
                )
            self.name = name
            self.kwargs = kwargs

    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    monkeypatch.setattr(stt, "_model", None, raising=False)
    monkeypatch.setattr(stt, "_error", None, raising=False)
    monkeypatch.setattr(stt, "_attempts", 0, raising=False)
    monkeypatch.setattr(stt, "_auto_retries", 0, raising=False)
    monkeypatch.setattr(stt, "_pending_loads", 0, raising=False)
    monkeypatch.setattr(stt, "_loading", False, raising=False)
    stt._ready.clear()
    yield state
    stt._ready.clear()


def wait_ready(timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stt.is_ready():
            return True
        time.sleep(0.01)
    return False


def test_load_success_sets_ready(whisper_module):
    assert stt.load() is True
    assert stt.is_ready() is True
    assert stt.status()["error"] is None
    assert whisper_module["calls"] == 1


def test_failed_load_is_recoverable_without_restart(whisper_module):
    whisper_module["fail"] = True
    assert stt.load() is False

    failed = stt.status()
    assert failed["ready"] is False
    assert failed["error"]
    # Sanitised: no home directory, no query token (FR-020 AC2).
    assert "bob" not in failed["error"]
    assert "token=zzz" not in failed["error"]

    # Fix the environment, press retry: no process restart involved.
    whisper_module["fail"] = False
    stt.retry()
    assert wait_ready()

    recovered = stt.status()
    assert recovered["ready"] is True
    assert recovered["error"] is None


def test_retry_is_a_noop_when_already_ready(whisper_module):
    stt.load()
    calls = whisper_module["calls"]
    result = stt.retry()
    assert result["ready"] is True
    assert whisper_module["calls"] == calls


def test_auto_retry_waits_for_the_delay(whisper_module, monkeypatch):
    whisper_module["fail"] = True
    stt.load()

    assert stt.maybe_auto_retry() is False  # too soon

    whisper_module["fail"] = False
    monkeypatch.setattr(stt, "_last_failure_at", time.monotonic() - 61, raising=False)
    assert stt.maybe_auto_retry() is True
    assert stt.is_ready() is True


def test_auto_retry_gives_up_after_three_attempts(whisper_module, monkeypatch):
    whisper_module["fail"] = True
    stt.load()

    monkeypatch.setattr(stt, "_auto_retries", stt.MAX_AUTO_RETRIES, raising=False)
    monkeypatch.setattr(stt, "_last_failure_at", time.monotonic() - 999, raising=False)
    assert stt.maybe_auto_retry() is False
    assert stt.status()["auto_retry_exhausted"] is True
    # The manual entry point still works.
    assert "ready" in stt.retry()


def test_preload_loads_in_the_background(whisper_module, monkeypatch):
    monkeypatch.setattr(stt, "_shutdown", threading.Event(), raising=False)
    monkeypatch.setattr(stt, "_scheduler_started", False, raising=False)
    stt.preload()
    assert wait_ready()
