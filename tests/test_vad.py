"""RG-01: VAD segmentation and timestamps.

Eight fixed fixtures, tolerant to ±1 frame (30 ms) as required by the
regression spec. Anything stricter would make the suite flaky for reasons
that do not matter to users.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.vad import Segmenter
from conftest import feed, silence, speech

FRAME_S = 0.03
TOLERANCE = FRAME_S + 0.001


def close(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= TOLERANCE


def test_silence_yields_nothing():
    segmenter = Segmenter()
    assert feed(segmenter, silence(3.0)) == []
    assert segmenter.flush() is None


def test_short_blip_is_dropped():
    """200 ms of speech is below the 270 ms minimum (9 frames)."""
    segmenter = Segmenter()
    audio = np.concatenate([speech(0.2), silence(1.0)])
    assert feed(segmenter, audio) == []


def test_single_utterance_bounds():
    segmenter = Segmenter()
    audio = np.concatenate([speech(1.2), silence(1.0)])
    events = feed(segmenter, audio)

    assert len(events) == 1
    assert close(events[0]["start"], 0.0)
    assert close(events[0]["end"], 1.2)
    # Audio keeps 150 ms of trailing context, it must not swallow the whole
    # 1 s of silence.
    duration = len(events[0]["audio"]) / 16000
    assert 1.2 <= duration <= 1.4


def test_two_utterances_keep_the_gap():
    segmenter = Segmenter()
    audio = np.concatenate([speech(1.0), silence(1.0), speech(1.0), silence(1.0)])
    events = feed(segmenter, audio)

    assert len(events) == 2
    assert close(events[0]["start"], 0.0)
    assert close(events[0]["end"], 1.0)
    assert close(events[1]["start"], 2.0)
    assert close(events[1]["end"], 3.0)


def test_forced_split_at_max_segment():
    segmenter = Segmenter(max_segment_s=1.0)
    events = feed(segmenter, speech(3.0))

    assert len(events) == 3
    for index, event in enumerate(events):
        assert close(event["start"], index * 1.0)
        assert event["end"] > event["start"]


def test_low_noise_is_not_speech():
    segmenter = Segmenter()
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(16000 * 3) * 0.0015).astype(np.float32)
    assert feed(segmenter, noise) == []


def test_flush_returns_the_open_segment():
    segmenter = Segmenter()
    audio = speech(1.0)  # no trailing silence on purpose
    assert feed(segmenter, audio) == []

    tail = segmenter.flush()
    assert tail is not None
    assert close(tail["start"], 0.0)
    assert close(tail["end"], 1.0)


def test_tail_audio_is_trimmed():
    """The 150 ms tail must not turn a 1 s utterance into a 2 s buffer."""
    segmenter = Segmenter()
    events = feed(segmenter, np.concatenate([speech(1.0), silence(2.0)]))
    assert len(events) == 1

    duration = len(events[0]["audio"]) / 16000
    assert duration < 1.3


def test_segment_start_property_tracks_current_segment():
    segmenter = Segmenter()
    feed(segmenter, np.concatenate([silence(0.5), speech(0.5)]))

    assert segmenter.in_speech is True
    assert close(segmenter.segment_start_s, 0.5)


@pytest.mark.parametrize("seconds", [0.3, 0.5, 0.9])
def test_short_utterances_above_minimum_are_kept(seconds: float):
    segmenter = Segmenter()
    events = feed(segmenter, np.concatenate([speech(seconds), silence(1.0)]))
    assert len(events) == 1
    assert close(events[0]["end"], seconds)
