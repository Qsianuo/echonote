"""RG-09 (latency): every turn reports where its milliseconds went.

The point of this module is that "did the optimization help?" becomes a
number in a log line instead of an opinion, so the tests pin the three things
that make the number trustworthy: which origin each segment is measured from,
that an unmeasured segment is absent rather than null, and that a nonsensical
number is reported instead of being flattened.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, main, protocol, stt
from app.timing import TurnTiming
from conftest import FRAME, silence, speech


def test_marks_are_relative_to_speech_finished():
    t = TurnTiming(speech_started_at=97.0, speech_finished_at=100.0)
    t.mark("final_text", 100.9)
    t.mark("refined", 102.14)
    assert t.since("final_text") == pytest.approx(900.0)
    assert t.since("refined") == pytest.approx(2140.0)


def test_marks_are_also_relative_to_speech_started():
    t = TurnTiming(speech_started_at=97.0, speech_finished_at=100.0)
    t.mark("first_text", 98.5)
    assert t.since_start("first_text") == pytest.approx(1500.0)
    assert t.since("first_text") == pytest.approx(-1500.0)


def test_missing_mark_returns_none():
    t = TurnTiming(speech_started_at=97.0, speech_finished_at=100.0)
    assert t.since("never_happened") is None
    assert t.since_start("never_happened") is None


def test_payload_omits_unmeasured_segments():
    t = TurnTiming(speech_started_at=99.0, speech_finished_at=100.0)
    t.mark("first_text", 102.0)
    p = t.payload(turn_id="1-1", epoch=1)
    assert p["segments"] == {"to_first_text_ms": 3000.0}
    assert "to_final_text_ms" not in p["segments"]


def test_payload_maps_every_measured_mark_to_its_key():
    t = TurnTiming(speech_started_at=99.0, speech_finished_at=100.0)
    t.mark("first_text", 100.214)
    t.mark("final_text", 102.9801)
    t.mark("refined", 104.5)
    p = t.payload(turn_id="2-3", epoch=2)
    assert p == {
        "type": "timing",
        "epoch": 2,
        "turn_id": "2-3",
        "speech_finished_at": 100.0,
        # One decimal place: sub-100 µs is noise, and the log line has to
        # stay readable without a formatting step at every call site.
        "segments": {
            # From the start of speech: the caption appeared 1.214 s in.
            "to_first_text_ms": 1214.0,
            # From the end of speech: this is what the user waits for.
            "to_final_text_ms": 2980.1,
            "to_refined_ms": 4500.0,
        },
    }


def test_payload_never_uses_null_for_a_missing_mark():
    t = TurnTiming(speech_started_at=99.0, speech_finished_at=100.0)
    p = t.payload(turn_id="1-1", epoch=1)
    assert p["segments"] == {}
    assert None not in p["segments"].values()


def test_the_first_caption_is_measured_from_the_start_of_speech():
    """The live caption appears *during* the utterance.

    Measuring it from the pause would report a negative delay and, once
    floored to zero, a metric that is always exactly 0 ms — the bug this test
    exists to prevent. It must be a positive time from the start of speech.
    """
    t = TurnTiming(speech_started_at=98.0, speech_finished_at=100.0)
    t.mark("first_text", 99.0)  # a full second before the pause
    first = t.payload(turn_id="1-1", epoch=1)["segments"]["to_first_text_ms"]
    assert first == pytest.approx((99.0 - 98.0) * 1000.0)
    assert first > 0


def test_a_final_that_predates_the_pause_is_not_flattened_to_zero():
    """``final_text`` is measured from the end of speech, so a negative there
    is a wiring bug.

    Clamping it would turn a regression into a perfect 0 ms score, so the value
    is passed through and ``protocol.validate`` rejects it loudly instead.
    """
    t = TurnTiming(speech_started_at=98.0, speech_finished_at=100.0)
    t.mark("final_text", 99.5)
    assert t.since("final_text") == pytest.approx(-500.0)
    payload = t.payload(turn_id="1-1", epoch=1)
    assert payload["segments"]["to_final_text_ms"] == -500.0
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(payload)


def test_mark_defaults_to_the_monotonic_clock():
    t = TurnTiming(speech_started_at=time.perf_counter() - 1.0, speech_finished_at=time.perf_counter())
    t.mark("final_text")
    delta = t.since("final_text")
    assert delta is not None and -1000.0 < delta < 1000.0


# --------------------------------------------------------------------------
# Wiring: the session actually emits it, once per turn and not per partial
# --------------------------------------------------------------------------


def _wait_until_idle(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while main.active_sessions() and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture()
def client():
    with TestClient(main.app) as test_client:
        _wait_until_idle()
        yield test_client


def _send_speech(ws, seconds: float) -> None:
    samples = (speech(seconds) * 32767).astype(np.int16)
    for start in range(0, len(samples) - FRAME + 1, FRAME):
        ws.send_bytes(samples[start : start + FRAME].tobytes())


def _send_silence(ws, seconds: float) -> None:
    samples = (silence(seconds) * 32767).astype(np.int16)
    for start in range(0, len(samples) - FRAME + 1, FRAME):
        ws.send_bytes(samples[start : start + FRAME].tobytes())


def _receive_until(ws, kind: str, limit: int = 40) -> dict:
    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == kind:
            return message
    raise AssertionError(f"no {kind} message within {limit} frames")


def test_a_dropped_misfire_does_not_poison_the_next_sentence(client, monkeypatch):
    """A blip shorter than ``VAD_MIN_SPEECH_MS`` leaves the segmenter as no event.

    ``Segmenter._finalize`` returns ``None`` for it, so the worker sees an
    empty ``events`` list and — before the fix — kept the captioner and the
    turn's start clock pointed at the discarded segment. The next sentence then
    inherited that origin, and ``to_first_text_ms`` silently spanned both.

    The leading silence matters: ``Segmenter.current_audio`` refuses a buffer
    under 250 ms, so a blip needs the 300 ms preroll in front of it before the
    captioner is seeded at all. That is also what makes the misfire reachable
    in a real room, where it is never the first sound of the session.
    """
    monkeypatch.setattr(stt, "is_ready", lambda: True)
    monkeypatch.setattr(stt, "transcribe_live", lambda audio, language=None, **_: "实时字幕")
    monkeypatch.setattr(
        stt, "transcribe", lambda audio, language=None, fast=False, **_: "精修字幕"
    )
    monkeypatch.setattr(stt, "refine_enabled", lambda: False)

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # status

        _send_silence(ws, 0.4)  # fills the preroll ring
        _send_speech(ws, 0.15)  # 150 ms of voice: opens a segment, too short to keep
        _send_silence(ws, 1.2)  # longer than the hangover, so it is finalized as junk
        time.sleep(0.9)  # a stale start clock would now be ~0.9 s too early

        _send_speech(ws, 2.6)  # > MIN_FIRST_DECODE_S, so a caption is produced
        time.sleep(0.6)
        ws.send_text(json.dumps({"type": "flush"}))

        messages: list[dict] = []
        for _ in range(40):
            messages.append(ws.receive_json())
            if any(m["type"] == "timing" for m in messages):
                break

    kinds = [m["type"] for m in messages]
    assert kinds.count("final") == 1
    # The discarded blip never became a turn, so it also produced no timing.
    assert kinds.count("timing") == 1
    segments = next(m for m in messages if m["type"] == "timing")["segments"]
    # Only the second sentence is measured: the partial lands a few ms after
    # that sentence starts, nowhere near the 0.9 s gap in front of it.
    assert segments["to_first_text_ms"] < 600


def test_a_dropped_refinement_does_not_emit_a_second_timing(client, monkeypatch):
    """When the accurate pass returns nothing the turn keeps its one timing.

    The refinement is dropped (`_refine_loop` gets an empty text), so there is
    no ``refine`` and therefore no second ``timing``. The empty flush below is
    the proof: it emits exactly one partial, and a stray timing would have been
    queued ahead of it.
    """
    monkeypatch.setattr(stt, "is_ready", lambda: True)
    monkeypatch.setattr(stt, "transcribe_live", lambda audio, language=None, **_: "实时字幕")
    monkeypatch.setattr(
        stt, "transcribe", lambda audio, language=None, fast=False, **_: ""
    )
    monkeypatch.setattr(stt, "refine_enabled", lambda: True)

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # status
        _send_speech(ws, 1.2)
        ws.send_text(json.dumps({"type": "flush"}))

        final = _receive_until(ws, "final")
        assert final["stable"] is False  # a refinement was promised
        timing = _receive_until(ws, "timing")
        assert "to_refined_ms" not in timing["segments"]

        # Flushing an empty buffer emits one bare partial and nothing else.
        ws.send_text(json.dumps({"type": "flush"}))
        assert ws.receive_json()["type"] == "partial"


def test_both_timings_of_a_turn_agree_on_the_epoch(client, monkeypatch):
    """A mid-turn ``config`` bump must not make the two messages disagree.

    ``final.id`` is "epoch-sequence" and is built from the epoch that was
    current when the turn finished, so the id is the authority. Reading
    ``self.epoch`` on the emission side made the pair drift apart as soon as
    the client bumped it — which it does on every reconnect.
    """
    monkeypatch.setattr(stt, "is_ready", lambda: True)
    monkeypatch.setattr(stt, "transcribe_live", lambda audio, language=None, **_: "实时字幕")

    def slow_accurate(audio, language=None, fast=False, **_):
        time.sleep(0.6)  # keep the refinement in flight across the epoch bump
        return "精修字幕"

    monkeypatch.setattr(stt, "transcribe", slow_accurate)
    monkeypatch.setattr(stt, "refine_enabled", lambda: True)

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # status
        _send_speech(ws, 1.2)
        ws.send_text(json.dumps({"type": "flush"}))

        final = _receive_until(ws, "final")
        first = _receive_until(ws, "timing")

        ws.send_text(
            json.dumps(
                {
                    "type": "config",
                    "protocol": protocol.PROTOCOL_VERSION,
                    "epoch": final["epoch"] + 1,
                    "origin_ms": 0,
                }
            )
        )

        refined = _receive_until(ws, "refine")
        second = _receive_until(ws, "timing")

    assert refined["id"] == final["id"]
    assert first["epoch"] == final["epoch"]
    assert second["epoch"] == final["epoch"]
    assert first["turn_id"] == second["turn_id"] == final["id"]


def test_a_turn_reports_once_per_visible_milestone(client, monkeypatch, caplog):
    """Two timing messages at most: one for the final, one for the refine.

    The failure this guards against is the obvious implementation — emit a
    timing message from ``_emit_partial`` — which would put one message per
    caption update on the wire and turn a diagnostic into traffic.
    """
    monkeypatch.setattr(stt, "is_ready", lambda: True)
    monkeypatch.setattr(stt, "transcribe_live", lambda audio, language=None, **_: "实时字幕")
    monkeypatch.setattr(
        stt, "transcribe", lambda audio, language=None, fast=False, **_: "精修字幕"
    )
    monkeypatch.setattr(stt, "refine_enabled", lambda: True)
    # The opt-in log line is the half of this feature a benchmark run reads.
    monkeypatch.setattr(config, "TIMING_LOG", True)
    caplog.set_level(logging.INFO, logger="echonote")

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # status
        _send_speech(ws, 2.6)  # > UtteranceStream.MIN_FIRST_DECODE_S (2 s)
        # Let the caption thread decode and emit *before* the pause is
        # finalised: a partial emitted after ``finish()`` is suppressed by
        # design, so without this wait the first-caption mark is legitimately
        # absent and the assertion below would be testing the race.
        time.sleep(0.6)
        ws.send_text(json.dumps({"type": "flush"}))

        messages: list[dict] = []
        for _ in range(40):
            messages.append(ws.receive_json())
            if sum(1 for m in messages if m.get("type") == "timing") >= 2:
                break

    kinds = [m["type"] for m in messages]
    timings = [m for m in messages if m["type"] == "timing"]
    assert kinds.count("timing") == 2
    assert kinds.count("final") == 1
    assert kinds.count("refine") == 1
    # The messages the naive implementation would key off: one per caption.
    assert any(m.get("text") for m in messages if m["type"] == "partial")

    final = next(m for m in messages if m["type"] == "final")
    for message in timings:
        assert message["turn_id"] == final["id"]
        assert message["epoch"] == final["epoch"]
        assert isinstance(message["speech_finished_at"], float)
        assert set(message["segments"]) <= {
            "to_first_text_ms",
            "to_final_text_ms",
            "to_refined_ms",
        }
        assert all(v >= 0 for v in message["segments"].values())

    # A partial is on screen, so the first-caption mark exists — and it is
    # measured from the start of speech, so it is a real, positive latency.
    assert timings[0]["segments"]["to_first_text_ms"] > 0
    assert "to_final_text_ms" in timings[0]["segments"]
    assert "to_refined_ms" not in timings[0]["segments"]
    # Same turn, later clock: the refine cannot precede the final.
    assert timings[1]["segments"]["to_refined_ms"] >= timings[1]["segments"]["to_final_text_ms"]
    protocol.validate(timings[1])

    # One [TURN_TIMING] line per message, and it parses as the same payload.
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[TURN_TIMING]")]
    assert len(lines) == 2
    logged = json.loads(lines[1].split(" ", 1)[1])
    assert logged == timings[1]


class _InlineLoop:
    """``StreamingSession._emit`` hops onto the event loop to enqueue messages.

    A real loop is not needed to observe them: running the callback inline on
    the emitting thread keeps ``results`` ordered and lets the test drive the
    session's ``push`` directly.
    """

    def call_soon_threadsafe(self, callback, *args) -> None:
        callback(*args)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _drain(session, kind: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = session.results.get_nowait()
        except asyncio.QueueEmpty:
            time.sleep(0.02)
            continue
        if message.get("type") == kind:
            return message
    raise AssertionError(f"no {kind} message from the session")


def test_a_short_misfire_dropped_in_the_same_push_does_not_poison_the_next_sentence(
    monkeypatch,
):
    """The discard and the next segment's start can land inside one push.

    One push runs several 30 ms VAD frames, so the frame that completes the
    hangover and throws a too-short segment away can be immediately followed by
    the frame that opens the next segment. When the push returns, ``in_speech``
    is True again and ``current_audio()`` was emptied by the discard, so
    ``in_speech`` says nothing about whether the utterance ended. The segment's
    start frame does, which is what the worker keys on now: without it the
    second sentence kept feeding the discarded buffer and inherited its start
    clock.
    """
    monkeypatch.setattr(stt, "is_ready", lambda: True)
    monkeypatch.setattr(stt, "transcribe_live", lambda audio, language=None, **_: "实时字幕")
    monkeypatch.setattr(
        stt, "transcribe", lambda audio, language=None, fast=False, **_: "精修字幕"
    )
    monkeypatch.setattr(stt, "refine_enabled", lambda: False)

    session = main.StreamingSession(_InlineLoop(), session_id=91)
    session.start()
    try:
        # Preroll first: ``current_audio`` refuses a buffer under 250 ms, so a
        # 150 ms burst only opens a segment when there is audio in front of it.
        session.push(np.concatenate((silence(0.4), speech(0.15))))
        assert _wait_until(lambda: session._segment_start_frame is not None)
        misfire_started_at = session._turn_started_at
        assert misfire_started_at is not None

        time.sleep(0.9)  # a stale clock would now be ~0.9 s behind
        before_push = time.perf_counter()

        # ONE push: the leading silence finishes the hangover and discards the
        # burst, and the sentence behind it opens the next segment in the same
        # ``Segmenter.push`` call.
        session.push(np.concatenate((silence(0.95), speech(2.6))))

        assert _wait_until(lambda: session._first_text_at is not None)
        assert session._turn_started_at is not None
        # Rebuilt for the second segment, not inherited from the first.
        assert session._turn_started_at > misfire_started_at
        assert session._turn_started_at >= before_push

        session.flush()
        final = _drain(session, "final")
        timing = _drain(session, "timing")
        leftovers = []
        while True:
            try:
                leftovers.append(session.results.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        session.stop()
        assert session.wait_done(5.0)

    # The discarded burst never became a row, so it never reported a latency.
    assert final["id"] == "1-1"
    assert [m for m in leftovers if m["type"] == "timing"] == []
    assert timing["turn_id"] == final["id"]
    # Only the second sentence is measured.
    assert timing["segments"]["to_first_text_ms"] < 600
