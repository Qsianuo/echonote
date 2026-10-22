"""Remote ASR backend: encoding, failure handling, circuit breaker."""

from __future__ import annotations

import numpy as np
import pytest

from app import asr_remote, config


@pytest.fixture
def remote_on(monkeypatch):
    """Configure a remote backend and reset the breaker around each test."""
    monkeypatch.setattr(config, "ASR_REMOTE", True)
    monkeypatch.setattr(
        config, "ASR_REMOTE_URL", "https://example.test/v1/audio/transcriptions"
    )
    monkeypatch.setattr(config, "ASR_REMOTE_KEY", "test-key")
    monkeypatch.setattr(config, "ASR_REMOTE_MODEL", "whisper-large-v3-turbo")
    monkeypatch.setattr(config, "ASR_REMOTE_PROMPT", "")
    monkeypatch.setattr(config, "ASR_REMOTE_FAIL_THRESHOLD", 3)
    monkeypatch.setattr(config, "ASR_REMOTE_COOLDOWN_S", 30.0)
    asr_remote.reset()
    try:
        yield
    finally:
        asr_remote.reset()


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def audio(seconds: float = 1.0, value: float = 0.1) -> np.ndarray:
    return np.full(int(config.SAMPLE_RATE * seconds), value, dtype=np.float32)


# --------------------------------------------------------------- encoding

def test_wav_is_pcm16_mono_at_the_project_rate():
    data = asr_remote.wav_bytes(np.zeros(1600, dtype=np.float32))

    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    assert data[12:16] == b"fmt "
    assert data[36:40] == b"data"
    # Channels at 22, rate at 24, bits per sample at 34, payload size at 40.
    assert int.from_bytes(data[22:24], "little") == 1
    assert int.from_bytes(data[24:28], "little") == config.SAMPLE_RATE
    assert int.from_bytes(data[34:36], "little") == 16
    assert int.from_bytes(data[40:44], "little") == 1600 * 2


def test_wav_header_size_field_matches_the_payload():
    data = asr_remote.wav_bytes(np.zeros(100, dtype=np.float32))
    # RIFF size counts everything after the first 8 bytes.
    assert int.from_bytes(data[4:8], "little") == len(data) - 8


def test_wav_clips_instead_of_wrapping():
    """A sample above 1.0 must saturate, not flip sign.

    Without the clip, 1.5 * 32767 overflows int16 and a loud syllable becomes
    a click on the opposite polarity — audible and unrecognisable.
    """
    data = asr_remote.wav_bytes(np.array([1.5, -1.5], dtype=np.float32))
    samples = np.frombuffer(data[44:], dtype=np.int16)
    assert samples.tolist() == [32767, -32767]


# ----------------------------------------------------------------- calls

def test_disabled_without_a_key(monkeypatch):
    monkeypatch.setattr(config, "ASR_REMOTE", False)
    assert asr_remote.enabled() is False
    assert asr_remote.transcribe(audio()) is None


