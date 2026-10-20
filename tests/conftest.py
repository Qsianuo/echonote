"""Shared pytest setup.

The suite must never download a model, bind a port or touch the network, so
model preloading is disabled before ``app`` is imported.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ECHONOTE_SKIP_PRELOAD", "1")
# Point model lookup at a path that does not exist so the suite behaves the
# same on a developer machine (which has real weights) and in CI (which does
# not). Tests that need a model build a fake folder under tmp_path.
os.environ.setdefault("ECHONOTE_MODEL_DIR", str(ROOT / "tests" / "_absent_models"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

SAMPLE_RATE = 16000
FRAME = 480  # 30 ms


def speech(seconds: float, amplitude: float = 0.2, freq: float = 220.0) -> np.ndarray:
    """A loud, periodic signal that the energy VAD will classify as speech."""
    count = int(SAMPLE_RATE * seconds)
    t = np.arange(count, dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def feed(segmenter, audio: np.ndarray) -> list[dict]:
    """Push audio in 30 ms frames, collecting every completed segment."""
    events: list[dict] = []
    for start in range(0, len(audio) - FRAME + 1, FRAME):
        events.extend(segmenter.push(audio[start : start + FRAME]))
    return events


@pytest.fixture()
def sample_wav() -> Path:
    return ROOT / "tests" / "sample.wav"
