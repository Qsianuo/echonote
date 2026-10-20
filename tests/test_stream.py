"""Stream a WAV file to EchoNote over WebSocket, simulating a microphone.

Usage:
    python tests/test_stream.py [path/to/audio.wav]
    python tests/test_stream.py tests/sample.wav --language zh

Requires the server to be running (default http://127.0.0.1:8000), with the
model already loaded — check ``/api/ready`` first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np
import websockets

TARGET_RATE = 16000
CHUNK_MS = 100  # protocol v2 requires >= 100 ms per frame (PRD D-06)


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit("Expected 16-bit mono WAV")
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)

    if rate != TARGET_RATE:
        count = int(len(samples) * TARGET_RATE / rate)
        positions = np.linspace(0, len(samples) - 1, count).astype(np.int64)
        samples = samples[positions]
    return samples


async def stream(url: str, wav_path: Path, language: str) -> int:
    samples = load_wav(wav_path)
    duration = len(samples) / TARGET_RATE
    print(f"audio: {wav_path} ({duration:.1f}s)")

    finals: list[dict] = []
    started = time.monotonic()

    async with websockets.connect(url, max_size=None) as ws:
        status = json.loads(await ws.recv())
        print(f"status: protocol={status.get('protocol')} ready={status.get('ready')}")
        if not status.get("ready"):
            print("model is not ready — wait for /api/ready before streaming")
            return 2

        await ws.send(
            json.dumps(
                {
                    "type": "config",
                    "protocol": "2",
                    "language": language,
                    "epoch": 1,
                    "origin_ms": 0,
                }
            )
        )

        chunk = int(TARGET_RATE * CHUNK_MS / 1000)
        for start in range(0, len(samples), chunk):
            frame = samples[start : start + chunk]
            await ws.send(frame.tobytes())
            await asyncio.sleep(CHUNK_MS / 1000)

        await asyncio.sleep(0.2)
        await ws.send(json.dumps({"type": "flush"}))

        print("-- waiting for transcripts --")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                break
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "final":
                finals.append(message)
                print(
                    f"final  id={message['id']} epoch={message['epoch']} "
                    f"{message['start']:.2f}-{message['end']:.2f}s  {message['text']}"
                )
            elif kind == "partial":
                print(f"partial {message['text']!r}")
            elif kind in ("error", "warning"):
                print(f"{kind}: {message}")
            elif kind == "done":
                break

    print(f"-- {len(finals)} final(s) in {time.monotonic() - started:.1f}s --")
    return 0 if finals else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="?", default="tests/sample.wav")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    args = parser.parse_args()

    path = Path(args.wav)
    if not path.exists():
        raise SystemExit(f"WAV not found: {path}")
    try:
        sys.exit(asyncio.run(stream(args.url, path, args.language)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
