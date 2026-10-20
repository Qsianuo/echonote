#!/usr/bin/env python
"""Compare installed models on the same audio: text, latency, RTF, CER.

This is a **regression aid**, not a benchmark suite. The corpus is one
hand-verified 9 s clip plus a repetition case derived from it, so the CER
numbers show relative behaviour between models on this machine — they are not
a claim about general Chinese accuracy. A real corpus needs human-labelled
recordings (see docs/consistency-checklist.md, GRTE-4).

Usage
-----
    python tools/bench_models.py                  # every model on disk
    python tools/bench_models.py --models small large-v3-turbo
    python tools/bench_models.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config, models  # noqa: E402

# Hand-checked transcript of tests/sample.wav. Punctuation is ignored when
# scoring, so a model that adds the wrong comma is not punished.
REFERENCE = "这是回声笔记的实时语音识别测试今天天气很好我们开始录音你好"


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise SystemExit(f"{path} must be 16-bit mono")
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != 16000:
        raise SystemExit(f"{path} must be 16 kHz, got {rate}")
    return audio


def with_pauses(audio: np.ndarray, repeats: int, pause_s: float = 1.5) -> np.ndarray:
    """Repeat a clip with real pauses between copies.

    Long-form input is where Whisper degrades: it either skips a repetition or
    keeps looping on one. A short single utterance would hide that.
    """
    gap = np.zeros(int(16000 * pause_s), dtype=np.float32)
    chunks = []
    for index in range(repeats):
        if index:
            chunks.append(gap)
        chunks.append(audio)
    return np.concatenate(chunks)


def normalize(text: str) -> str:
    drop = set("，。、！？；：,.!?;: \t\n\r\"'“”‘’（）()《》-—…")
    return "".join(ch for ch in text if ch not in drop)


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def cer(hypothesis: str, reference: str) -> float:
    ref = normalize(reference)
    if not ref:
        return 0.0
    return edit_distance(normalize(hypothesis), ref) / len(ref)


def transcribe(model, audio: np.ndarray, prompt: str) -> tuple[str, float]:
    started = time.perf_counter()
    segments, _info = model.transcribe(
        audio,
        language="zh",
        # Exactly the production settings, so the numbers in the README can be
        # reproduced by re-running this script.
        beam_size=config.ASR_BEAM_SIZE,
        best_of=config.ASR_BEST_OF,
        patience=config.ASR_PATIENCE,
        temperature=config.ASR_TEMPERATURES,
        vad_filter=False,
        condition_on_previous_text=config.ASR_CONDITION_ON_PREVIOUS,
        without_timestamps=True,
        initial_prompt=prompt,
        no_speech_threshold=config.ASR_NO_SPEECH_THRESHOLD,
        log_prob_threshold=config.ASR_LOG_PROB_THRESHOLD,
        compression_ratio_threshold=config.ASR_COMPRESSION_RATIO_THRESHOLD,
    )
    text = "".join(segment.text for segment in segments).strip()
    return text, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare models on the same audio")
    parser.add_argument("--models", nargs="*", help="catalog keys (default: all on disk)")
    parser.add_argument("--model-dir", default=str(ROOT / "models"))
    parser.add_argument("--audio", default=str(ROOT / "tests" / "sample.wav"))
    parser.add_argument("--json", help="write results to this path")
    args = parser.parse_args(argv)

    from faster_whisper import WhisperModel

    model_dir = Path(args.model_dir)
    specs = (
        [models.BY_KEY[key] for key in args.models]
        if args.models
        else models.available(model_dir)
    )
    specs = [spec for spec in specs if models.is_complete(models.local_dir(model_dir, spec))]
    if not specs:
        print("no complete model on disk; run tools/fetch_model.py first")
        return 2

    base = load_wav(Path(args.audio))
    prompt = "以下是普通话录音，请使用简体中文和标点符号转写。"
    cases = [
        ("single", base, REFERENCE),
        ("repeat3", with_pauses(base, 3), REFERENCE * 3),
    ]

    results: list[dict] = []
    for spec in specs:
        path = models.local_dir(model_dir, spec)
        print(f"\n=== {spec.key} ({spec.label}, {spec.size_mb} MB) ===")
        started = time.perf_counter()
        engine = WhisperModel(str(path), device="cpu", compute_type="int8")
        load_s = time.perf_counter() - started
        print(f"  load: {load_s:.2f}s")

        for name, audio, reference in cases:
            duration = len(audio) / 16000
            text, elapsed = transcribe(engine, audio, prompt)
            score = cer(text, reference)
            rtf = elapsed / duration
            results.append(
                {
                    "model": spec.key,
                    "case": name,
                    "audio_s": round(duration, 2),
                    "elapsed_s": round(elapsed, 3),
                    "rtf": round(rtf, 3),
                    "cer": round(score, 4),
                    "load_s": round(load_s, 2),
                    "text": text,
                }
            )
            print(
                f"  {name:<8} {duration:5.1f}s audio  {elapsed:6.2f}s  RTF {rtf:4.2f}  "
                f"CER {score * 100:5.2f}%"
            )
            print(f"           {text}")

    print("\n=== summary (single case) ===")
    print(f"{'model':<18}{'RTF':>7}{'CER':>9}{'est. first caption':>22}")
    for row in results:
        if row["case"] != "single":
            continue
        print(
            f"{row['model']:<18}{row['rtf']:>7.2f}{row['cer'] * 100:>8.2f}%"
            f"{row['elapsed_s']:>18.2f}s"
        )

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
