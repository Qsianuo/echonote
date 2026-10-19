"""Stream a WAV file to EchoNote over WebSocket, simulating a microphone.

Usage:
    python tests/test_stream.py [path/to/audio.wav]
    python tests/test_stream.py tests/sample.wav --language zh

Requires the server to be running on http://127.0.0.1:8000.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path

import numpy as np
import websockets

TARGET_RATE = 16000
CHUNK_MS = 100


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


async def stream(wav_path: Path, language: str) -> None:
    samples = load_wav(wav_path)
    duration = len(samples) / TARGET_RATE
    print(f"audio: {wav_path} ({duration:.1f}s)")

    async with websockets.connect("ws://127.0.0.1:8000/ws", max_size=None) as ws:
        print("status:", await ws.recv())
        await ws.send(json.dumps({"type": "config", "language": language}))

        chunk = int(TARGET_RATE * CHUNK_MS / 1000)
        for start in range(0, len(samples), chunk):
            await ws.send(samples[start : start + chunk].tobytes())
            await asyncio.sleep(CHUNK_MS / 1000)

        await asyncio.sleep(0.2)
        await ws.send(json.dumps({"type": "flush"}))

        print("-- waiting for transcripts --")
        try:
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=15)
                print(message)
                if json.loads(message).get("type") == "final":
                    # Give the server a moment in case the utterance is split.
                    try:
                        extra = await asyncio.wait_for(ws.recv(), timeout=3)
                        print(extra)
                    except asyncio.TimeoutError:
                        break
        except asyncio.TimeoutError:
            print("(no more messages)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="?", default="tests/sample.wav")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()

    path = Path(args.wav)
    if not path.exists():
        raise SystemExit(f"WAV not found: {path}")
    try:
        asyncio.run(stream(path, args.language))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
