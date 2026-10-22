"""Live caption pipeline: decode the utterance while it is still growing.

Two threads cooperate:

* the session worker owns the VAD and calls :meth:`LiveCaptioner.feed` /
  :meth:`LiveCaptioner.begin` / :meth:`LiveCaptioner.finish`;
* the caption thread inside this class pulls the newest snapshot, decodes it
  and emits a ``partial``.

Why a thread instead of decoding inline: one decode of the live model takes
~0.9 s on the 4-core target. Doing that on the VAD thread would stall
segmentation for the same 0.9 s, which lands directly on top of the "how long
after I stop talking does the sentence appear" number.

The mailbox holds **one** snapshot, newest wins. If a decode is slower than
the arrival rate, older snapshots are dropped rather than queued — replaying
stale audio would only add latency, never information.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from . import config
from .stream import Snapshot, UtteranceStream

log = logging.getLogger("echonote.live")


class LiveCaptioner:
    """Owns one utterance's growing audio and its incremental caption."""

    # How long ``finish`` waits for a decode that is already running. One
    # decode of the live model is ~1.1 s on the 4-core target; the cap only
    # exists so a wedged decode cannot hold the utterance hostage.
    FINISH_WAIT_S = 1.6

    def __init__(
        self,
        *,
        transcribe,
        emit,
        epoch,
        language=lambda: config.LANGUAGE,
        step_s: float | None = None,
    ) -> None:
        self._transcribe = transcribe
        self._emit = emit
        self._epoch = epoch
        self._language = language
        self._stream = UtteranceStream(step_s=step_s if step_s is not None else config.LIVE_STEP_S)

        self._cond = threading.Condition()
        self._job: tuple[int, Snapshot, float] | None = None
        self._gen = 0
        self._finishing = False
        self._inflight = False
        # How much audio the in-flight decode was given. ``finish`` uses it to
        # decide whether waiting can possibly pay off.
        self._inflight_covered = 0.0
        self._stop = False
        self._start_s = 0.0
        self._thread = threading.Thread(target=self._loop, name="live-caption", daemon=True)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._job = None
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    # ------------------------------------------------------------------ input

    def begin(self, audio: np.ndarray, start_s: float) -> None:
        """A new utterance started: seed the buffer with the audio so far."""
        with self._cond:
            self._gen += 1
            self._stream.reset()
            self._stream.feed(audio)
            self._start_s = start_s
            self._job = None
            self._finishing = False
            self._schedule_locked()

    def feed(self, samples: np.ndarray) -> None:
        with self._cond:
            self._stream.feed(samples)
            self._schedule_locked()

    def _schedule_locked(self) -> None:
        if self._finishing or self._job is not None or not self._stream.due():
            return
        snapshot = self._stream.snapshot()
        if snapshot is None:
            return
        self._job = (self._gen, snapshot, self._start_s)
        self._cond.notify_all()

    # ---------------------------------------------------------------- results

    def finish(self, audio: np.ndarray, end_s: float) -> str:
        """End the utterance and return its text.

        Reuses the live result when it already heard the whole utterance
        (the common case after a pause: the last decode during the hangover
        covered everything), otherwise pays for one more decode so the last
        words are never dropped.
        """
        with self._cond:
            self._finishing = True
            self._job = None
            # An in-flight decode holds the freshest audio anyone has heard.
            # Dropping it (the previous behaviour) threw away up to 1.2 s of
            # work and then paid for the same audio again below, which is
            # exactly the "text appears after the pause" delay this class
            # exists to remove. Wait for it instead — but only when it can
            # still cover the end of the utterance: if it was scheduled before
            # the last words, waiting is dead time and the tail decode below
            # has to run anyway (measured: that wait alone added 1.6 s).
            # Wait for the in-flight decode only when it can still cover the
            # end of the *buffer* (the voiced end plus the trailing coda), not
            # just ``end_s``. Otherwise the coda is never decoded and the
            # final falls through to a full re-decode of the same audio.
            if self._inflight_covered >= max(end_s, self._stream.duration_s) - 0.05:
                deadline = time.monotonic() + self.FINISH_WAIT_S
                while self._inflight and time.monotonic() < deadline:
                    self._cond.wait(0.02)
            reusable = self._stream.covers_end(end_s) and bool(self._stream.text)
            text = self._stream.text

        if not reusable:
            try:
                text = self._transcribe(audio) or text
            except Exception:  # noqa: BLE001 - one bad utterance must not kill the session
                log.exception("Final live decode failed")

        with self._cond:
            self._gen += 1
            self._stream.reset()
            self._finishing = False
            self._job = None
            self._cond.notify_all()
        return (text or "").strip()

    def reset(self) -> None:
        with self._cond:
            self._gen += 1
            self._stream.reset()
            self._job = None

    @property
    def text(self) -> str:
        with self._cond:
            return self._stream.text

    # ----------------------------------------------------------------- worker

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._job is None and not self._stop:
                    self._cond.wait(0.25)
                if self._stop:
                    return
                gen, snapshot, start_s = self._job
                self._job = None
                self._inflight = True
                self._inflight_covered = snapshot.covered_s

            try:
                text = self._transcribe(snapshot.audio)
            except Exception:  # noqa: BLE001 - captions are best effort
                log.exception("Live decode failed")
                text = None

            with self._cond:
                self._inflight = False
                # A decode that landed while the utterance was being finalised
                # is still worth keeping: ``finish`` reads the stream text and
                # may be able to reuse it instead of decoding again.
                if text is None or gen != self._gen:
                    self._cond.notify_all()
                    continue
                changed = self._stream.apply(text, snapshot.covered_s)
                payload = self._stream.text if changed else ""
                finishing = self._finishing
                self._cond.notify_all()

            if payload and not finishing:
                self._emit(payload, start_s)
