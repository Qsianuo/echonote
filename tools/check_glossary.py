"""术语纠错实战校验：拿真实课堂录音跑一遍，看哪些专业词被纠对了。

用法：
    python tools/check_glossary.py <音频.wav> [--glossary models/glossary.txt]

做三件事：
1. 用定稿引擎（SenseVoice）把音频转一遍；
2. 过一遍术语纠错，打印「纠正前 → 纠正后」的每一处改动；
3. 报告术语表里哪些词真的出现了、哪些一次都没出现（后者多半是词表太宽）。

注意：这里用的是**整段离线解码**，而应用里是 VAD 切段后逐段解码。结论对
「术语能纠对吗」有效，对「切段时机好不好」无效。
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import asr_sensevoice, config, glossary  # noqa: E402


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        if w.getnchannels() != 1:
            raise SystemExit(f"只支持单声道，当前 {w.getnchannels()} 声道")
        if w.getsampwidth() != 2:
            raise SystemExit(f"只支持 16 位 PCM，当前 {w.getsampwidth() * 8} 位")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, rate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--glossary", default=None)
    args = ap.parse_args()

    terms = config.GLOSSARY_TERMS
    if args.glossary:
        path = Path(args.glossary)
        terms = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not terms:
        print("术语表为空：检查 models/glossary.txt 是否存在")
        return 1

    audio, rate = read_wav(args.wav)
    if rate != config.SAMPLE_RATE:
        print(f"警告：采样率 {rate} 与引擎 {config.SAMPLE_RATE} 不一致，结果可能失真")

    print(f"音频 {len(audio) / rate:.1f}s · 术语 {len(terms)} 个\n")
    raw_text = asr_sensevoice.transcribe(audio)
    if not raw_text:
        print("识别结果为空——可能是全段静音或远场太弱，先确认这段里有人说话")
        return 2

    fixed = glossary.correct(raw_text, terms)
    print("── 识别原文 ──")
    print(raw_text)
    print("\n── 纠错后 ──")
    print(fixed)

    if fixed != raw_text:
        print("\n── 改动了哪里 ──")
        hits = [t for t in terms if t in fixed and t not in raw_text]
        for term in dict.fromkeys(hits):
            print(f"  {term}")
    else:
        print("\n没有发生纠正。可能原因：这段里没出现专业词，或错法与术语读音不同。")

    missing = [t for t in dict.fromkeys(terms) if t not in fixed and t not in raw_text]
    print(f"\n── 术语覆盖 ──")
    print(f"  出现并正确：{len([t for t in dict.fromkeys(terms) if t in fixed])}")
    print(f"  一次未出现：{len(missing)}（含 {', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
