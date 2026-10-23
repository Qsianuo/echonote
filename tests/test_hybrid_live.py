"""Hybrid mode: remote finals, local caption.

The caption is the one place a network hop is structurally wrong — the buffer
is re-uploaded every LIVE_STEP_S as it grows, so one sentence costs dozens of
requests. These tests pin the split so a later refactor cannot quietly send the
caption back over the wire.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import asr_remote, asr_sherpa, config, stt  # noqa: E402


@pytest.fixture
def hybrid(monkeypatch):
    """Final path remote, caption local — the configuration under test."""
    monkeypatch.setattr(config, "ASR_BACKEND", "remote", raising=False)
    monkeypatch.setattr(config, "ASR_REMOTE", True, raising=False)
    monkeypatch.setattr(config, "ASR_REMOTE_ONLY", False, raising=False)
    monkeypatch.setattr(config, "ASR_LIVE_REMOTE", False, raising=False)
    return monkeypatch


def test_sherpa_backend_serves_both_roles(monkeypatch):
    monkeypatch.setattr(config, "ASR_BACKEND", "sherpa", raising=False)
    monkeypatch.setattr(config, "ASR_LIVE_REMOTE", False, raising=False)
    assert stt.live_backend() == "sherpa"


def test_remote_finals_keep_the_caption_on_the_streaming_model(hybrid):
    hybrid.setattr(asr_sherpa, "is_installed", lambda: True)
    assert stt.live_backend() == "sherpa"


def test_falls_back_to_whisper_when_the_streaming_model_is_absent(hybrid):
    # No zipformer on disk: a local whisper caption beats no caption at all.
    hybrid.setattr(asr_sherpa, "is_installed", lambda: False)
    assert stt.live_backend() == "whisper"


def test_explicit_live_remote_still_wins(hybrid):
    hybrid.setattr(config, "ASR_LIVE_REMOTE", True, raising=False)
    hybrid.setattr(asr_sherpa, "is_installed", lambda: True)
    assert stt.live_backend() == "remote"


def test_plain_whisper_backend_is_unaffected(monkeypatch):
    monkeypatch.setattr(config, "ASR_BACKEND", "whisper", raising=False)
    monkeypatch.setattr(config, "ASR_LIVE_REMOTE", False, raising=False)
    assert stt.live_backend() == "whisper"


def test_hybrid_reports_two_engines(hybrid):
    hybrid.setattr(asr_sherpa, "is_installed", lambda: True)
    assert stt.live_is_separate() is True


def test_caption_never_calls_the_network_in_hybrid(hybrid):
    """The whole point: a remote final must not make the caption remote."""
    hybrid.setattr(asr_sherpa, "is_installed", lambda: True)
    hybrid.setattr(asr_sherpa, "transcribe", lambda *a, **k: "本地字幕")

    def explode(*_args, **_kwargs):
        raise AssertionError("live caption went over the network")

    hybrid.setattr(asr_remote, "transcribe", explode)

    audio = np.zeros(16000, dtype=np.float32)
    assert stt.transcribe_live(audio, "zh") == "本地字幕"


def test_strict_mode_forces_the_caption_remote_too():
    """--offline-style strict mode promises "never run here", caption included."""
    env = dict(os.environ)
    env["ECHONOTE_ASR_REMOTE_ONLY"] = "1"
    env.pop("ECHONOTE_ASR_LIVE_REMOTE", None)
    result = subprocess.run(
        [sys.executable, "-c", "from app import config; print(config.ASR_LIVE_REMOTE)"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def _read_flag(extra: dict[str, str], name: str) -> str:
    env = dict(os.environ)
    for key in ("ECHONOTE_ASR_REMOTE_ONLY", "ECHONOTE_ASR_LIVE_REMOTE", "ECHONOTE_ASR_BACKEND"):
        env.pop(key, None)
    env.update(extra)
    result = subprocess.run(
        [sys.executable, "-c", f"from app import config; print(config.{name})"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_live_remote_is_off_by_default():
    assert _read_flag({}, "ASR_LIVE_REMOTE") == "False"


def test_pinning_remote_in_the_file_leaves_the_caption_local():
    """The sticky file picks where the transcript comes from, nothing more."""
    flags = {"ECHONOTE_ASR_BACKEND": "remote"}
    assert _read_flag(flags, "ASR_LIVE_REMOTE") == "False"
    # ...while the final path still refuses to degrade silently.
    assert _read_flag(flags, "ASR_REMOTE_ONLY") == "True"


def test_the_operator_env_still_moves_the_caption_off_machine():
    assert _read_flag({"ECHONOTE_ASR_REMOTE_ONLY": "1"}, "ASR_LIVE_REMOTE") == "True"

