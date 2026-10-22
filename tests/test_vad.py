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
    # The first utterance is finalized at 1.92 s (900 ms hangover), so only
    # 2 frames of the gap are in the pre-roll ring when the second utterance
    # triggers at 1.98 s: its start is pulled back from 2.0 s to 1.92 s.
    assert close(events[1]["start"], 1.92)
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
    # Voice is confirmed at 0.48 s, and the full 300 ms pre-roll ring is glued
    # back on, so the reported start is 0.48 - 0.30 = 0.18 s.
    assert close(segmenter.segment_start_s, 0.18)


@pytest.mark.parametrize("seconds", [0.3, 0.5, 0.9])
def test_short_utterances_above_minimum_are_kept(seconds: float):
    segmenter = Segmenter()
    events = feed(segmenter, np.concatenate([speech(seconds), silence(1.0)]))
    assert len(events) == 1
    assert close(events[0]["end"], seconds)


# ---------------------------------------------------------------- preroll
# The VAD only confirms speech after VAD_MIN_SPEECH_MS of voice, so the audio
# right before that confirmation used to be dropped, clipping quiet onsets.
# The fix buffers it and glues it back onto the head of the utterance.


def _run_preroll(preroll_ms: int) -> Segmenter:
    """Same input, one knob: 10 quiet frames then 20 loud ones."""
    segmenter = Segmenter(preroll_ms=preroll_ms)
    quiet = np.zeros(segmenter.frame_size, dtype=np.float32)
    loud = np.full(segmenter.frame_size, 0.5, dtype=np.float32)
    for _ in range(10):
        assert segmenter.push(quiet) == []
    for _ in range(20):
        segmenter.push(loud)
    assert segmenter.in_speech is True
    return segmenter


def test_preroll_keeps_audio_before_speech_was_confirmed():
    """The 300 ms before the trigger must reach the utterance."""
    with_preroll = _run_preroll(300)
    without_preroll = _run_preroll(0)

    assert with_preroll.segment_start_s < without_preroll.segment_start_s
    assert with_preroll.current_audio().size > without_preroll.current_audio().size


def test_preroll_disabled_matches_previous_behaviour():
    """preroll_ms=0 keeps the old start: the first voiced frame, no rollback."""
    segmenter = _run_preroll(0)

    # Frames 0-9 are quiet, so the first voiced frame is index 10.
    assert segmenter.segment_start_frame == 10
    assert segmenter.segment_start_s == pytest.approx(0.30, abs=1e-9)


def test_finalize_reports_finished_at():
    segmenter = Segmenter()
    feed(segmenter, speech(1.0))

    event = segmenter.flush()
    assert isinstance(event, dict)
    assert "finished_at" in event
    assert isinstance(event["finished_at"], float)


# ------------------------------------------------------------------- span
# The invariant the exported SRT leans on: the samples handed back are the
# span [start, end). If the preroll moved the audio but not the timestamp,
# the subtitle would land 300 ms late — and no other assertion catches it.

SAMPLE_RATE = 16000
TAIL_FRAMES = int(0.15 * 1000 / 30)  # the 150 ms of trailing context


def _span_samples(event: dict) -> int:
    return round((event["end"] - event["start"]) * SAMPLE_RATE)


def test_audio_length_matches_reported_span():
    """A flushed segment ends at its last voiced frame: samples == span."""
    for preroll_ms in (300, 0):
        segmenter = Segmenter(preroll_ms=preroll_ms)
        feed(segmenter, np.concatenate([silence(0.5), speech(1.0)]))

        event = segmenter.flush()
        assert event is not None
        assert abs(len(event["audio"]) - _span_samples(event)) <= 1


def test_audio_length_matches_reported_span_across_utterances():
    segmenter = Segmenter(preroll_ms=300)
    for gap_s in (0.5, 0.3):
        feed(segmenter, np.concatenate([silence(gap_s), speech(1.0)]))

        event = segmenter.flush()
        assert event is not None
        assert abs(len(event["audio"]) - _span_samples(event)) <= 1


def test_hangover_final_audio_starts_at_its_reported_start():
    """Hangover finalization ends in silence, so the audio runs past `end`.

    The surplus must be exactly the 150 ms tail — never the preroll on top of
    it. ``keep`` is computed from ``segment_start_frame``, so a start that
    failed to roll back would silently *truncate the head* instead: the two
    preroll settings must therefore differ by exactly the preroll frames.
    """
    lengths = {}
    for preroll_ms in (300, 0):
        segmenter = Segmenter(preroll_ms=preroll_ms)
        events = feed(
            segmenter, np.concatenate([silence(0.5), speech(1.0), silence(1.2)])
        )

        assert len(events) == 1
        surplus = len(events[0]["audio"]) - _span_samples(events[0])
        assert surplus == TAIL_FRAMES * (SAMPLE_RATE * 30 // 1000)
        lengths[preroll_ms] = len(events[0]["audio"])

    assert lengths[300] - lengths[0] == 10 * (SAMPLE_RATE * 30 // 1000)
