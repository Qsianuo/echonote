"""Compare the local model against the remote backend on one file.

Usage::

    python tools/bench_remote.py docs/examples/echonote-20261020-2056.wav \
        --reference docs/examples/echonote-20261020-2056.txt --local base

Reports, per backend: wall time, real-time factor and character error rate
against the reference. The reference defaults to a ``.txt`` sitting next to
the ``.wav`` with the same stem, which is how ``docs/examples`` is laid out.

Remote is only measured when ``ECHONOTE_ASR_REMOTE_KEY`` is set; without it the
script benchmarks the local model alone and says so, rather than failing.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import asr_remote, config


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


def load_audio(path: Path) -> np.ndarray:
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=config.SAMPLE_RATE)


def run_local(audio: np.ndarray, model_key: str) -> tuple[str, float]:
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(Path(config.MODEL_DIR) / f"faster-whisper-{model_key}"),
        device="cpu",
        compute_type="int8",
        local_files_only=True,
    )
    started = time.perf_counter()
    segments, _ = model.transcribe(audio, language="zh", beam_size=5)
    text = "".join(segment.text for segment in segments).strip()
    return text, time.perf_counter() - started


def run_remote(audio: np.ndarray) -> tuple[str, float]:
    started = time.perf_counter()
    text = asr_remote.transcribe(audio, "zh")
    elapsed = time.perf_counter() - started
    if text is None:
        raise RuntimeError("remote request failed (see the warning above)")
    return text, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="16 kHz WAV to transcribe")
    parser.add_argument("--reference", type=Path, help="ground-truth text")
    parser.add_argument("--local", default="base", help="local model key (default: base)")
    parser.add_argument(
        "--skip-local", action="store_true", help="only measure the remote backend"
    )
    args = parser.parse_args()

    reference_path = args.reference or args.audio.with_suffix(".txt")
    if not reference_path.exists():
        print(f"[warn] no reference at {reference_path}; CER will be omitted")
        reference = ""
    else:
        reference = reference_path.read_text(encoding="utf-8")

    audio = load_audio(args.audio)
    duration = len(audio) / config.SAMPLE_RATE
    print(f"audio: {args.audio.name}  {duration:.1f}s  reference: {len(reference)} chars\n")

    rows: list[tuple[str, str, float, float, str]] = []

    if not args.skip_local:
        text, elapsed = run_local(audio, args.local)
        rows.append(
            ("local", args.local, elapsed, elapsed / duration, text if reference else "")
        )

    if asr_remote.enabled():
        text, elapsed = run_remote(audio)
        rows.append(
            ("remote", config.ASR_REMOTE_MODEL, elapsed, elapsed / duration,
             text if reference else "")
        )
    else:
        print("[skip] ECHONOTE_ASR_REMOTE_KEY is not set; remote not measured\n")

    header = f"{'backend':<9}{'model':<26}{'sec':>8}{'rtf':>7}{'cer':>9}"
    print(header)
    print("-" * len(header))
    for backend, model, elapsed, rtf, text in rows:
        cer_text = f"{cer(text, reference) * 100:>8.2f}%" if reference else "       -"
        print(f"{backend:<9}{model:<26}{elapsed:>8.2f}{rtf:>7.2f}{cer_text}")

    if reference and len(rows) > 1:
        print("\ntranscripts:")
        for backend, _model, _elapsed, _rtf, text in rows:
            print(f"  [{backend}] {text[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
