"""Lightweight energy-based voice activity detection and segmentation.

The browser streams raw 16 kHz mono PCM. This segmenter splits the stream
into utterances: a segment starts when the signal rises above an adaptive
noise floor and ends after a short period of silence.
"""

from __future__ import annotations

import numpy as np

from . import config

FRAME_MS = 30


class Segmenter:
    def __init__(
        self,
        sample_rate: int = config.SAMPLE_RATE,
        threshold: float = config.VAD_THRESHOLD,
        hangover_ms: int = config.VAD_HANGOVER_MS,
        min_speech_ms: int = config.VAD_MIN_SPEECH_MS,
        max_segment_s: float = config.VAD_MAX_SEGMENT_S,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * FRAME_MS / 1000)
        self.base_threshold = threshold
        # 650 ms / 30 ms -> 21 frames = 630 ms effective
        self.hangover_frames = max(1, int(hangover_ms / FRAME_MS))
        # 280 ms / 30 ms -> 9 frames = 270 ms effective
        self.min_speech_frames = max(1, int(min_speech_ms / FRAME_MS))
        self.max_segment_frames = max(1, int(max_segment_s * 1000 / FRAME_MS))

        self._pending = np.empty(0, dtype=np.float32)
        self.total_frames = 0  # number of frames consumed since session start
        self.noise_floor = 0.004

        self.in_speech = False
        self.frames: list[np.ndarray] = []
        self.speech_frames = 0
        self.silence_run = 0
        self.segment_start_frame = 0
        self.last_speech_frame = 0

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

        keep = self.last_speech_frame - self.segment_start_frame + 1
        keep = max(1, min(keep, len(self.frames)))
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
        threshold = max(self.base_threshold, self.noise_floor * 3.0)
        is_voice = rms > threshold

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

        self.total_frames += 1
        return event

    def _finalize(self) -> dict | None:
        if not self.frames:
            self._reset_segment()
            return None

        # Keep a little tail after the last voiced frame, drop the rest.
        keep = self.last_speech_frame - self.segment_start_frame + 1
        tail = int(0.15 * 1000 / FRAME_MS)  # 150 ms of trailing audio
        keep = min(len(self.frames), keep + tail)
        audio = np.concatenate(self.frames[:keep]) if keep else None

        start = self.segment_start_frame * FRAME_MS / 1000.0
        end = (self.last_speech_frame + 1) * FRAME_MS / 1000.0
        speech_ok = self.speech_frames >= self.min_speech_frames

        self._reset_segment()

        if not speech_ok or audio is None or len(audio) < self.sample_rate * 0.25:
            return None
        return {"type": "final", "audio": audio, "start": start, "end": end}

    def _reset_segment(self) -> None:
        self.in_speech = False
        self.frames = []
        self.speech_frames = 0
        self.silence_run = 0
        self.last_speech_frame = self.segment_start_frame
