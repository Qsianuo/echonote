"""Lightweight energy-based voice activity detection and segmentation.

The browser streams raw 16 kHz mono PCM. This segmenter splits the stream
into utterances: a segment starts when the signal rises above an adaptive
noise floor and ends after a short period of silence.
"""

from __future__ import annotations

import logging
import time
from collections import deque

import numpy as np

from . import config

log = logging.getLogger("echonote.vad")

FRAME_MS = 30


class Segmenter:
    def __init__(
        self,
        sample_rate: int = config.SAMPLE_RATE,
        threshold: float = config.VAD_THRESHOLD,
        hangover_ms: int = config.VAD_HANGOVER_MS,
        min_speech_ms: int = config.VAD_MIN_SPEECH_MS,
        max_segment_s: float = config.VAD_MAX_SEGMENT_S,
        preroll_ms: int = config.VAD_PREROLL_MS,
        gate_ratio: float = 3.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * FRAME_MS / 1000)
        self.base_threshold = threshold
        # How far above the *tracked room floor* a frame must stand to count
        # as voice (``threshold = max(base_threshold, floor * gate_ratio)``).
        # The recording-mode presets tune this: a meeting room has a louder
        # floor than a desk, so it needs a wider margin, while the absolute
        # gate stays put to keep far-field speech audible.
        self.gate_ratio = float(gate_ratio)
        # 650 ms / 30 ms -> 21 frames = 630 ms effective
        self.hangover_frames = max(1, int(hangover_ms / FRAME_MS))
        # 280 ms / 30 ms -> 9 frames = 270 ms effective
        self.min_speech_frames = max(1, int(min_speech_ms / FRAME_MS))
        self.max_segment_frames = max(1, int(max_segment_s * 1000 / FRAME_MS))
        # Speech is only confirmed after min_speech_ms of voice, and the frames
        # before that point used to be discarded — quiet onsets (soft vowels,
        # unvoiced consonants) lost their first tenths of a second. Keep the
        # most recent non-speech frames so they can be glued back on.
        self.preroll_frames = max(0, preroll_ms // FRAME_MS)
        self._ring: deque[np.ndarray] = deque(maxlen=max(1, self.preroll_frames))

        self._pending = np.empty(0, dtype=np.float32)
        self.total_frames = 0  # number of frames consumed since session start
        self.noise_floor = 0.004

        self.in_speech = False
        self.frames: list[np.ndarray] = []
        self.speech_frames = 0
        self.silence_run = 0
        self.segment_start_frame = 0
        self.last_speech_frame = 0

        # Diagnostics (see config.VAD_DEBUG): the most recent frame level and
        # the threshold it was compared against. Reading these from the worker
        # is the only way to tell "the mic is quiet" apart from "the gate is
        # too high" without guessing.
        self.last_rms = 0.0
        self.last_threshold = self.base_threshold
        # Loudest frame seen since the current segment started, for diagnostics.
        self.segment_peak_rms = 0.0

    # ------------------------------------------------------------------ API

    def push(self, samples: np.ndarray) -> list[dict]:
        """Feed audio and return any completed segments."""
        if samples is None or len(samples) == 0:
            return []

        self._pending = np.concatenate(
            (self._pending, samples.astype(np.float32, copy=False))
        )

        events: list[dict] = []
        while len(self._pending) >= self.frame_size:
            frame = self._pending[: self.frame_size]
            self._pending = self._pending[self.frame_size :]
            event = self._process_frame(frame)
            if event is not None:
                events.append(event)
        return events

    def current_audio(self) -> np.ndarray | None:
        """Audio of the in-progress utterance (for partial transcripts)."""
        if not self.in_speech or not self.frames:
            return None

        # Keep the same tail window as ``_finalize`` so the live caption sees
        # the coda too. Clipping at ``last_speech_frame`` here was the other
        # half of "the trailing syllable is missing": the partial never heard
        # it, so the partial text ended early and the final had to re-decode
        # from scratch.
        keep = self.last_speech_frame - self.segment_start_frame + 1
        tail_frames = max(1, int(config.VAD_TAIL_KEEP_MS / FRAME_MS))
        keep = max(1, min(keep + tail_frames, len(self.frames)))
        audio = np.concatenate(self.frames[:keep])
        if len(audio) < self.sample_rate * 0.25:
            return None
        return audio

    @property
    def segment_start_s(self) -> float:
        """Session-relative start of the in-progress utterance, in seconds."""
        return self.segment_start_frame * FRAME_MS / 1000.0

    def flush(self) -> dict | None:
        """Finalize whatever is currently buffered (end of session)."""
        if not self.in_speech:
            return None
        return self._finalize()

    # ------------------------------------------------------------- internal

    def _process_frame(self, frame: np.ndarray) -> dict | None:
        rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
        threshold = max(self.base_threshold, self.noise_floor * self.gate_ratio)
        is_voice = rms > threshold
        self.last_rms = rms
        self.last_threshold = threshold
        if rms > self.segment_peak_rms:
            self.segment_peak_rms = rms

        # Adapt the noise floor only when the frame is clearly not speech.
        if rms < max(self.base_threshold * 0.6, self.noise_floor * 1.5):
            self.noise_floor = 0.90 * self.noise_floor + 0.10 * rms
        elif not self.in_speech and rms < threshold:
            self.noise_floor = 0.995 * self.noise_floor + 0.005 * rms

        event = None

        if is_voice:
            if not self.in_speech:
                self.in_speech = True
                self.frames = []
                self.speech_frames = 0
                self.silence_run = 0
                self.segment_start_frame = self.total_frames
                n_pre = len(self._ring)
                if n_pre:
                    # The preroll frames become the head of the utterance, so
                    # the timestamp has to move back with them: without this
                    # the audio and the exported SRT drift apart by 300 ms.
                    self.frames = list(self._ring)
                    self.segment_start_frame = max(0, self.segment_start_frame - n_pre)
                self._ring.clear()
            self.silence_run = 0
            self.speech_frames += 1
            self.frames.append(frame.copy())
            self.last_speech_frame = self.total_frames

            if len(self.frames) >= self.max_segment_frames:
                event = self._finalize()
        elif self.in_speech:
            self.frames.append(frame.copy())
            self.silence_run += 1
            if self.silence_run >= self.hangover_frames:
                event = self._finalize()
        elif self.preroll_frames > 0:
            # Not speaking yet: hold the last preroll_frames frames in case
            # speech starts with a quiet onset.
            self._ring.append(frame.copy())

        self.total_frames += 1
        return event

    def _finalize(self) -> dict | None:
        if not self.frames:
            self._reset_segment()
            return None

        # Trailing audio belongs to the utterance. The old cut kept only
        # ``last_speech_frame + 150 ms`` and threw the rest away, which is what
        # clipped the soft coda of a spoken Chinese sentence ("呢", "啊",
        # trailing nasal) and left single-character rows ("文") hanging at the
        # end. The tail is kept unless it is clearly just silence.
        #
        # The silence test is per-frame, not a mean over the whole tail
        # window: a 300 ms trailing syllable followed by 700 ms of true
        # silence averages *below* the gate and used to be misread as "pure
        # silence", cutting the very coda we are trying to keep. One frame
        # above the silence floor anywhere in the tail is enough to keep the
        # whole window.
        keep = len(self.frames)
        tail_frames = max(1, int(config.VAD_TAIL_KEEP_MS / FRAME_MS))
        if keep > tail_frames:
            tail_audio = np.concatenate(self.frames[-tail_frames:])
            # Silence floor: anything this low is room tone, not speech. Well
            # below the VAD gate so a soft coda is never mistaken for it.
            silence_floor = self.base_threshold * 0.15
            tail_has_signal = bool(np.any(np.abs(tail_audio) > silence_floor))
            if not tail_has_signal:
                # Pure silence past the last voiced frame: trim back to the
                # configured tail window, never further.
                keep = min(
                    keep,
                    (self.last_speech_frame - self.segment_start_frame + 1) + tail_frames,
                )
        audio = np.concatenate(self.frames[:keep]) if keep else None

        start = self.segment_start_frame * FRAME_MS / 1000.0
        # ``end`` is the last *voiced* frame, as before: SRT cues and the
        # "did the sentence end here" check both want the moment the user
        # stopped making sound, not the moment the buffer was handed over.
        # The audio buffer is deliberately allowed to run a little past it —
        # that extra is the soft coda we now keep, not a timestamp bug.
        end = (self.last_speech_frame + 1) * FRAME_MS / 1000.0
        # Snapshot before _reset_segment() clears them.
        speech_frames = self.speech_frames
        peak_rms = self.segment_peak_rms
        speech_ok = speech_frames >= self.min_speech_frames

        self._reset_segment()

        if not speech_ok or audio is None or len(audio) < self.sample_rate * 0.25:
            # Silent until now, and the single most common reason a spoken
            # sentence never reaches the transcript: the gate opened for too
            # few frames, or the segment was too short to be worth decoding.
            if config.VAD_DEBUG:
                log.info(
                    "[SEG-DROP] speech_frames=%d/%d audio_ms=%d peak=%.4f thr=%.4f floor=%.4f",
                    speech_frames,
                    self.min_speech_frames,
                    0 if audio is None else int(len(audio) * 1000 / self.sample_rate),
                    peak_rms,
                    self.last_threshold,
                    self.noise_floor,
                )
            return None
        return {
            "type": "final",
            "audio": audio,
            "start": start,
            "end": end,
            "finished_at": time.perf_counter(),
        }

    def _reset_segment(self) -> None:
        self.in_speech = False
        self.frames = []
        self.speech_frames = 0
        self.silence_run = 0
        self.last_speech_frame = self.segment_start_frame
        self.segment_peak_rms = 0.0
