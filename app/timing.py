"""Per-turn latency instrumentation.

One :class:`TurnTiming` per finished utterance. It holds absolute
``time.perf_counter()`` stamps and reports them relative to one of two
origins:

* the moment speech *started* — for the live caption, which appears while the
  user is still talking, so measuring it from the pause would always give a
  negative number;
* the moment speech *ended* — for everything the user actually waits for
  after they stop.

``perf_counter`` is monotonic, not wall-clock: those numbers can only be
subtracted, never rendered as a date.
"""

from __future__ import annotations

import time

# name -> wire key -> which origin. Fixed on purpose: a free-form dict would
# let a typo silently become a new metric.
_SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("first_text", "to_first_text_ms", "start"),
    ("final_text", "to_final_text_ms", "finish"),
    ("refined", "to_refined_ms", "finish"),
)


class TurnTiming:
    """Latency marks for one utterance, anchored at both ends of its speech."""

    def __init__(self, speech_started_at: float, speech_finished_at: float) -> None:
        self.speech_started_at = float(speech_started_at)
        self.speech_finished_at = float(speech_finished_at)
        self._marks: dict[str, float] = {}

    def mark(self, name: str, at: float | None = None) -> None:
        """Record ``name``. ``at`` defaults to *now* on the monotonic clock."""
        self._marks[name] = time.perf_counter() if at is None else float(at)

    def since(self, name: str) -> float | None:
        """Milliseconds from the end of speech to ``name``, or ``None``.

        Deliberately unclamped. For ``final_text`` / ``refined`` a negative
        value means the mark was taken before the pause was detected, which is
        a wiring bug: reporting it as ``0 ms`` would hide a regression as a
        perfect score.
        """
        return self._delta(self.speech_finished_at, name)

    def since_start(self, name: str) -> float | None:
        """Milliseconds from the start of speech to ``name``, or ``None``.

        This is the origin for the first live caption: it appears *during* the
        utterance, so "how long until text showed up" can only be answered
        from the beginning.
        """
        return self._delta(self.speech_started_at, name)

    def _delta(self, origin: float, name: str) -> float | None:
        at = self._marks.get(name)
        if at is None:
            return None
        return (at - origin) * 1000.0

    def payload(self, turn_id: str, epoch: int) -> dict:
        """The ``timing`` message body.

        A segment that was never measured is *absent*, never null: a client
        that renders "no refinement happened" as ``0 ms`` would report a
        latency the app never had.
        """
        segments: dict[str, float] = {}
        for name, key, origin in _SEGMENTS:
            value = self.since_start(name) if origin == "start" else self.since(name)
            if value is None:
                continue
            segments[key] = round(value, 1)
        return {
            "type": "timing",
            "epoch": int(epoch),
            "turn_id": str(turn_id),
            "speech_finished_at": round(self.speech_finished_at, 3),
            "segments": segments,
        }
