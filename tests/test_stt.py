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

from pathlib import Path

from app import config, models, stt


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
    assert fake.calls[0]["initial_prompt"] == stt.config.ASR_PROMPT_ZH
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
        assert call["without_timestamps"] is True

    # Partials are thrown away once the final lands, so they stay greedy and
    # do not carry context; the final pass pays for both.
    assert fake.calls[0]["temperature"] == 0.0
    assert fake.calls[0]["condition_on_previous_text"] is False
    # A single decoding pass by default: the temperature ladder truncated long
    # audio on the project's own sample (see config.ASR_TEMPERATURES).
    assert fake.calls[1]["temperature"] == stt.config.ASR_TEMPERATURES
    assert fake.calls[1]["condition_on_previous_text"] is True


def test_final_pass_sets_quality_thresholds(fake):
    """These three thresholds are what stop silence from becoming text."""
    stt.transcribe(audio(), "zh")
    call = fake.calls[0]
    assert call["no_speech_threshold"] == stt.config.ASR_NO_SPEECH_THRESHOLD
    assert call["log_prob_threshold"] == stt.config.ASR_LOG_PROB_THRESHOLD
    assert call["compression_ratio_threshold"] == stt.config.ASR_COMPRESSION_RATIO_THRESHOLD


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
    for key in (
        "ready",
        "model",
        "error",
        "retrying",
        "attempts",
        "model_label",
        "model_key",
        "offline_ready",
    ):
        assert key in data


# --------------------------------------------------------------------------
# FR-046: offline behaviour
# --------------------------------------------------------------------------


def _fake_model_dir(root: Path) -> Path:
    folder = root / "faster-whisper-small"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text("{}", encoding="utf-8")
    (folder / "model.bin").write_bytes(b"\0" * (2 << 20))
    return folder


def test_offline_without_a_model_never_starts_a_download(
    monkeypatch, tmp_path, whisper_module
):
    """Air-gapped + no weights must fail immediately, not block for minutes."""
    monkeypatch.setenv("ECHONOTE_OFFLINE", "1")
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODEL_CHOICE", None)

    assert stt.load() is False
    assert whisper_module["calls"] == 0

    reported = stt.status()
    assert reported["ready"] is False
    assert reported["offline_ready"] is False
    assert "未找到模型" in reported["error"]
    assert str(tmp_path) in reported["error"]


def test_local_model_is_loaded_from_disk_without_the_hub(
    monkeypatch, tmp_path, whisper_module
):
    folder = _fake_model_dir(tmp_path)
    monkeypatch.setenv("ECHONOTE_OFFLINE", "1")
    monkeypatch.setattr(config, "MODEL_CHOICE", str(folder))

    assert stt.load() is True
    assert whisper_module["name"] == str(folder)
    assert whisper_module["kwargs"]["local_files_only"] is True
    assert stt.status()["offline_ready"] is True


def test_missing_model_without_offline_still_attempts_a_download(
    monkeypatch, tmp_path, whisper_module
):
    """A brand-new install has to be able to fetch its model once."""
    monkeypatch.delenv("ECHONOTE_OFFLINE", raising=False)
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODEL_CHOICE", None)

    assert stt.load() is True
    assert whisper_module["calls"] == 1
    # No local folder -> loader gets a repo id and is allowed to reach out.
    assert whisper_module["name"] == models.BY_KEY[models.DEFAULT_KEY].repo
    assert whisper_module["kwargs"]["local_files_only"] is False
    assert stt.status()["offline_ready"] is False


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
            state["name"] = name
            state["kwargs"] = kwargs
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


# --------------------------------------------------------------------------
# FR-047: runtime model switch
# --------------------------------------------------------------------------


def test_switch_refuses_a_model_that_is_not_on_disk(monkeypatch, tmp_path, whisper_module):
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODEL_CHOICE", None)

    result = stt.switch("large-v3-turbo")
    assert result["ok"] is False
    assert "faster-whisper-large-v3-turbo" in result["error"]
    # A switch must never become an accidental download.
    assert whisper_module["calls"] == 0


def test_switch_loads_an_installed_model(monkeypatch, tmp_path, whisper_module):
    folder = _fake_model_dir(tmp_path)
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODEL_CHOICE", None)

    result = stt.switch("small")
    assert result == {"ok": True, "switching": True, "key": "small"}
    assert wait_ready()
    assert whisper_module["name"] == str(folder)
    assert stt.status()["model_key"] == "small"


def test_offline_missing_model_does_not_auto_retry(monkeypatch, tmp_path, whisper_module):
    """A missing model is a permanent condition: retrying it forever is noise."""
    monkeypatch.setenv("ECHONOTE_OFFLINE", "1")
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODEL_CHOICE", None)
    monkeypatch.setattr(stt, "_retry_blocked", False, raising=False)
    monkeypatch.setattr(stt, "_last_failure_at", 0.0, raising=False)

    assert stt.load() is False
    assert stt.maybe_auto_retry() is False
    # A human can still force an attempt, even though auto-retry is blocked.
    attempts_before = stt._attempts
    stt.retry()
    deadline = time.monotonic() + 3
    while stt._attempts == attempts_before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stt._attempts > attempts_before


# --------------------------------------------------------------------------
# v3: prompt echo and the live decode path
# --------------------------------------------------------------------------


