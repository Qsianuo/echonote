"""The live caption pipeline: growth policy, shrink guard, thread handoff.

These tests use a fake transcriber, so they run in milliseconds and assert
the *scheduling* behaviour that the latency promise rests on — not the
quality of any model.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from app import config
from app import stream as stream_module
from app.live import LiveCaptioner
from app.stream import UtteranceStream
from conftest import SAMPLE_RATE, silence, speech


# --------------------------------------------------------------------------
# UtteranceStream
# --------------------------------------------------------------------------


def test_due_only_after_enough_new_audio():
    stream = UtteranceStream(step_s=0.6)
    stream.feed(speech(0.3))
    assert stream.due() is False  # below the first-decode minimum
    stream.feed(speech(stream_module.MIN_FIRST_DECODE_S))
    assert stream.due() is True

    snapshot = stream.snapshot()
    stream.apply("你好", snapshot.covered_s)
    # Same audio again: nothing new to decode.
    assert stream.due() is False
    stream.feed(speech(0.7))
    assert stream.due() is True


def test_first_decode_waits_for_enough_audio_to_be_meaningful():
    """A sub-second buffer is where Whisper hallucinates; do not ask early."""
    stream = UtteranceStream(step_s=0.2)
    stream.feed(speech(0.5))
    assert stream.due() is False
    stream.feed(speech(stream_module.MIN_FIRST_DECODE_S))
    assert stream.due() is True
    # The floor is a product decision backed by measurement, not a magic 0.35.
    assert stream_module.MIN_FIRST_DECODE_S >= 1.5


def test_apply_rejects_a_hypothesis_that_loses_half_the_sentence():
    stream = UtteranceStream()
    stream.feed(speech(3.0))
    assert stream.apply("这是回声笔记的实时语音识别测试", 3.0) is True
    # More audio, but the model came back with a fragment: keep the good one.
    stream.feed(speech(1.0))
    assert stream.apply("这是", 4.0) is False
    assert stream.text == "这是回声笔记的实时语音识别测试"


def test_apply_ignores_out_of_order_results():
    stream = UtteranceStream()
    stream.feed(speech(3.0))
    stream.apply("第一版", 2.0)
    assert stream.apply("更旧的一版", 1.5) is False
    assert stream.text == "第一版"


def test_covers_end_uses_a_small_float_tolerance():
    """The tolerance is a float-equality buffer, not a policy.

    The old 0.25 s window let ``finish`` treat a decode that stopped *before*
    the final syllable as "already covered", so the tail of the sentence was
    never re-decoded and went missing from the transcript.
    """
    stream = UtteranceStream()
    stream.feed(speech(2.0))
    stream.apply("你好", 2.0)
    # 2.0 vs 2.05: covered (float tolerance). 2.0 vs 2.1: not covered — the
    # decode must be run again so the tail syllable is not lost.
    assert stream.covers_end(2.03) is True
    assert stream.covers_end(2.1) is False
    assert stream.covers_end(2.6) is False


def test_buffer_is_capped_at_the_whisper_window():
    stream = UtteranceStream(max_s=2.0)
    stream.feed(speech(5.0))
    assert stream.duration_s == pytest.approx(2.0, abs=0.01)


# --------------------------------------------------------------------------
# LiveCaptioner
# --------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.partials: list[str] = []
        self.lock = threading.Lock()

    def emit(self, text: str, start_s: float) -> None:
        with self.lock:
            self.partials.append(text)

    def wait_for(self, count: int, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.partials) >= count:
                    return True
            time.sleep(0.01)
        return False


def make_captioner(text_for=lambda audio: "实时字幕", recorder=None, step_s=0.2):
    recorder = recorder or Recorder()
    captioner = LiveCaptioner(
        transcribe=text_for,
        emit=recorder.emit,
        epoch=lambda: 1,
        language=lambda: "zh",
        step_s=step_s,
    )
    return captioner, recorder


def test_captioner_emits_a_partial_while_the_utterance_grows():
    captioner, recorder = make_captioner()
    captioner.start()
    try:
        captioner.begin(speech(0.3), 0.0)
        captioner.feed(speech(stream_module.MIN_FIRST_DECODE_S))
        assert recorder.wait_for(1), "no partial emitted"
        assert recorder.partials[0] == "实时字幕"
    finally:
        captioner.stop()


def test_finish_reuses_the_live_text_when_it_covers_the_utterance():
    calls = []

    def transcribe(audio):
        calls.append(len(audio))
        return "实时字幕"

    captioner, recorder = make_captioner(transcribe)
    captioner.start()
    try:
        floor = stream_module.MIN_FIRST_DECODE_S
        captioner.begin(speech(1.0), 0.0)
        captioner.feed(speech(floor - 1.0))
        assert recorder.wait_for(1)
        # Give the caption thread a moment to settle on the newest snapshot.
        time.sleep(0.2)
        before = len(calls)
        text = captioner.finish(speech(floor), floor)
        assert text == "实时字幕"
        # The live decode already heard the end, so no extra decode is paid.
        assert len(calls) == before
    finally:
        captioner.stop()


def test_finish_decodes_when_the_tail_was_never_heard():
    calls = []

    def transcribe(audio):
        calls.append(len(audio))
        return f"第{len(calls)}次"

    captioner, recorder = make_captioner(transcribe, step_s=10.0)  # never due
    captioner.start()
    try:
        captioner.begin(speech(1.0), 0.0)
        text = captioner.finish(speech(1.0), 1.0)
        assert text == "第1次"
        assert len(calls) == 1
    finally:
        captioner.stop()


def test_a_late_result_never_lands_after_the_final():
    gate = threading.Event()

    def transcribe(audio):
        if len(audio) > SAMPLE_RATE * 0.8:
            gate.wait(1.0)
        return "迟到的字幕"

    captioner, recorder = make_captioner(transcribe, step_s=0.2)
    captioner.start()
    try:
        captioner.begin(speech(0.5), 0.0)
        captioner.feed(speech(0.5))
        time.sleep(0.15)  # let the decode start and block on the gate
        text = captioner.finish(speech(1.0), 1.0)
        assert text  # final has its own text
        emitted_before = len(recorder.partials)
        gate.set()
        time.sleep(0.4)
        # Nothing may be emitted for the finished utterance.
        assert len(recorder.partials) == emitted_before
    finally:
        captioner.stop()


def test_stop_joins_the_caption_thread():
    captioner, _recorder = make_captioner()
    captioner.start()
    captioner.stop()
    assert not captioner._thread.is_alive()


def test_silence_only_audio_produces_no_caption():
    captioner, recorder = make_captioner(lambda audio: "")
    captioner.start()
    try:
        captioner.begin(silence(0.5), 0.0)
        captioner.feed(silence(0.5))
        time.sleep(0.3)
        assert recorder.partials == []
    finally:
        captioner.stop()


def test_live_step_defaults_are_sane():
    # A step below one decode would redo the same audio; above 1.5 s the
    # caption visibly jumps. Keep the shipped value in that band.
    assert 0.3 <= config.LIVE_STEP_S <= 1.5


def test_finish_does_not_wait_for_a_decode_that_cannot_cover_the_end():
    """Waiting is only worth it when the in-flight decode heard the last words.

    Measured before this rule: a pause that arrived while an early decode was
    still running cost a full FINISH_WAIT_S of dead time, because that decode
    was never going to cover the tail and a second decode had to run anyway.
    """
    gate = threading.Event()
    calls: list[int] = []

    def transcribe(audio):
        calls.append(len(audio))
        if len(calls) == 1:
            gate.wait(2.0)  # first decode is slow and covers only the head
        return "实时字幕"

    captioner, recorder = make_captioner(transcribe, step_s=0.2)
    captioner.start()
    try:
        floor = stream_module.MIN_FIRST_DECODE_S
        captioner.begin(speech(floor), 0.0)  # exactly at the first-decode floor
        assert recorder.wait_for(1, timeout=0.05) is False  # decode still blocked
        started = time.monotonic()
        text = captioner.finish(speech(4.0), 4.0)
        elapsed = time.monotonic() - started
        assert text
        # It went straight to the tail decode instead of waiting 1.6 s.
        assert elapsed < 1.0
        assert len(calls) == 2
    finally:
        gate.set()
        captioner.stop()


def test_a_decode_that_lands_during_finish_is_still_kept():
    """The newest decode must not be thrown away just because finish started."""
    gate = threading.Event()
    calls: list[int] = []

    def transcribe(audio):
        calls.append(len(audio))
        gate.wait(2.0)
        return "实时字幕"

    captioner, recorder = make_captioner(transcribe, step_s=0.2)
    captioner.start()
    try:
        floor = stream_module.MIN_FIRST_DECODE_S
        captioner.begin(speech(1.0), 0.0)
        captioner.feed(speech(floor - 1.0))  # due, covers the whole utterance
        time.sleep(0.1)
        result: list[str] = []
        worker = threading.Thread(
            target=lambda: result.append(captioner.finish(speech(floor), floor))
        )
        worker.start()
        time.sleep(0.1)
        gate.set()  # the in-flight decode lands *while* finish is waiting
        worker.join(timeout=3.0)
        assert result == ["实时字幕"]
        # Reused, not re-decoded.
        assert len(calls) == 1
    finally:
        gate.set()
        captioner.stop()
