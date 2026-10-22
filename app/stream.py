"""Incremental decoding for live captions.

Why this exists
---------------
Whisper pads every call to a 30 s encoder window, so one ``transcribe()`` call
costs the same whether it is fed 1 s or 25 s of audio (measured on this
project's 4-core target: ~3.9 s for ``small``, ~0.9 s for ``base``). That fixed
cost is the whole latency problem: the old pipeline waited for a pause
(900 ms), then decoded the finished utterance once, so the first text of a
10 s sentence appeared ~15 s after the user started talking.

The fix is not a smaller model, it is *when* the decode happens: decode the
utterance while it is still growing, and keep the last result as the live
caption. Latency then stops accumulating with sentence length — it becomes
"one decode", which is the floor on this hardware.

This module holds that policy as pure logic (no model, no threads) so it can
be unit-tested deterministically:

* :class:`UtteranceStream` — the growing audio buffer of one utterance plus
  the best hypothesis so far, with a guard against a hypothesis that suddenly
  loses half the sentence (Whisper does that on truncated audio).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import config

# A hypothesis that is much shorter than the previous one, on *more* audio,
# is a decode failure rather than a correction. Whisper produces those when
# the buffer ends mid-syllable.
_SHRINK_GUARD = 0.55

# Minimum buffer before the first decode of an utterance (see ``due``).
MIN_FIRST_DECODE_S = 2.0


@dataclass(frozen=True)
class Snapshot:
    """Immutable view of the buffer handed to the decoder."""

    audio: np.ndarray
    covered_s: float


class UtteranceStream:
    """One utterance: audio in, incremental captions out.

    ``step_s`` is the minimum amount of *new* audio that must arrive before
    another decode is worth doing. It is deliberately smaller than one decode
    (otherwise the queue never catches up) but large enough that a decoder
    which is slower than real time is not asked to redo the same work twice.
    """

    def __init__(
        self,
        sample_rate: int = config.SAMPLE_RATE,
        step_s: float = config.LIVE_STEP_S,
        max_s: float = config.VAD_MAX_SEGMENT_S,
    ) -> None:
        self.sample_rate = sample_rate
        self.step_s = max(0.2, float(step_s))
        self.max_samples = int(max_s * sample_rate)
        self._audio = np.empty(0, dtype=np.float32)
        self._text = ""
        self._covered_s = 0.0
        self._rejected = 0

    # ------------------------------------------------------------------ feed

    def feed(self, samples: np.ndarray) -> None:
        if samples is None or len(samples) == 0:
            return
        self._audio = np.concatenate((self._audio, samples.astype(np.float32, copy=False)))
        # Hard cap: Whisper only ever sees 30 s, and the VAD splits there
        # anyway. Dropping the oldest audio keeps memory bounded.
        if len(self._audio) > self.max_samples:
            self._audio = self._audio[-self.max_samples :]

    # ------------------------------------------------------------- scheduling

    @property
    def duration_s(self) -> float:
        return len(self._audio) / self.sample_rate

    @property
    def pending_s(self) -> float:
        """New audio since the last decode."""
        return max(0.0, self.duration_s - self._covered_s)

    @property
    def text(self) -> str:
        return self._text

    @property
    def covered_s(self) -> float:
        return self._covered_s

    def due(self, min_audio_s: float = MIN_FIRST_DECODE_S) -> bool:
        """True when another decode would add information.

        The first decode waits for :data:`MIN_FIRST_DECODE_S` rather than the
        0.35 s floor: a sub-second buffer of half a syllable is where Whisper
        hallucinates (the first live measurement produced "以后。" from 0.6 s
        of "这是回声笔记"). Waiting 0.5 s more costs one step and removes a
        whole class of nonsense captions.
        """
        return self.duration_s >= min_audio_s and self.pending_s >= self.step_s

    def snapshot(self) -> Snapshot | None:
        """Audio to decode, or None when there is not enough of it yet."""
        if self.duration_s < 0.35:
            return None
        return Snapshot(audio=self._audio.copy(), covered_s=self.duration_s)

    # ---------------------------------------------------------------- results

    def apply(self, text: str, covered_s: float) -> bool:
        """Store a hypothesis. Returns True when the caption changed.

        Rejects a result that drops most of the sentence on *more* audio:
        that is the model losing its place, not a correction.
        """
        cleaned = (text or "").strip()
        if covered_s < self._covered_s:
            # Out-of-order result (a slow decode finishing after a newer one).
            return False
        if cleaned and self._text:
            grew = covered_s - self._covered_s
            if grew > 0 and len(cleaned) < len(self._text) * _SHRINK_GUARD:
                self._rejected += 1
                return False
        self._covered_s = max(self._covered_s, covered_s)
        if cleaned == self._text:
            return False
        self._text = cleaned
        return True

    def covers_end(self, end_s: float, tolerance_s: float = 0.05) -> bool:
        """True when the last decode already heard the end of the utterance.

        Tolerance is a float-equality buffer, not a policy: a larger one (the
        old 0.25 s) made ``finish`` treat a decode that stopped *before* the
        final syllable as "already covered", so the tail of the sentence was
        never re-decoded and went missing from the transcript.
        """
        return self._covered_s >= end_s - tolerance_s

    def reset(self) -> None:
        self._audio = np.empty(0, dtype=np.float32)
        self._text = ""
        self._covered_s = 0.0
        self._rejected = 0
