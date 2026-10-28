"""Shared pytest setup.

The suite must never download a model, bind a port or touch the network, so
model preloading is disabled before ``app`` is imported.
"""

from __future__ import annotations

import os
import sys
import threading
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


@pytest.fixture(autouse=True)
def _isolate_stt_engine():
    """每条用例前后都把 stt 的进程级引擎状态复位。

    这些全局变量在真机上本就该活到进程结束，但测试之间会互相污染：
    ``preload()`` 起的 ``whisper-retry-loop`` 是后台线程，若只把 ``_shutdown``
    换成新 Event 而不 join，老线程会一直跑下去，在后续用例里调 ``load()``，
    把上一条用例装的假模型写回 ``_model`` —— 表现为一条毫不相干的用例随机
    报 ``'WhisperModel' object has no attribute 'transcribe'``（GitHub Actions
    run 40648497208，py3.11 独有）。
    """
    from app import stt

    def reset() -> None:
        stt.shutdown()  # 有 join，最多等 2 s；旧线程必须先死，才能换 Event
        stt._model = None
        stt._live_model = None
        stt._error = None
        stt._loading = False
        stt._pending_loads = 0
        stt._attempts = 0
        stt._auto_retries = 0
        stt._last_failure_at = 0.0
        stt._scheduler_started = False
        stt._scheduler_thread = None
        stt._scheduler_stop = None
        stt._shutdown = threading.Event()
        stt._ready.clear()
        stt._live_ready.clear()

    reset()
    yield
    reset()
