"""EchoNote server: streaming audio in, live transcripts out.

Protocol v4 (see ``app/protocol.py``). The server owns segmentation and
inference; the client owns the recording timeline. Each WebSocket session
carries an ``epoch`` and an ``origin_ms`` so that reconnects do not reset
timestamps (PRD FR-010 / FR-040).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import queue
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import asr_sensevoice, asr_sherpa, config, glossary, models, protocol, stt
from .live import LiveCaptioner
from .timing import TurnTiming
from .vad import Segmenter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("echonote")

LANGUAGES = [
    {"code": "auto", "label": "自动识别"},
    {"code": "zh", "label": "中文"},
    {"code": "en", "label": "English"},
    {"code": "ja", "label": "日本語"},
    {"code": "ko", "label": "한국어"},
    {"code": "fr", "label": "Français"},
    {"code": "de", "label": "Deutsch"},
    {"code": "es", "label": "Español"},
    {"code": "ru", "label": "Русский"},
]

_SESSION_IDS = itertools.count(1)

_active_sessions = 0
_active_lock = threading.Lock()


def active_sessions() -> int:
    """Number of sessions currently holding a slot (used by tests/ops)."""
    with _active_lock:
        return _active_sessions


def _acquire_slot() -> bool:
    """Reserve one of the ``MAX_CONCURRENT_SESSIONS`` slots (D-12)."""
    global _active_sessions
    with _active_lock:
        if _active_sessions >= config.MAX_CONCURRENT_SESSIONS:
            return False
        _active_sessions += 1
        return True


def _release_slot() -> None:
    """Idempotent release: a cancelled request must never leak a slot."""
    global _active_sessions
    with _active_lock:
        if _active_sessions > 0:
            _active_sessions -= 1


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Defence in depth: even if the process is started via `uvicorn ...` with
    # ECHONOTE_HOST set to a LAN address, refuse without the explicit opt-in.
    check_bind(config.HOST)
    log.info(
        "EchoNote %s (%s), protocol v%s",
        config.APP_VERSION,
        config.APP_STAGE,
        protocol.PROTOCOL_VERSION,
    )
    if config.PRELOAD:
        stt.preload()
    else:
        log.info("Model preload disabled (ECHONOTE_SKIP_PRELOAD)")
    try:
        yield
    finally:
        stt.shutdown()


app = FastAPI(
    title="EchoNote",
    description="Local recording with live speech-to-text",
    version=config.APP_VERSION,
    lifespan=lifespan,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Headers required by PRD §12.4."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response


app.add_middleware(SecurityHeadersMiddleware)


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    """Liveness only. Says nothing about the model (PRD FR-044)."""
    return {"ok": True}


@app.get("/api/ready")
def ready() -> dict:
    """Readiness: the only probe the frontend may gate on."""
    return stt.status()


@app.get("/api/cloud-diag")
def cloud_diag() -> dict:
    """Cloud-deploy diagnostics, opt-in via ECHONOTE_CLOUD_DIAG=1.

    Reports the model directory layout and the tail of the cloud fetch log so
    a headless sandbox can be debugged from the outside. Disabled by default:
    on a personal machine it would leak directory listings (PRD FR-020).
    """
    import os as _os
    from pathlib import Path

    if not _os.getenv("ECHONOTE_CLOUD_DIAG"):
        raise HTTPException(status_code=404, detail="not found")

    model_dir = Path(config.MODEL_DIR)
    layout: dict[str, list[str]] = {}
    if model_dir.is_dir():
        for child in sorted(model_dir.iterdir()):
            if child.is_dir():
                layout[child.name] = sorted(p.name for p in child.iterdir())
            else:
                layout.setdefault("(files)", []).append(child.name)

    log_tail: list[str] = []
    for log_path in (Path("/tmp/echonote-fetch.log"), Path("/workspace/app.log")):
        try:
            log_tail.append(f"===== {log_path} =====")
            log_tail.extend(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:])
        except OSError:
            log_tail.append("(not found)")

    return {"model_dir": str(model_dir), "layout": layout, "fetch_log": log_tail}


@app.get("/api/status")
def status() -> dict:
    """Backwards-compatible alias of /api/ready."""
    return stt.status()


@app.post("/api/retry")
def retry_model() -> dict:
    """Retry a failed model load without restarting the process (FR-036)."""
    return stt.retry()


@app.get("/api/models")
def list_models() -> dict:
    """Which models exist on this machine, and which one is loaded.

    The UI only offers installed entries: switching must never become an
    accidental download.
    """
    target = config.model_target()
    installed = {spec.key for spec in models.available(config.MODEL_DIR)}
    return {
        "current": target.key,
        "current_path": target.path,
        "offline_ready": target.path is not None,
        "items": [
            {
                "key": spec.key,
                "label": spec.label,
                "size_mb": spec.size_mb,
                "tier": spec.tier,
                "note": spec.note,
                "installed": spec.key in installed,
            }
            for spec in models.CATALOG
        ],
    }


@app.post("/api/model")
def switch_model(payload: dict) -> dict:
    key = str((payload or {}).get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "缺少 key"}, status_code=400)
    result = stt.switch(key)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/mode")
def switch_mode(payload: dict) -> dict:
    """Switch the recording-mode preset (personal / meeting).

    Applies from the **next** recording: a session's ``Segmenter`` is built
    when the recording starts, so the segmentation rules of one utterance
    never change mid-utterance. The choice is sticky across restarts.
    """
    name = str((payload or {}).get("mode") or "").strip().lower()
    if name not in config.MODES:
        return JSONResponse(
            {"ok": False, "error": f"未知模式 {name!r}，可选：{' / '.join(config.MODES)}"},
            status_code=400,
        )
    config.ACTIVE_MODE = name
    config.remember_mode(name)
    log.info("Recording mode set to %s (applies from the next recording)", name)
    return {"ok": True, "mode": name}


@app.get("/api/config")
def get_config() -> dict:
    target = config.model_target()
    live = stt.live_target()
    payload = {
        "version": config.APP_VERSION,
        "stage": config.APP_STAGE,
        "protocol": protocol.PROTOCOL_VERSION,
        # "local" or "remote": where recognition actually runs. The UI has to
        # say which, because only one of them keeps the audio on this machine.
        "asr_backend": (
            "sherpa"
            if config.ASR_BACKEND == "sherpa"
            else (
                "sensevoice"
                if config.ASR_BACKEND == "sensevoice"
                else (
                    "remote"
                    if (config.ASR_REMOTE or config.ASR_REMOTE_ONLY)
                    else "local"
                )
            )
        ),
        "asr_remote_model": config.ASR_REMOTE_MODEL
        if (config.ASR_REMOTE or config.ASR_REMOTE_ONLY)
        else None,
        "model": target.loader_value,
        "model_label": target.label,
        "model_key": target.key,
        "offline_ready": target.path is not None,
        "offline_mode": config.offline_mode(),
        "sample_rate": config.SAMPLE_RATE,
        "language": config.LANGUAGE,
        "languages": LANGUAGES,
        "live_step_s": config.LIVE_STEP_S,
        "live_captions": config.LIVE_CAPTIONS,
        "live_model": live.loader_value,
        "live_model_key": live.key,
        "live_model_label": live.label,
        "live_separate": stt.live_is_separate(),
        "refine": stt.refine_enabled(),
        "max_record_minutes": config.MAX_RECORD_MINUTES,
        "memory_budget_bytes": config.RECORDING_MEMORY_BUDGET_BYTES,
        "min_frame_ms": config.MIN_FRAME_MS,
        "max_frame_bytes": config.MAX_FRAME_BYTES,
        "queue_max_seconds": config.AUDIO_QUEUE_MAX_SECONDS,
        "hotwords": config.HOTWORDS or "",
        "max_sessions": config.MAX_CONCURRENT_SESSIONS,
        # Recording-mode preset ("personal" / "meeting"); the UI offers the
        # named list, the server owns the parameter sets behind them.
        "mode": config.ACTIVE_MODE,
    }
    if config.ASR_BACKEND == "sherpa":
        # The whisper names describe a model that is not loaded and will not
        # be used. Reporting "large-v3-turbo / 高精度" while the sherpa
        # recognizer does the work is what put a wrong model name in the
        # exported transcript header and in the model picker.
        payload.update(
            model=asr_sherpa.label(),
            model_label="sherpa 流式",
            model_key="sherpa",
            offline_ready=asr_sherpa.is_installed(),
            live_model=asr_sherpa.label(),
            live_model_key="sherpa",
            live_model_label="sherpa 流式",
            live_separate=False,
            refine=False,
        )
    elif config.ASR_BACKEND == "sensevoice":
        # SenseVoice runs the finals; the whisper names would describe a model
        # that is not loaded. The live caption runs on another engine, which
        # is why ``live_separate`` is True here and not in the sherpa branch.
        payload.update(
            model=asr_sensevoice.label(),
            model_label="SenseVoice 离线",
            model_key="sensevoice",
            offline_ready=asr_sensevoice.is_installed(),
            live_separate=True,
            refine=stt.refine_enabled(),
        )
        if stt.live_backend() == "sherpa":
            payload.update(
                live_model=asr_sherpa.label(),
                live_model_key="sherpa",
                live_model_label="本地流式",
            )
    elif config.ASR_REMOTE and stt.live_backend() == "sherpa":
        # Hybrid: remote finals, local caption. The whisper names in the
        # payload would describe a model that is not loaded, and the picker
        # would then disagree with what the pipeline actually runs.
        payload.update(
            live_model=asr_sherpa.label(),
            live_model_key="sherpa",
            live_model_label="本地流式",
            live_separate=True,
        )
    return payload


# --------------------------------------------------------------------------
# Streaming session
# --------------------------------------------------------------------------

FLUSH = object()  # sentinel: transcribe whatever is buffered, keep the session alive


class StreamingSession:
    """One WebSocket connection = one recording session.

    Three threads, each with a single job:

    * ``_run`` — the VAD. Owns segmentation and the transcript timeline.
    * ``LiveCaptioner`` — decodes the utterance while it grows (``partial``).
    * ``_refine_loop`` — re-decodes finished utterances with the accurate
      model and replaces the provisional text (``refine``).

    They exist separately because their costs differ by an order of
    magnitude: 0.9 s for a live decode, ~4 s for an accurate one on the
    4-core target. Sharing one thread made the accurate pass starve the live
    caption, which is precisely the "说一句等十几秒" complaint.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, session_id: int) -> None:
        self.loop = loop
        self.id = session_id
        self.language = config.LANGUAGE
        # Per-session glossary. Measured on `tiny`: 31.0% -> 10.3% CER on the
        # same clip, which is a bigger lever than every decoder knob combined.
        self.hotwords: str | None = config.HOTWORDS
        self.epoch = 1
        self.origin_ms = 0
        self.results: asyncio.Queue = asyncio.Queue()

        self._audio: queue.Queue = queue.Queue(maxsize=config.AUDIO_QUEUE_MAX_FRAMES)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._final_seq = 0
        self._last_partial = ""
        self._last_final_text = ""
        self._dropped_samples = 0
        self._warned_backlog = False

        # Latency instrumentation. ``_first_text_at`` is stamped by the
        # caption thread while the user is still talking, i.e. before the
        # turn's TurnTiming exists; ``_turn_started_at`` is the other end of
        # that measurement (the first live caption is reported from the start
        # of speech, since it appears before the pause); ``_timings`` holds the
        # marks of turns whose refinement has not landed yet, keyed by
        # ``final.id``.
        #
        # ``_segment_start_frame`` is the identity of the segment the captioner
        # is following. It is how "is this still the same utterance?" gets
        # answered: ``in_speech`` cannot answer it, because one push can hold
        # both the frame that ends a segment and the frame that starts the next
        # one (see ``_run``).
        self._first_text_at: float | None = None
        self._turn_started_at: float | None = None
        self._segment_start_frame: int | None = None
        self._timings: dict[str, TurnTiming] = {}

        self._live: LiveCaptioner | None = None
        if config.LIVE_CAPTIONS:
            self._live = LiveCaptioner(
                transcribe=self._decode_live,
                emit=self._emit_partial,
                epoch=lambda: self.epoch,
            )

        # Refinement queue is deliberately tiny: it is a background nicety,
        # and an unbounded one would let a slow accurate model fall hours
        # behind a meeting while holding every utterance's audio in memory.
        # Unbounded on purpose: the backlog is capped by *audio seconds* below,
        # which is what actually bounds memory. An item cap dropped work during
        # ordinary speech and the export silently kept the live-model text.
        self._refine: queue.Queue = queue.Queue()
        self._refine_pending = 0
        self._refine_seconds = 0.0
        self._refine_lock = threading.Lock()
        self._refine_dropped = 0
        self._warned_refine = False
        # Foreground state the refinement thread yields to. Two decodes of
        # different sizes sharing four cores is what turned a 0.9 s tail
        # decode into 3.9 s; refinement therefore only *starts* while the user
        # is silent and no final is in flight.
        self._speaking = False
        self._foreground = False

        self._thread = threading.Thread(
            target=self._run, name=f"stt-session-{session_id}", daemon=True
        )
        self._refine_thread = threading.Thread(
            target=self._refine_loop, name=f"refine-{session_id}", daemon=True
        )

    # ------------------------------------------------------------- control

    def start(self) -> None:
        if self._live is not None:
            self._live.start()
        self._refine_thread.start()
        self._thread.start()

    def push(self, samples: np.ndarray) -> None:
        """Enqueue audio. On overflow drop the *oldest* frame and warn once.

        Dropping silently is forbidden by anti-goal 8, so the warning is
        emitted to the client (FR-038 AC3/AC4).
        """
        if samples is None or len(samples) == 0:
            return
        while True:
            try:
                self._audio.put_nowait(samples)
                return
            except queue.Full:
                try:
                    dropped = self._audio.get_nowait()
                except queue.Empty:
                    continue
                if isinstance(dropped, np.ndarray):
                    self._dropped_samples += int(dropped.size)
                self._maybe_warn_backlog()

    def _maybe_warn_backlog(self) -> None:
        if self._warned_backlog or self._dropped_samples <= 0:
            return
        self._warned_backlog = True
        dropped_ms = int(self._dropped_samples * 1000 / config.SAMPLE_RATE)
        log.warning("Session %s dropped %d ms of backlog audio", self.id, dropped_ms)
        self._emit(protocol.warning(code="audio_backlog_dropped", dropped_ms=dropped_ms))

    def flush(self) -> None:
        """End the current utterance even if the user stopped mid-sentence."""
        self._audio.put(FLUSH)

    def stop(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            self._audio.put(None)  # sentinel: process the backlog, then flush

    def wait_done(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def set_config(
        self,
        language: str | None,
        epoch: object,
        origin_ms: object,
        hotwords: object = None,
    ) -> None:
        """Apply a client ``config`` message.

        The language only affects segments that start *after* this call, so
        already-produced finals are never rewritten (FR-006 AC6).
        """
        if language:
            self.language = str(language)
        if epoch is not None:
            try:
                value = int(epoch)
            except (TypeError, ValueError):
                value = self.epoch
            if value > self.epoch:
                self.epoch = value
                self._final_seq = 0
                self._last_final_text = ""
        if origin_ms is not None:
            try:
                self.origin_ms = max(0, int(origin_ms))
            except (TypeError, ValueError):
                pass
        if hotwords is not None:
            # Whitespace-separated terms; capped so a paste of an entire
            # document cannot blow up the decoder prompt.
            text = " ".join(str(hotwords).split())[: config.HOTWORDS_MAX_CHARS]
            self.hotwords = text or None

    # ------------------------------------------------------------- decoding

    def _decode_live(self, audio: np.ndarray) -> str:
        """One live caption decode (runs on the caption thread)."""
        return stt.transcribe_live(audio, self.language, hotwords=self.hotwords)

    def _decode_final(self, audio: np.ndarray) -> str:
        text = stt.transcribe(audio, self.language, hotwords=self.hotwords)
        # 术语纠错放在定稿这一步：只有这里拿得到完整句子，也只能改"最终文本"，
        # 不动实时字幕（边出字时句子还在长，纠错会在半句上误判）。
        if text and config.GLOSSARY_TERMS:
            text = glossary.correct(text, config.GLOSSARY_TERMS)
        return text

    def _emit_partial(self, text: str, start_s: float) -> None:
        self._last_partial = text
        if self._first_text_at is None:
            # First caption of this utterance (runs on the caption thread, so
            # it lands *before* the VAD closes the segment). Stamped here and
            # applied to the TurnTiming that ``_complete`` builds, because the
            # origin of the clock — the end of speech — is not known yet.
            self._first_text_at = time.perf_counter()
        self._emit(protocol.partial(text=text, epoch=self.epoch, start=start_s))

    def _end_utterance_state(self) -> None:
        """Forget everything that belonged to the utterance that just ended.

        Called from *every* exit of the worker loop, including the one with no
        message at all: the segmenter drops a segment shorter than
        ``VAD_MIN_SPEECH_MS`` without an event (``Segmenter._finalize`` returns
        ``None``), and leaving the state set made the next sentence inherit this
        one's start clock and first-caption mark — ``to_first_text_ms`` then
        spanned two utterances. The captioner is reset for the same reason: it
        was still holding the discarded audio and would have kept feeding the
        next sentence into that buffer.
        """
        self._turn_started_at = None
        self._first_text_at = None
        self._segment_start_frame = None
        self._last_partial = ""
        if self._live is not None:
            self._live.reset()

    # ------------------------------------------------------------- worker

    def _run(self) -> None:
        # The mode preset lands here, at session start: a switch made while a
        # recording is running applies from the next recording, so the
        # segmentation rules of one utterance never change mid-utterance.
        segmenter = Segmenter(**config.mode_overrides())
        in_utterance = False
        debug = config.VAD_DEBUG
        dbg_peak = 0.0
        dbg_ms = 0.0
        try:
            while True:
                try:
                    chunk = self._audio.get(timeout=0.1)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue

                if chunk is None:  # stop sentinel
                    break

                if chunk is FLUSH:
                    tail = segmenter.flush()
                    if tail is not None:
                        self._complete(tail)
                    in_utterance = False
                    self._speaking = False
                    self._end_utterance_state()
                    # Make sure a stale partial line disappears in the UI.
                    self._emit(protocol.partial(text="", epoch=self.epoch, start=0.0))
                    continue

                events = segmenter.push(chunk)
                if debug:
                    dbg_peak = max(dbg_peak, segmenter.last_rms)
                    dbg_ms += len(chunk) * 1000.0 / config.SAMPLE_RATE
                    if dbg_ms >= 1000.0:
                        # One line a second: is the microphone delivering a
                        # level the gate can open on, and where is the gate?
                        log.info(
                            "[VAD] peak=%.4f thr=%.4f floor=%.4f speaking=%s",
                            dbg_peak,
                            segmenter.last_threshold,
                            segmenter.noise_floor,
                            segmenter.in_speech,
                        )
                        dbg_peak = 0.0
                        dbg_ms = 0.0
                for event in events:
                    if debug:
                        log.info(
                            "[SEG] dur=%.2fs audio_ms=%d",
                            event["end"] - event["start"],
                            int(len(event["audio"]) * 1000 / config.SAMPLE_RATE),
                        )
                    self._complete(event)

                # "Still the same utterance?" is a question about segment
                # identity, not about ``in_speech``: one push runs several
                # 30 ms VAD frames, so the frame that ends a discarded segment
                # and the frame that opens the next one can land in the same
                # call — by the time this line runs, ``in_speech`` is already
                # True again. The start frame changes whenever a segment opens,
                # so it catches both exits: the one with an event and the one
                # where the segmenter threw a too-short segment away.
                if events or (
                    self._segment_start_frame is not None
                    and segmenter.segment_start_frame != self._segment_start_frame
                ):
                    in_utterance = False
                    self._end_utterance_state()

                if self._live is not None and segmenter.in_speech:
                    if in_utterance:
                        self._live.feed(chunk)
                    else:
                        seed = segmenter.current_audio()
                        if seed is not None:
                            self._live.begin(seed, segmenter.segment_start_s)
                            # Origin for ``to_first_text_ms``: this is the
                            # moment the utterance became capturable. Taken
                            # here rather than in the segmenter so ``vad.py``
                            # stays free of wall-clock reads.
                            self._turn_started_at = time.perf_counter()
                            # Recorded only after a begin that actually
                            # happened: a None seed has to stay retryable on
                            # the next frame, or the segment would never be
                            # captioned at all.
                            self._segment_start_frame = segmenter.segment_start_frame
                            in_utterance = True
                self._speaking = bool(segmenter.in_speech)

            tail = segmenter.flush()
            if tail is not None:
                self._complete(tail)
        except Exception:  # pragma: no cover - defensive
            log.exception("Session %s worker crashed", self.id)
        finally:
            self._maybe_warn_backlog()
            self._speaking = False
            if self._live is not None:
                self._live.stop()
            # Finals still waiting for their accurate pass: give them a
            # bounded chance to land, because a dropped refinement means the
            # exported text silently stays at live-model quality.
            self._drain_refine(config.REFINE_DRAIN_TIMEOUT_S)
            self._emit(protocol.done())
            self._done.set()

    def _complete(self, event: dict) -> None:
        """Turn one finished utterance into a ``final`` (+ a refinement)."""
        audio = event["audio"]
        start, end = event["start"], event["end"]
        # The two origins of the measurement. ``finished_at`` is a
        # perf_counter reading taken when the VAD closed the utterance (the
        # instant the user starts feeling like they are waiting); the start is
        # the one recorded when the live captioner picked the utterance up and
        # falls back to the timeline the segmenter reported when no captioner
        # ran (live captions disabled, or a segment that began before it was
        # seeded). Consumed here so the next turn cannot inherit it.
        started_at, self._turn_started_at = self._turn_started_at, None
        if started_at is None:
            started_at = event["finished_at"] - (end - start)
        turn = TurnTiming(started_at, event["finished_at"])
        # Mark 1/3: the first partial was stamped on the caption thread, before
        # this object existed. Consuming it clears the flag for the next turn —
        # including on every early return below, which is why it is taken here
        # and not next to the emit.
        first_text_at, self._first_text_at = self._first_text_at, None
        if first_text_at is not None:
            turn.mark("first_text", first_text_at)
        self._foreground = True
        try:
            if self._live is not None:
                # The caption thread has been decoding all along; this either
                # reuses that result or pays for one last decode of the tail.
                text = self._live.finish(audio, end - start)
            else:
                text = self._decode_final(audio)
        except Exception:  # noqa: BLE001 - one bad segment must not end the session
            log.exception("Final transcription failed")
            return
        finally:
            self._foreground = False

        text = (text or "").strip()
        if not text:
            # The segment reached the decoder and came back empty — either the
            # transcript was filtered (boilerplate, low confidence) or no
            # speech was recognised at all.
            if config.VAD_DEBUG:
                log.info("[ASR-DROP] empty text, dur=%.2fs", end - start)
            return

        # The same text twice in a row on a *short* segment is one
        # hallucination, not two sentences. Seen in the field: room noise split
        # by the VAD into ~1 s blips, each decoding to "转写。" on `tiny` — the
        # same row at 00:13, 00:20 and 00:21, which reads as a stuck app. The
        # duration guard is the point: a real sentence said twice after a pause
        # is longer than REPEAT_MAX_DURATION_S and keeps both rows, because
        # dropping the second one would lose words from the transcript.
        if (
            stt.same_line(text, self._last_final_text)
            and (end - start) < config.REPEAT_MAX_DURATION_S
        ):
            log.info(
                "Session %s: repeated final %r on a %.2fs segment, not adding another row",
                self.id,
                text,
                end - start,
            )
            return

        self._final_seq += 1
        final_id = f"{self.epoch}-{self._final_seq}"
        # One epoch for the whole turn, read back out of the id it was baked
        # into. Taking it from ``self.epoch`` again on the emission side let a
        # client `config` (which bumps the epoch on every reconnect) land
        # between the two writes, so the same turn could report two epochs.
        epoch = int(final_id.split("-")[0])
        self._last_partial = ""
        stable = not stt.refine_enabled()
        self._emit(
            protocol.final(
                final_id=final_id,
                epoch=epoch,
                text=text,
                start=start,
                end=end,
                stable=stable,
            )
        )
        self._last_final_text = text
        # Mark 2/3: the text is on screen. Reported once per turn — this is
        # the ``to_final_text_ms`` number that every latency claim is about.
        turn.mark("final_text")
        self._emit_timing(final_id, epoch, turn)
        if not stable:
            # Kept until the accurate pass lands (``_refine_loop``) or the job
            # is dropped, whichever comes first.
            self._timings[final_id] = turn
            self._enqueue_refine(final_id, audio, start, end)

    # ------------------------------------------------------------- refine

    def _enqueue_refine(self, final_id: str, audio: np.ndarray, start: float, end: float) -> None:
        held = len(audio) / config.SAMPLE_RATE
        with self._refine_lock:
            # Only overflow drops work, and only when the accurate model is
            # genuinely behind by minutes. The oldest entry goes first: it is
            # the one whose provisional text has been on screen longest.
            while self._refine_seconds + held > config.REFINE_BACKLOG_SECONDS:
                try:
                    dropped = self._refine.get_nowait()
                except queue.Empty:
                    break
                self._refine_pending = max(0, self._refine_pending - 1)
                self._refine_seconds = max(
                    0.0, self._refine_seconds - len(dropped[1]) / config.SAMPLE_RATE
                )
                self._refine_dropped += 1
                # Its refinement will never land, so its latency marks are
                # dead weight; dropping them keeps ``_timings`` bounded by the
                # queue instead of by the length of the recording.
                self._timings.pop(dropped[0], None)
                self._refine.task_done()
                if not self._warned_refine:
                    self._warned_refine = True
                    log.warning(
                        "Session %s refinement backlog over %.0fs; dropping the oldest",
                        self.id,
                        config.REFINE_BACKLOG_SECONDS,
                    )
                    self._emit(
                        protocol.warning(code="refine_backlog_dropped", dropped_ms=0)
                    )
            self._refine.put((final_id, audio, start, end))
            self._refine_pending += 1
            self._refine_seconds += held

    def _refine_loop(self) -> None:
        while True:
            job = self._refine.get()
            if job is None:
                return
            # Yield to anything the user is waiting for: a decode that shares
            # four cores with the live caption makes both of them late.
            while not self._stop.is_set() and (self._speaking or self._foreground):
                time.sleep(0.05)
            final_id, audio, start, end = job
            try:
                text = self._decode_final(audio)
            except Exception:  # noqa: BLE001 - provisional text stays valid
                log.exception("Refine decode failed")
                text = ""
            finally:
                with self._refine_lock:
                    self._refine_pending = max(0, self._refine_pending - 1)
                    self._refine_seconds = max(
                        0.0, self._refine_seconds - len(audio) / config.SAMPLE_RATE
                    )
                self._refine.task_done()
            text = (text or "").strip()
            # Taken before the emit: a refinement that decodes to nothing has
            # no marks to report, and leaving them behind would leak.
            turn = self._timings.pop(final_id, None)
            if not text:
                continue
            try:
                epoch = int(final_id.split("-")[0])
            except ValueError:
                epoch = self.epoch
            self._emit(
                protocol.refine(
                    final_id=final_id, epoch=epoch, text=text, start=start, end=end
                )
            )
            if turn is not None:
                # Mark 3/3: the accurate text replaced the provisional one.
                # The second and last ``timing`` message of this turn.
                turn.mark("refined")
                self._emit_timing(final_id, epoch, turn)

    def _drain_refine(self, timeout: float) -> None:
        with self._refine_lock:
            pending_s = self._refine_seconds
        # Wait for what is actually queued, not a fixed number: exporting a
        # sentence before its accurate pass lands is the one silent way this
        # app can hand back worse text than it produced.
        budget = min(config.REFINE_DRAIN_MAX_S, max(timeout, pending_s * 2.5))
        if pending_s > 0:
            log.info("Draining %.1fs of queued refinement (budget %.0fs)", pending_s, budget)
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            with self._refine_lock:
                if self._refine_pending <= 0:
                    break
            time.sleep(0.05)
        # Stop the thread; anything still queued after the deadline keeps the
        # provisional text, which is exactly what `stable: false` promised.
        self._refine.put(None)

    def _emit_timing(self, turn_id: str, epoch: int, turn: TurnTiming) -> None:
        """Report one turn's latency breakdown (v4 ``timing`` message).

        The only place a ``timing`` message is built. That is what bounds a
        turn to two of them — one after its ``final``, one after its
        ``refine`` — because those are the only two call sites, and each runs
        at most once per turn id.
        """
        payload = turn.payload(turn_id=turn_id, epoch=epoch)
        if config.TIMING_LOG:
            log.info("[TURN_TIMING] %s", json.dumps(payload, ensure_ascii=False))
        self._emit(
            protocol.timing(
                epoch=payload["epoch"],
                turn_id=payload["turn_id"],
                speech_finished_at=payload["speech_finished_at"],
                segments=payload["segments"],
            )
        )

    def _emit(self, message: dict) -> None:
        protocol.validate(message)
        try:
            self.loop.call_soon_threadsafe(self.results.put_nowait, message)
        except RuntimeError:
            pass  # event loop already closed


# --------------------------------------------------------------------------
# WebSocket endpoint
# --------------------------------------------------------------------------


def _origin_allowed(origin: str | None) -> bool:
    """Reject cross-origin browser connections (PRD §12.4).

    Non-browser clients (no Origin header) are allowed because they still
    have to be on the loopback interface unless a token was configured.
    """
    if not origin:
        return True
    parsed = urlparse(origin)
    host = (parsed.hostname or "").lower()
    if config.is_loopback(host):
        return True
    if origin.rstrip("/") in config.ALLOWED_ORIGINS:
        return True
    return host in config.ALLOWED_ORIGINS


def check_bind(host: str) -> None:
    """Refuse to start on a non-loopback interface without explicit consent.

    Raises ``SystemExit`` with an actionable message (PRD D-05, GATE-7).
    """
    if config.is_loopback(host):
        return
    if not config.ALLOW_NONLOCAL:
        raise SystemExit(
            "[EchoNote] 拒绝在 %s 上启动：默认只允许本机访问。\n"
            "如需局域网访问，请设置 ECHONOTE_ALLOW_NONLOCAL=1 与 "
            "ECHONOTE_ACCESS_TOKEN=<令牌>，并配置 ECHONOTE_ALLOWED_ORIGINS。" % host
        )
    if not config.ACCESS_TOKEN:
        raise SystemExit(
            "[EchoNote] 非回环地址必须设置 ECHONOTE_ACCESS_TOKEN，否则任何能访问该端口的人都能打开你的麦克风。"
        )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    if not _origin_allowed(ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    if config.ACCESS_TOKEN and ws.query_params.get("token") != config.ACCESS_TOKEN:
        await ws.close(code=1008)
        return

    await ws.accept()

    if not _acquire_slot():
        await ws.send_json(
            protocol.error(
                code="busy",
                message=f"已有 {config.MAX_CONCURRENT_SESSIONS} 个会话进行中，请 {config.BUSY_RETRY_AFTER_S} 秒后重试",
                retryable=True,
            )
        )
        await ws.close(code=1013)
        return

    loop = asyncio.get_running_loop()
    session = StreamingSession(loop, next(_SESSION_IDS))
    session.start()
    log.info("Session %s connected", session.id)

    await ws.send_json(protocol.status(**stt.status()))

    async def send_results() -> None:
        while True:
            message = await session.results.get()
            done = message.get("type") == "done"
            try:
                await ws.send_json(message)
            except Exception:  # client already gone
                return
            if done:
                return

    sender = asyncio.create_task(send_results())
    warned_not_ready = False

    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break

            payload = message.get("bytes")
            if payload is not None:
                if len(payload) > config.MAX_FRAME_BYTES:
                    await ws.send_json(
                        protocol.error(
                            code="frame_too_large",
                            message=f"单个音频帧超过 {config.MAX_FRAME_BYTES // 1024} KiB，已停止接收",
                            retryable=False,
                        )
                    )
                    break
                if len(payload) % 2:  # Int16 requires an even byte count
                    continue
                if not stt.is_ready():
                    if not warned_not_ready:
                        warned_not_ready = True
                        await ws.send_json(
                            protocol.error(
                                code="model_not_ready",
                                message="模型尚未就绪，音频已丢弃，请稍候",
                                retryable=True,
                            )
                        )
                    continue
                samples = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
                session.push(samples)
                continue

            text = message.get("text")
            if text:
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = data.get("type")
                if kind == "config":
                    incoming = str(data.get("protocol", protocol.PROTOCOL_VERSION))
                    if incoming != protocol.PROTOCOL_VERSION:
                        await ws.send_json(
                            protocol.error(
                                code="unsupported_protocol",
                                message="客户端协议版本不匹配，请刷新页面",
                                retryable=False,
                            )
                        )
                        continue
                    session.set_config(
                        data.get("language"),
                        data.get("epoch"),
                        data.get("origin_ms"),
                        data.get("hotwords"),
                    )
                elif kind == "flush":
                    session.flush()

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        session.stop()
        finished = False
        cancelled = False
        try:
            # Give the worker up to 30 s to flush its tail so produced finals
            # are never truncated (FR-041 AC3).
            finished = await asyncio.to_thread(
                session.wait_done, config.SESSION_TAIL_TIMEOUT_S
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # This block must run even when the request task is cancelled,
            # otherwise the concurrency slot leaks and every later client
            # gets `busy` (found while testing RG-05).
            sender.cancel()
            _release_slot()
            if cancelled:
                log.info("Session %s cancelled; worker finishes in background", session.id)
            elif finished:
                log.info("Session %s closed (tail ok)", session.id)
            else:
                log.warning(
                    "Session %s tail abandoned after %.0fs",
                    session.id,
                    config.SESSION_TAIL_TIMEOUT_S,
                )
            try:
                await ws.close()
            except Exception:
                pass


# Static frontend. Keep this mount last so /api and /ws take precedence.
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")


@app.exception_handler(Exception)
async def unhandled_exception(_request, exc: Exception) -> JSONResponse:  # pragma: no cover
    log.exception("Unhandled error: %s", exc)
    # Never leak paths or tracebacks to the browser (PRD FR-020 AC2).
    return JSONResponse(status_code=500, content={"ok": False, "error": "服务器内部错误，请查看控制台日志"})


def run() -> None:
    import uvicorn

    check_bind(config.HOST)
    # Pass the app object, not "app.main:app": string imports rely on the
    # module being importable by name, which is fragile inside a PyInstaller
    # bundle (the module is loaded as __main__'s sibling, not as app.main).
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info",
    )


if __name__ == "__main__":
    run()
