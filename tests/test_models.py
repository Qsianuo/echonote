"""FR-046: offline-first model resolution.

The product claim is "works with the network unplugged". That is only true if
resolution picks a local folder and never falls back to a repo id, so these
tests assert the decision itself, not just the happy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import config, models


def make_model(root: Path, dirname: str, size: int = 2 << 20) -> Path:
    """Create a minimal but *complete* model folder."""
    folder = root / dirname
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text("{}", encoding="utf-8")
    (folder / "model.bin").write_bytes(b"\0" * size)
    return folder


def test_is_complete_requires_weights_and_config(tmp_path: Path):
    folder = make_model(tmp_path, "m")
    assert models.is_complete(folder)
    (folder / "config.json").unlink()
    assert not models.is_complete(folder)


def test_is_complete_rejects_truncated_weights(tmp_path: Path):
    """An interrupted download leaves a short model.bin; it must not count."""
    folder = make_model(tmp_path, "m", size=1024)
    assert not models.is_complete(folder)


def test_is_complete_rejects_missing_dir(tmp_path: Path):
    assert not models.is_complete(tmp_path / "nope")
    assert not models.is_complete(None)


def test_available_is_ordered_by_quality(tmp_path: Path):
    make_model(tmp_path, "faster-whisper-small")
    make_model(tmp_path, "faster-whisper-large-v3-turbo")
    keys = [spec.key for spec in models.available(tmp_path)]
    assert keys == ["large-v3-turbo", "small"]


def test_resolve_picks_the_declared_default_when_installed(tmp_path: Path):
    make_model(tmp_path, "faster-whisper-small")
    make_model(tmp_path, "faster-whisper-medium")
    target = models.resolve(tmp_path)
    assert target.key == "small"
    assert target.source == "local"
    assert target.path == str(tmp_path / "faster-whisper-small")
    assert target.ready_offline is True


def test_the_declared_default_wins_when_installed(tmp_path: Path):
    """The default is a product decision, not something a heuristic overrides."""
    make_model(tmp_path, "faster-whisper-large-v3-turbo")
    make_model(tmp_path, "faster-whisper-small")
    assert models.resolve(tmp_path).key == "small"


def test_best_realtime_model_is_chosen_when_the_default_is_absent(tmp_path: Path):
    """No small on disk: prefer a realtime tier over a quality-only one."""
    make_model(tmp_path, "faster-whisper-medium")
    make_model(tmp_path, "faster-whisper-large-v3")
    assert models.resolve(tmp_path).key == "medium"


def test_a_lone_quality_model_is_still_used(tmp_path: Path):
    """Copying 3 GB in by hand is an explicit choice; honour it."""
    make_model(tmp_path, "faster-whisper-large-v3")
    target = models.resolve(tmp_path)
    assert target.key == "large-v3"
    assert target.ready_offline is True


def test_explicit_key_beats_the_default(tmp_path: Path):
    make_model(tmp_path, "faster-whisper-large-v3-turbo")
    make_model(tmp_path, "faster-whisper-small")
    target = models.resolve(tmp_path, "small")
    assert target.key == "small"
    assert target.path is not None


def test_resolve_falls_back_to_default_when_nothing_is_local(tmp_path: Path):
    target = models.resolve(tmp_path)
    assert target.key == models.DEFAULT_KEY
    assert target.path is None
    assert target.ready_offline is False
    # Without a local path the loader would have to reach the network.
    assert target.loader_value == models.BY_KEY[models.DEFAULT_KEY].repo


def test_resolve_honours_an_explicit_key(tmp_path: Path):
    make_model(tmp_path, "faster-whisper-large-v3-turbo")
    target = models.resolve(tmp_path, "small")
    assert target.key == "small"
    assert target.source == "env-key"
    # small is not on disk, so this must not pretend to be offline ready.
    assert target.path is None


def test_resolve_accepts_a_directory_path(tmp_path: Path):
    folder = make_model(tmp_path, "my-custom-model")
    target = models.resolve(tmp_path, str(folder))
    assert target.source == "env-path"
    assert target.path == str(folder)
    assert target.loader_value == str(folder)


def test_resolve_passes_unknown_names_through(tmp_path: Path):
    """``small.en`` and HF repo ids still work for people who want them."""
    target = models.resolve(tmp_path, "small.en")
    assert target.key == "small.en"
    assert target.path is None
    assert target.loader_value == "small.en"


def test_missing_hint_names_the_exact_folder(tmp_path: Path):
    target = models.resolve(tmp_path)
    hint = models.missing_hint(tmp_path, target)
    expected = tmp_path / models.BY_KEY[models.DEFAULT_KEY].dirname
    assert str(expected) in hint
    assert "fetch_model.py" in hint


def test_offline_env_forbids_hub_access():
    env = models.offline_env()
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    # The mirror cannot proxy Xet, so it must stay off even offline.
    assert env["HF_HUB_DISABLE_XET"] == "1"


def test_offline_mode_is_read_on_every_call(monkeypatch):
    monkeypatch.delenv("ECHONOTE_OFFLINE", raising=False)
    assert config.offline_mode() is False
    monkeypatch.setenv("ECHONOTE_OFFLINE", "1")
    assert config.offline_mode() is True
    monkeypatch.setenv("ECHONOTE_OFFLINE", "off")
    assert config.offline_mode() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_offline_mode_accepts_common_truthy_spellings(monkeypatch, value: str):
    monkeypatch.setenv("ECHONOTE_OFFLINE", value)
    assert config.offline_mode() is True
