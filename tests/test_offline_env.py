"""The offline flag must not outlive the decision that justified it.

A fresh install has no weights on disk, so the loader deliberately allows one
download. Setting ``HF_HUB_OFFLINE`` at the same time made that download
impossible, and the user saw a network error on a machine that was online —
the packaged build shipped with that contradiction.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import models  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ.pop(key, None)


def test_offline_env_sets_the_flags_that_block_the_hub(clean_env):
    models.apply_offline_env()
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_apply_online_env_clears_them_again(clean_env):
    models.apply_offline_env()
    models.apply_online_env()
    assert "HF_HUB_OFFLINE" not in os.environ
    assert "TRANSFORMERS_OFFLINE" not in os.environ


def test_apply_online_env_is_safe_when_nothing_was_set(clean_env):
    models.apply_online_env()
    assert "HF_HUB_OFFLINE" not in os.environ


def test_an_operator_opt_in_offline_run_is_not_overridden_by_the_loader(clean_env):
    # ECHONOTE_OFFLINE is the user's explicit "--offline"; the loader honours
    # it by keeping local_files_only, so the env flags staying set is correct.
    clean_env.setenv("ECHONOTE_OFFLINE", "1")
    models.apply_offline_env()
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_offline_env_also_disables_telemetry_and_xet(clean_env):
    # Xet is the protocol hf-mirror does not implement; leaving it on is how a
    # mirrored download stalls at zero bytes.
    env = models.offline_env()
    assert env["HF_HUB_DISABLE_XET"] == "1"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
