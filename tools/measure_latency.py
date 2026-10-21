"""Measure what a user actually feels: when does text appear?

Streams a WAV in real time over the WebSocket and records

* **first caption** — seconds from the first audio frame to the first
  ``partial``/``final`` carrying text;
* **final lag** — seconds from the *last* audio frame to each ``final``;
* **refine lag** — seconds from a ``final`` to its ``refine``.

The audio is sent at wall-clock speed, so the numbers include the whole
pipeline: uplink merge, VAD, decode, socket.

Usage:
    python tools/measure_latency.py                       # server on :8000
    python tools/measure_latency.py --url ws://127.0.0.1:8000/ws --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CHUNK_MS = 100


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    if rate != 16000:
        count = int(len(samples) * 16000 / rate)
        positions = np.linspace(0, len(samples) - 1, count).astype(np.int64)
        samples = samples[positions]
    return samples


async def run(url: str, wav: Path, language: str, quiet: bool) -> dict:
    import websockets

    samples = load_wav(wav)
    duration = len(samples) / 16000
    chunk = int(16000 * CHUNK_MS / 1000)
    started = time.monotonic()
    last_sent_at = started
    first_text_at: float | None = None
    finals: list[dict] = []
    partials = 0
    refined: dict[str, float] = {}

    async with websockets.connect(url, max_size=None) as ws:
        status = json.loads(await ws.recv())
        if not status.get("ready"):
            raise SystemExit("model not ready — wait for /api/ready")
        if not quiet:
            print(
                f"live={status.get('live_model_label') or status.get('live_model')} "
                f"final={status.get('model_label') or status.get('model')} "
                f"refine={status.get('refine')}"
            )
        await ws.send(
            json.dumps(
                {
                    "type": "config",
                    "protocol": status.get("protocol"),
                    "language": language,
                    "epoch": 1,
                    "origin_ms": 0,
                }
            )
        )

        async def pump() -> None:
            nonlocal last_sent_at
            for offset in range(0, len(samples), chunk):
                await ws.send(samples[offset : offset + chunk].tobytes())
                last_sent_at = time.monotonic()
                target = started + (offset + chunk) / 16000
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            await ws.send(json.dumps({"type": "flush"}))

        async def listen() -> None:
            nonlocal first_text_at, partials
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=25)
                except asyncio.TimeoutError:
                    return
                message = json.loads(raw)
                kind = message.get("type")
                now = time.monotonic()
                if kind in ("partial", "final") and message.get("text"):
                    if first_text_at is None:
                        first_text_at = now - started
                        if not quiet:
                            print(f"first caption at {first_text_at:.2f}s: {message['text']}")
                if kind == "partial":
                    partials += 1
                    if not quiet:
                        print(f"  partial @{now - started:5.2f}s  {message['text']}")
                elif kind == "final":
                    lag = now - last_sent_at
                    finals.append(
                        {
                            "id": message["id"],
                            "text": message["text"],
                            "stable": message.get("stable", True),
                            "at_s": round(now - started, 2),
                            "lag_s": round(lag, 2),
                        }
                    )
                    if not quiet:
                        print(f"  final   @{now - started:5.2f}s (+{lag:.2f}s) {message['text']}")
                elif kind == "refine":
                    refined[message["id"]] = now - started
                    if not quiet:
                        print(f"  refine  @{now - started:5.2f}s  {message['text']}")
                elif kind == "done":
                    return
                elif kind in ("error", "warning"):
                    print(f"  {kind}: {message}")

        await asyncio.gather(pump(), listen())

    result = {
        "audio_s": round(duration, 2),
        "first_caption_s": None if first_text_at is None else round(first_text_at, 2),
        "partials": partials,
        "finals": finals,
        "final_lag_s": [item["lag_s"] for item in finals],
        "refine_lag_s": {
            key: round(value - next(f["at_s"] for f in finals if f["id"] == key), 2)
            for key, value in refined.items()
        },
        "text": "".join(item["text"] for item in finals),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="?", default=str(ROOT / "tests" / "sample.wav"))
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--json", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(run(args.url, Path(args.wav), args.language, args.quiet))
    print("\n=== summary ===")
    print(f"audio            {result['audio_s']}s")
    print(f"first caption    {result['first_caption_s']}s after the first frame")
    print(f"finals           {len(result['finals'])}  lag {result['final_lag_s']}")
    if result["refine_lag_s"]:
        print(f"refine lag       {result['refine_lag_s']}")
    print(f"text             {result['text']}")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
