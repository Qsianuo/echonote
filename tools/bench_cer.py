#!/usr/bin/env python
"""Compare the installed ASR engines on one wav.

The engine-debate tool. Point it at a clip, optionally at a reference
transcript, and get text + latency + CER per engine:

    python tools/bench_cer.py tests/sample.wav --ref "你好，这是回声笔记……"
    python tools/bench_cer.py noisy-clip.wav              # text only

Engines whose model is not installed are skipped, not failed, so the same
command works on a machine with any subset present. The noise matrix from
the 2026-10-24 review (clean / fan / keyboard x engines) is this script run
once per wav — one row per condition, one engine column each.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app import asr_sensevoice, asr_sherpa, config, models  # noqa: E402


def load_wav(path: Path) -> np.ndarray:
    """Read a 16 kHz mono wav as float32 in [-1, 1]. Anything else is an error."""
    with wave.open(str(path), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        if rate != config.SAMPLE_RATE or channels != 1:
            raise SystemExit(
                f"{path}: {rate} Hz x {channels} ch — 需要 16 kHz 单声道"
                "（先用 ffmpeg -ac 1 -ar 16000 重采样）"
            )
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def core_chars(text: str) -> str:
    """CER is computed on characters: punctuation and spaces removed."""
    return "".join(ch for ch in (text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate = edit distance / reference length."""
    ref, hyp = core_chars(reference), core_chars(hypothesis)
    if not ref:
        return 0.0 if not hyp else float("inf")
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def run_engine(name: str, audio: np.ndarray) -> tuple[str, float] | None:
    """One decode. Returns (text, seconds) or None when the model is absent."""
    if name == "sensevoice":
        if not asr_sensevoice.is_installed():
            return None
        started = time.perf_counter()
        text = asr_sensevoice.transcribe(audio, config.LANGUAGE)
        if text is None:
            return None
        return text, time.perf_counter() - started

    if name == "sherpa":
        if not asr_sherpa.is_installed():
            return None
        started = time.perf_counter()
        text = asr_sherpa.transcribe(audio, config.LANGUAGE)
        if text is None:
            return None
        return text, time.perf_counter() - started

    if name == "whisper":
        # Only run when the weights are on disk: a benchmark must never turn
        # into a surprise 1.6 GB download.
        if config.model_target().path is None:
            return None
        previous = config.ASR_BACKEND
        config.ASR_BACKEND = "whisper"
        try:
            if not stt_is_ready():
                return None
            started = time.perf_counter()
            text = stt_transcribe(audio)
            return text, time.perf_counter() - started
        finally:
            config.ASR_BACKEND = previous

    raise SystemExit(f"unknown engine {name!r}")


_stt_module = None


def _stt():
    global _stt_module
    if _stt_module is None:
        from app import stt as stt_module

        _stt_module = stt_module
    return _stt_module


def stt_is_ready() -> bool:
    return _stt().load()


def stt_transcribe(audio: np.ndarray) -> str:
    return _stt().transcribe(audio, config.LANGUAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark installed ASR engines on one wav")
    parser.add_argument("wav", type=Path, help="16 kHz mono wav clip")
    parser.add_argument("--ref", default="", help="reference transcript for CER")
    parser.add_argument(
        "--engines",
        default="sensevoice,sherpa,whisper",
        help="comma-separated subset of: sensevoice,sherpa,whisper",
    )
    args = parser.parse_args(argv)

    audio = load_wav(args.wav)
    duration = len(audio) / config.SAMPLE_RATE
    print(f"{args.wav.name}: {duration:.2f} s @ {config.SAMPLE_RATE} Hz\n")

    reference = args.ref
    for name in [item.strip() for item in args.engines.split(",") if item.strip()]:
        result = run_engine(name, audio)
        if result is None:
            print(f"[{name:>10}] 模型未安装，跳过")
            continue
        text, elapsed = result
        rtf = f" rtf {elapsed / duration:.2f}" if duration else ""
        line = f"[{name:>10}] {elapsed * 1000:.0f} ms{rtf}: {text or '（空）'}"
        if reference:
            line += f"   CER {cer(reference, text) * 100:.1f}%"
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