def test_success_returns_the_text(remote_on, monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        seen["url"] = url
        return FakeResponse({"text": "今天天气不错"})

    monkeypatch.setattr(asr_remote.httpx, "post", fake_post)

    assert asr_remote.transcribe(audio(), "zh") == "今天天气不错"
    assert seen["url"] == config.ASR_REMOTE_URL
    assert seen["data"]["model"] == "whisper-large-v3-turbo"
    assert seen["data"]["language"] == "zh"
    # Greedy decoding: the same buffer must not flip wording between pushes.
    assert seen["data"]["temperature"] == "0"
    assert seen["headers"]["Authorization"] == "Bearer test-key"


def test_empty_prompt_is_not_sent(remote_on, monkeypatch):
    """An empty prompt must be omitted, not sent as "".

    A hosted Whisper continues from a prompt that does not match the audio;
    that is exactly how a session fills up with "转写。".
    """
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse({"text": "你好"})

    monkeypatch.setattr(asr_remote.httpx, "post", fake_post)
    asr_remote.transcribe(audio(), "zh")
    assert "prompt" not in seen["data"]


def test_transport_error_returns_none_not_empty(remote_on, monkeypatch):
    """None means "use the local model"; "" would mean "this audio is silent"."""

    def boom(*args, **kwargs):
        raise asr_remote.httpx.ConnectError("no route")

    monkeypatch.setattr(asr_remote.httpx, "post", boom)
    assert asr_remote.transcribe(audio()) is None


def test_missing_text_field_is_not_a_failure(remote_on, monkeypatch):
    """A 200 with an unexpected body is the provider's answer, not a transport
    error — treat it as silence so the breaker does not trip on a format quirk."""
    monkeypatch.setattr(
        asr_remote.httpx, "post", lambda *a, **k: FakeResponse({"segments": []})
    )
    assert asr_remote.transcribe(audio()) == ""


def test_breaker_opens_after_threshold_and_stops_calling(remote_on, monkeypatch):
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise asr_remote.httpx.ConnectError("down")

    monkeypatch.setattr(asr_remote.httpx, "post", boom)

    for _ in range(config.ASR_REMOTE_FAIL_THRESHOLD):
        assert asr_remote.transcribe(audio()) is None
    assert calls["n"] == config.ASR_REMOTE_FAIL_THRESHOLD

    # Breaker is now open: no further HTTP attempt, immediate None.
    assert asr_remote.transcribe(audio()) is None
    assert calls["n"] == config.ASR_REMOTE_FAIL_THRESHOLD


def test_success_closes_the_breaker(remote_on, monkeypatch):
    def boom(*args, **kwargs):
        raise asr_remote.httpx.ConnectError("down")

    monkeypatch.setattr(asr_remote.httpx, "post", boom)
    asr_remote.transcribe(audio())
    asr_remote.transcribe(audio())
    assert asr_remote.status()["remote_fail_streak"] == 2

    monkeypatch.setattr(
        asr_remote.httpx, "post", lambda *a, **k: FakeResponse({"text": "恢复"})
    )
    assert asr_remote.transcribe(audio()) == "恢复"
    assert asr_remote.status()["remote_fail_streak"] == 0


def test_live_calls_use_the_short_timeout(remote_on, monkeypatch):
    seen = {}

    def fake_post(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse({"text": "hi"})

    monkeypatch.setattr(asr_remote.httpx, "post", fake_post)
    asr_remote.transcribe(audio(), live=True)
    assert seen["timeout"] == config.ASR_REMOTE_LIVE_TIMEOUT_S

    asr_remote.transcribe(audio(), live=False)
    assert seen["timeout"] == config.ASR_REMOTE_TIMEOUT_S


def test_median_latency_is_reported(remote_on, monkeypatch):
    monkeypatch.setattr(
        asr_remote.httpx, "post", lambda *a, **k: FakeResponse({"text": "x"})
    )
    assert asr_remote.median_ms() is None
    asr_remote.transcribe(audio())
    assert asr_remote.median_ms() is not None


# ------------------------------------------------- strict ("remote only") mode

def test_status_says_remote_when_pinned_without_a_key(monkeypatch):
    """The whole point of strict mode: the UI must never claim "本地推理".

    Reporting ``local`` while the user asked for remote is what made the
    missing key invisible.
    """
    from app import stt

    monkeypatch.setattr(config, "ASR_REMOTE", False)
    monkeypatch.setattr(config, "ASR_REMOTE_ONLY", True)
    payload = stt.status()

    assert payload["backend"] == "remote"
    assert payload["model_key"] == "remote"
    assert payload["offline_ready"] is False


def test_status_says_local_when_nothing_is_configured(monkeypatch):
    from app import stt

    monkeypatch.setattr(config, "ASR_REMOTE", False)
    monkeypatch.setattr(config, "ASR_REMOTE_ONLY", False)
    assert stt.status()["backend"] == "local"


def test_strict_mode_never_calls_the_local_model(monkeypatch):
    """Remote failure in strict mode must yield "", not a local decode."""
    from app import stt

    class ExplodingModel:
        def transcribe(self, *args, **kwargs):
            raise AssertionError("local model must not run in remote-only mode")

    monkeypatch.setattr(stt, "_model", ExplodingModel())
    monkeypatch.setattr(stt, "_ready", _AlwaysSet())
    monkeypatch.setattr(config, "ASR_REMOTE", True)
    monkeypatch.setattr(config, "ASR_REMOTE_ONLY", True)
    monkeypatch.setattr(asr_remote, "transcribe", lambda *a, **k: None)

    assert stt.transcribe(audio()) == ""


def test_non_strict_mode_still_falls_back_to_local(monkeypatch):
    """Without ASR_REMOTE_ONLY the fallback stays: it is the documented
    behaviour for anyone who wants best-effort captions."""
    from app import stt

    called = {"n": 0}

    class CountingModel:
        def transcribe(self, *args, **kwargs):
            called["n"] += 1
            raise RuntimeError("stop here — reaching the local path is enough")

    monkeypatch.setattr(stt, "_model", CountingModel())
    monkeypatch.setattr(stt, "_ready", _AlwaysSet())
    monkeypatch.setattr(config, "ASR_REMOTE", True)
    monkeypatch.setattr(config, "ASR_REMOTE_ONLY", False)
    monkeypatch.setattr(asr_remote, "transcribe", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        stt.transcribe(audio())
    assert called["n"] == 1


class _AlwaysSet:
    def is_set(self):
        return True

    def set(self):
        return None

    def clear(self):
        return None