def test_prompt_echo_is_stripped_from_a_short_hypothesis():
    prompt = "以下是普通话录音，请使用简体中文和标点符号转写。"
    assert stt._clean(prompt, prompt) == ""
    assert stt._clean("以下是普通话录音，请使用简体中文和标点符号转写。你好", prompt) == "你好"
    # A genuine opening that merely shares the first characters is untouched.
    assert stt._clean("今天的会议开始", prompt) == "今天的会议开始"


def test_strip_echo_ignores_short_overlaps():
    # "以下" alone is too short to be an echo; dropping it would eat speech.
    assert stt._strip_echo("以下三点", "以下是普通话录音") == "以下三点"


def test_live_transcribe_returns_empty_without_a_model(monkeypatch):
    monkeypatch.setattr(stt, "_live_model", None)
    monkeypatch.setattr(stt, "_model", None)
    assert stt.transcribe_live(np.zeros(16000, dtype=np.float32), "zh") == ""


def test_live_target_prefers_the_fastest_readable_model(tmp_path, monkeypatch):
    for name in ("faster-whisper-base", "faster-whisper-small"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "model.bin").write_bytes(b"0" * (2 << 20))
        (folder / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODEL_CHOICE", None)
    monkeypatch.setattr(config, "LIVE_MODEL", None)
    assert stt.live_target().key == "base"


def test_live_target_falls_back_to_the_main_model(tmp_path, monkeypatch):
    folder = tmp_path / "faster-whisper-medium"
    folder.mkdir()
    (folder / "model.bin").write_bytes(b"0" * (2 << 20))
    (folder / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MODEL_CHOICE", None)
    monkeypatch.setattr(config, "LIVE_MODEL", None)
    assert stt.live_target().key == "medium"
    assert stt.live_is_separate() is False


# --------------------------------------------------------------------------
# v0.11.1: the 75 MB tier must never make a caption worse
# --------------------------------------------------------------------------


def test_prompt_echo_is_stripped_even_when_it_is_the_tail_of_the_instruction():
    """`tiny` answers the instruction instead of transcribing the audio.

    Reported from the UI: a caption line read "请使用简体中文和标点符号转写。"
    — the tail of ``ASR_PROMPT_ZH``, not anything the user said.
    """
    prompt = config.ASR_PROMPT_ZH
    assert stt._clean("请使用简体中文和标点符号转写。", prompt) == ""
    # A genuine sentence that merely starts with the same two characters stays.
    assert stt._clean("以下是今天的会议记录。", prompt) == "以下是今天的会议记录。"


def test_prompt_echo_is_stripped_from_the_middle_of_a_hypothesis():
    prompt = config.ASR_PROMPT_ZH
    text = "今天天气特别差。请使用简体中文和标点符号转写。"
    assert stt._clean(text, prompt) == "今天天气特别差。"


def test_tiny_gets_the_short_prompt_not_the_instruction():
    """`tiny` answers the instruction; with no prompt it writes Traditional.

    Measured on the sample clip:
      no prompt        "你好,這是回生筆記的實實與因時別測試"   (Traditional)
      short prompt     "你好,这是回生笔记的实实与音实别特势。" (Simplified)
      full prompt      the instruction itself as a caption
    """
    assert stt._prompt_for("zh", "tiny") == config.ASR_PROMPT_ZH_SHORT
    assert stt._prompt_for("zh", "base") == config.ASR_PROMPT_ZH
    assert len(config.ASR_PROMPT_ZH_SHORT) < len(config.ASR_PROMPT_ZH)
    # Other languages never get the Chinese instruction.
    assert stt._prompt_for("en", "base") is None


def test_refinement_is_skipped_when_the_final_model_is_smaller(tmp_path, monkeypatch):
    """Picking the 75 MB tier as the final model used to rewrite a better caption."""
    for name in ("faster-whisper-base", "faster-whisper-tiny", "faster-whisper-small"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "model.bin").write_bytes(b"0" * (2 << 20))
        (folder / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "LIVE_MODEL", None)

    # tiny as the final model: the caption is capped at the chosen size, so
    # tiny runs both passes and there is no second model to refine with.
    monkeypatch.setattr(config, "MODEL_CHOICE", "tiny")
    assert stt.live_target().key == "tiny"
    assert stt.live_is_separate() is False
    assert stt.refine_enabled() is False
    assert stt.refine_blocked_reason() == "single_model"

    # small as the final model: the second pass is a real improvement.
    monkeypatch.setattr(config, "MODEL_CHOICE", "small")
    assert stt.refine_enabled() is True
    assert stt.refine_blocked_reason() is None


def test_the_model_choice_survives_a_restart(tmp_path, monkeypatch):
    """The dropdown used to forget the choice, so picking 75 MB looked broken."""
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    assert config._sticky_choice() is None
    config.remember_model("tiny")
    assert config._sticky_choice() == "tiny"
    config.remember_model(None)
    assert config._sticky_choice() is None


def test_the_model_choice_survives_a_restart(tmp_path, monkeypatch):
    """The dropdown used to forget the choice, so picking 75 MB looked broken."""
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    assert config._sticky_choice() is None
    config.remember_model("tiny")
    assert config._sticky_choice() == "tiny"
    config.remember_model(None)
    assert config._sticky_choice() is None
