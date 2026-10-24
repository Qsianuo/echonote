#!/usr/bin/env python
"""Generate VAD fixtures and reference results from the *Python* Segmenter.

The JS port (mobile/src/vad.js) must segment the same audio identically; this
script dumps both the audio and Python's verdicts to fixtures.json so the node
test compares against an identical input, not a re-synthesized lookalike.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.vad import Segmenter  # noqa: E402

SAMPLE_RATE = 16000


def speech(seconds: float, amplitude: float = 0.2, freq: float = 220.0) -> list[float]:
    count = int(SAMPLE_RATE * seconds)
    t = np.arange(count, dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32).tolist()


def silence(seconds: float) -> list[float]:
    return [0.0] * int(SAMPLE_RATE * seconds)


def room_noise(seconds: float) -> list[float]:
    rng = np.random.default_rng(7)
    return rng.normal(0, 0.004, int(SAMPLE_RATE * seconds)).astype(np.float32).tolist()


def feed(segmenter, audio):
    events = []
    pending = np.asarray(audio, dtype=np.float32)
    frame = int(SAMPLE_RATE * 0.03)
    for start in range(0, len(pending) - frame + 1, frame):
        events.extend(segmenter.push(pending[start : start + frame]))
    return events


def slim(events):
    """Only what the JS side must reproduce: boundaries, not audio buffers."""
    return [
        {"start": round(e["start"], 3), "end": round(e["end"], 3)} for e in events
    ]


CASES = [
    # (name, overrides, audio pieces)
    ("single_sentence", {}, [silence(0.5), speech(1.2), silence(1.6)]),
    ("two_sentences", {}, [speech(0.9), silence(1.2), speech(0.7), silence(1.4)]),
    ("short_blip_dropped", {}, [silence(0.5), speech(0.1), silence(1.0)]),
    (
        "quiet_onset_kept_by_preroll",
        {},
        [silence(0.5), speech(0.5, amplitude=0.03), silence(1.2)],
    ),
    (
        "meeting_gate_rejects_room_blip",
        {"gate_ratio": 4.5},
        [room_noise(1.0), speech(0.3, amplitude=0.02), silence(1.5)],
    ),
    (
        "personal_gate_admits_room_blip",
        {"gate_ratio": 3.0},
        [room_noise(1.0), speech(0.3, amplitude=0.02), silence(1.5)],
    ),
]


def main() -> None:
    out = {"sampleRate": SAMPLE_RATE, "cases": []}
    for name, overrides, pieces in CASES:
        audio = [x for piece in pieces for x in piece]
        segmenter = Segmenter(**overrides)
        events = feed(segmenter, audio)
        tail = segmenter.flush()
        if tail is not None:
            events.append(tail)
        out["cases"].append(
            {"name": name, "overrides": overrides, "audio": audio, "events": slim(events)}
        )
        print(f"{name}: {len(events)} event(s)")

    target = Path(__file__).resolve().parent / "fixtures.json"
    target.write_text(json.dumps(out), encoding="utf-8")
    print(f"\nwrote {target} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
