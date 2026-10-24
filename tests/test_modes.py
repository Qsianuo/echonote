"""Recording-mode presets (personal / meeting): presets, sticky file, API.

The mode is a named set of Segmenter overrides, so the tests cover three
things: the preset shapes stay within the parameters Segmenter accepts, the
sticky file and env var resolve the way the backend/model sticky choices do,
and the HTTP switch validates before mutating anything.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture()
def client():
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def mode_env(monkeypatch, tmp_path):
    """A throwaway sticky-file home and a known starting mode."""
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ACTIVE_MODE", "personal")
    return tmp_path


# --------------------------------------------------------------------------
# Preset shape
# --------------------------------------------------------------------------

_ALLOWED_KEYS = frozenset(
    {
        "sample_rate",
        "threshold",
        "hangover_ms",
        "min_speech_ms",
        "max_segment_s",
        "preroll_ms",
        "gate_ratio",
    }
)


def test_every_preset_only_names_segmenter_parameters():
    """An unknown key would explode ``Segmenter(**mode_overrides())`` at the
    start of the next recording — catch it at test time instead."""
    for name, overrides in config.MODES.items():
        assert set(overrides) <= _ALLOWED_KEYS, name


def test_personal_mode_is_the_untouched_default():
    """``personal`` exists as a name, not as a behaviour change: its override
    set is empty, so a session on it runs exactly the pre-mode defaults."""
    assert config.MODES["personal"] == {}


def test_meeting_mode_tightens_noise_robustness():
    overrides = config.MODES["meeting"]
    assert overrides["gate_ratio"] > 3.0
    assert overrides["min_speech_ms"] >= config.VAD_MIN_SPEECH_MS


def test_mode_overrides_follows_the_active_mode(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_MODE", "meeting")
    assert config.mode_overrides() == config.MODES["meeting"]
    monkeypatch.setattr(config, "ACTIVE_MODE", "nonexistent")
    assert config.mode_overrides() == {}


# --------------------------------------------------------------------------
# Resolution: env > sticky file > default
# --------------------------------------------------------------------------


def test_env_mode_wins(monkeypatch):
    monkeypatch.setenv("ECHONOTE_MODE", "meeting")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.ACTIVE_MODE == "meeting"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_sticky_mode_file_is_honoured(monkeypatch, tmp_path):
    (tmp_path / "mode.txt").write_text("meeting\n", encoding="utf-8")
    monkeypatch.setenv("ECHONOTE_MODEL_DIR", str(tmp_path))
    reloaded = importlib.reload(config)
    try:
        assert reloaded.ACTIVE_MODE == "meeting"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_an_unknown_sticky_mode_falls_back_to_personal(monkeypatch, tmp_path):
    (tmp_path / "mode.txt").write_text("party\n", encoding="utf-8")
    monkeypatch.setenv("ECHONOTE_MODEL_DIR", str(tmp_path))
    reloaded = importlib.reload(config)
    try:
        assert reloaded.ACTIVE_MODE == "personal"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_config_carries_the_active_mode(client):
    body = client.get("/api/config").json()
    assert body["mode"] == config.ACTIVE_MODE


def test_mode_switch_round_trips_and_persists(client, mode_env):
    response = client.post("/api/mode", json={"mode": "meeting"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "mode": "meeting"}
    assert config.ACTIVE_MODE == "meeting"
    assert (mode_env / "mode.txt").read_text(encoding="utf-8").strip() == "meeting"
    assert client.get("/api/config").json()["mode"] == "meeting"


def test_mode_switch_rejects_unknown_names(client, mode_env):
    response = client.post("/api/mode", json={"mode": "party"})
    assert response.status_code == 400
    # A rejected switch must not leave a half-applied mode behind.
    assert config.ACTIVE_MODE == "personal"
    assert not (mode_env / "mode.txt").exists()
