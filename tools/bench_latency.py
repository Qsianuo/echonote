"""Measure the fixed cost of one ``transcribe()`` call.

The whole latency question reduces to one number: how long does a single
decode take, and does it depend on how much audio is in the buffer? Whisper
pads every call to a 30 s encoder window, so if the cost is flat we can only
buy latency by making each call cheaper (smaller model / more threads), not by
feeding it less audio.

Usage:
    python tools/bench_latency.py --models small tiny --threads 4 8
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        assert handle.getframerate() == 16000, handle.getframerate()
        assert handle.getnchannels() == 1
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def bench(model_dir: Path, key: str, threads: int, clips: list[float], audio: np.ndarray) -> dict:
    from faster_whisper import WhisperModel

    load_start = time.perf_counter()
    model = WhisperModel(
        str(model_dir / f"faster-whisper-{key}"),
        device="cpu",
        compute_type="int8",
        local_files_only=True,
        cpu_threads=threads,
    )
    load_s = time.perf_counter() - load_start

    rows = []
    for seconds in clips:
        chunk = audio[: int(16000 * seconds)]
        # Warm up once so thread-pool spin-up is not billed to the first clip.
        model.transcribe(chunk, language="zh", beam_size=1, without_timestamps=True)[0]
        samples = []
        text = ""
        for _ in range(3):
            started = time.perf_counter()
            segments, _info = model.transcribe(
                chunk,
                language="zh",
                beam_size=1,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            text = "".join(s.text for s in segments).strip()
            samples.append(time.perf_counter() - started)
        rows.append(
            {
                "audio_s": seconds,
                "call_s_median": round(statistics.median(samples), 3),
                "calls": [round(v, 3) for v in samples],
                "text": text,
            }
        )
    return {"model": key, "threads": threads, "load_s": round(load_s, 2), "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["small"])
    parser.add_argument("--threads", nargs="+", type=int, default=[4])
    parser.add_argument("--clips", nargs="+", type=float, default=[1.0, 2.0, 3.0, 5.0, 9.0])
    parser.add_argument("--wav", default=str(ROOT / "tests" / "sample.wav"))
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    audio = read_wav(Path(args.wav))
    model_dir = ROOT / "models"
    results = []
    for key in args.models:
        for threads in args.threads:
            print(f"\n=== {key} / {threads} threads ===")
            result = bench(model_dir, key, threads, args.clips, audio)
            results.append(result)
            print(f"load {result['load_s']}s")
            for row in result["rows"]:
                print(
                    f"  {row['audio_s']:>4.1f}s audio -> {row['call_s_median']:>6.3f}s per call "
                    f"(rtf {row['call_s_median'] / row['audio_s']:.2f})  {row['text'][:28]}"
                )

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
