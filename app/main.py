"""EchoNote server: streaming audio in, live transcripts out."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, stt
from .vad import Segmenter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("echonote")

app = FastAPI(title="EchoNote", description="Local recording with live speech-to-text", version="1.0.0")

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


@app.on_event("startup")
def _on_startup() -> None:
    stt.preload()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "model": config.MODEL_NAME}


@app.get("/api/status")
def status() -> dict:
    return stt.status()


@app.get("/api/config")
def get_config() -> dict:
    return {
        "model": config.MODEL_NAME,
        "sample_rate": config.SAMPLE_RATE,
        "language": config.LANGUAGE,
        "languages": LANGUAGES,
        "partial_interval": config.PARTIAL_INTERVAL_S,
    }


FLUSH = object()  # sentinel: transcribe whatever is buffered, keep the session alive


class StreamingSession:
    """One WebSocket connection = one recording session."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.language = config.LANGUAGE
        self.results: asyncio.Queue = asyncio.Queue()

        self._audio: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._final_id = 0
        self._last_partial = ""
        self._thread = threading.Thread(target=self._run, name="stt-session", daemon=True)

    # ------------------------------------------------------------- control

    def start(self) -> None:
        self._thread.start()

    def push(self, samples: np.ndarray) -> None:
        self._audio.put(samples)

    def flush(self) -> None:
        """End the current utterance even if the user stopped mid-sentence."""
        self._audio.put(FLUSH)

    def stop(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            self._audio.put(None)  # sentinel: process the backlog, then flush

    def set_language(self, language: str | None) -> None:
        if language:
            self.language = language

    # ------------------------------------------------------------- worker

    def _run(self) -> None:
        segmenter = Segmenter()
        partial_clock = time.monotonic()

        while True:
            try:
                chunk = self._audio.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue

            if chunk is None:  # stop sentinel
                break

            if chunk is FLUSH:
                tail = segmenter.flush()
                if tail is not None:
                    self._finalize(tail)
                self._last_partial = ""
                # Make sure a stale partial line disappears in the UI.
                self._emit({"type": "partial", "text": ""})
                continue

            for event in segmenter.push(chunk):
                self._finalize(event)

            now = time.monotonic()
            if (
                now - partial_clock >= config.PARTIAL_INTERVAL_S
                and segmenter.in_speech
                and self._audio.empty()  # don't fall behind: finish the backlog first
            ):
                partial_clock = now
                audio = segmenter.current_audio()
                if audio is not None:
                    text = stt.transcribe(audio, self.language, fast=True)
                    if text and text != self._last_partial:
                        self._last_partial = text
                        self._emit({"type": "partial", "text": text})

        tail = segmenter.flush()
        if tail is not None:
            self._finalize(tail)

        self._emit({"type": "done"})

    def _finalize(self, event: dict) -> None:
        text = stt.transcribe(event["audio"], self.language)
        if not text:
            return
        self._final_id += 1
        self._last_partial = ""
        self._emit(
            {
                "type": "final",
                "id": self._final_id,
                "text": text,
                "start": round(event["start"], 2),
                "end": round(event["end"], 2),
            }
        )

    def _emit(self, message: dict) -> None:
        try:
            self.loop.call_soon_threadsafe(self.results.put_nowait, message)
        except RuntimeError:
            pass  # event loop already closed


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()

    session = StreamingSession(loop)
    session.start()

    state = stt.status()
    await ws.send_json(
        {
            "type": "status",
            "ready": state["ready"],
            "model": state["model"],
            "error": state["error"],
        }
    )

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

    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break

            payload = message.get("bytes")
            if payload:
                samples = (
                    np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
                )
                session.push(samples)
                continue

            text = message.get("text")
            if text:
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "config":
                    session.set_language(data.get("language"))
                elif data.get("type") == "flush":
                    session.flush()

    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        session.stop()
        try:
            await asyncio.wait_for(sender, timeout=30)
        except Exception:
            sender.cancel()


# Static frontend. Keep this mount last so /api and /ws take precedence.
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")


@app.exception_handler(Exception)
async def unhandled_exception(_request, exc: Exception) -> JSONResponse:  # pragma: no cover
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
    )


if __name__ == "__main__":
    run()
