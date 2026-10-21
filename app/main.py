"""EchoNote server: streaming audio in, live transcripts out.

Protocol v2 (see ``app/protocol.py``). The server owns segmentation and
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import config, models, protocol, stt
from .live import LiveCaptioner
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


@app.get("/api/config")
def get_config() -> dict:
    target = config.model_target()
    live = stt.live_target()
    return {
        "version": config.APP_VERSION,
        "stage": config.APP_STAGE,
        "protocol": protocol.PROTOCOL_VERSION,
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
        "max_sessions": config.MAX_CONCURRENT_SESSIONS,
    }


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
        self._dropped_samples = 0
        self._warned_backlog = False

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
        return stt.transcribe(audio, self.language, hotwords=self.hotwords)

    def _emit_partial(self, text: str, start_s: float) -> None:
        self._last_partial = text
        self._emit(protocol.partial(text=text, epoch=self.epoch, start=start_s))

    # ------------------------------------------------------------- worker

    def _run(self) -> None:
        segmenter = Segmenter()
        in_utterance = False
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
                    self._last_partial = ""
                    # Make sure a stale partial line disappears in the UI.
                    self._emit(protocol.partial(text="", epoch=self.epoch, start=0.0))
                    continue

                events = segmenter.push(chunk)
                for event in events:
                    self._complete(event)
                if events:
                    # The segmenter closed the utterance (pause or 30 s cap).
                    in_utterance = False

                if self._live is not None and segmenter.in_speech:
                    if in_utterance:
                        self._live.feed(chunk)
                    else:
                        seed = segmenter.current_audio()
                        if seed is not None:
                            self._live.begin(seed, segmenter.segment_start_s)
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
            return

        self._final_seq += 1
        final_id = f"{self.epoch}-{self._final_seq}"
        self._last_partial = ""
        stable = not stt.refine_enabled()
        self._emit(
            protocol.final(
                final_id=final_id,
                epoch=self.epoch,
                text=text,
                start=start,
                end=end,
                stable=stable,
            )
        )
        if not stable:
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
