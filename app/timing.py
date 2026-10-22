"""Per-turn latency instrumentation.

One :class:`TurnTiming` per finished utterance. It holds absolute
``time.perf_counter()`` stamps and reports them relative to the moment the
VAD closed the utterance — the instant the user stops feeling like they are
waiting for the app.

``perf_counter`` is monotonic, not wall-clock: those numbers can only be
subtracted, never rendered as a date. ``speech_finished_at`` is carried on the
wire as-is for debugging, not for display.
"""

from __future__ import annotations

import time

# The three segments worth reporting, in the order they happen. Fixed on
# purpose: a free-form dict would let a typo silently become a new metric.
_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("first_text", "to_first_text_ms"),
    ("final_text", "to_final_text_ms"),
    ("refined", "to_refined_ms"),
)


class TurnTiming:
    """Latency marks for one utterance, anchored at the end of its speech."""

    def __init__(self, speech_finished_at: float) -> None:
        self.speech_finished_at = float(speech_finished_at)
        self._marks: dict[str, float] = {}

    def mark(self, name: str, at: float | None = None) -> None:
        """Record ``name``. ``at`` defaults to *now* on the monotonic clock."""
        self._marks[name] = time.perf_counter() if at is None else float(at)

    def since(self, name: str) -> float | None:
        """Milliseconds from the end of speech to ``name``, or ``None``.

        The value is signed and unclamped: a live caption stamped *before* the
        pause is a negative number here, which is the honest measurement of
        "the text was already on screen". :meth:`payload` is where that gets
        floored, because the wire contract forbids negative delays.
        """
        at = self._marks.get(name)
        if at is None:
            return None
        return (at - self.speech_finished_at) * 1000.0

    def payload(self, turn_id: str, epoch: int) -> dict:
        """The ``timing`` message body.

        A segment that was never measured is *absent*, never null: a client
        that renders "no refinement happened" as ``0 ms`` would report a
        latency the app never had.
        """
        segments: dict[str, float] = {}
        for name, key in _SEGMENTS:
            value = self.since(name)
            if value is None:
                continue
            # Floored at zero: the origin is the end of speech, so a caption
            # that was already visible at that moment has nothing left to
            # wait for. See ``since`` for the signed value.
            segments[key] = round(max(0.0, value), 1)
        return {
            "type": "timing",
            "epoch": int(epoch),
            "turn_id": str(turn_id),
            "speech_finished_at": round(self.speech_finished_at, 3),
            "segments": segments,
        }
