"""把一段 wav 按**与运行时相同的分段参数**跑一遍定稿流水线，输出文本与质量指标。

为什么需要它：`tools/bench_cer.py` 是把整段音频一次性喂给识别器，长音频会
漂移（实测 154 秒连续音频，连 whisper-medium 都在重复“扫描的扫描”），拿它
比较引擎会得出完全错误的结论。真正决定观感的是「VAD 分段 + 逐段定稿」这条
链路，所以这里复刻这条链路：读 wav → 30 ms 帧喂 Segmenter → 每段送定稿模型
→ 拼回文本。

输出三组数字，用来判断一次改动到底有没有用：

* 字数/秒      —— 说得越快这个数越大，能侧面反映漏字
* 标点收尾率   —— 定稿真的跑完的标志（流式文本几乎没有标点）
* 碎段率       —— ≤6 字的段落占比，精修被丢时这个数会明显变大

用法::

    python tools/bench_pipeline.py D:/下载/edgedown_/echonote-20261028-0833.wav
    python tools/bench_pipeline.py clip.wav --engine whisper --out out.txt
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app import asr_sensevoice, config  # noqa: E402
from app.vad import Segmenter  # noqa: E402

FRAME = int(config.SAMPLE_RATE * 0.03)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != config.SAMPLE_RATE or handle.getnchannels() != 1:
            raise SystemExit(f"{path.name}: 需要 16 kHz 单声道，实际 {handle.getframerate()} Hz / {handle.getnchannels()} 声道")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def segments_of(audio: np.ndarray) -> list[tuple[float, float, np.ndarray]]:
    """用运行时的模式参数分段，返回 (start, end, audio)。"""
    segmenter = Segmenter(**config.mode_overrides())
    out: list[tuple[float, float, np.ndarray]] = []
    for offset in range(0, len(audio) - FRAME + 1, FRAME):
        for event in segmenter.push(audio[offset : offset + FRAME]):
            out.append((event["start"], event["end"], event["audio"]))
    tail = segmenter.flush()
    if tail:
        out.append((tail["start"], tail["end"], tail["audio"]))
    return out


def whisper_engine():
    """faster-whisper 作为定稿模型的候选（本地有 medium 权重时可用）。"""
    from faster_whisper import WhisperModel

    directory = Path(config.MODEL_DIR) / "faster-whisper-medium"
    if not directory.is_dir():
        raise SystemExit(f"找不到 {directory}")
    model = WhisperModel(str(directory), device="cpu", compute_type="int8")

    def transcribe(audio: np.ndarray) -> str:
        chunks, _ = model.transcribe(audio, language="zh", beam_size=5, vad_filter=False)
        return "".join(chunk.text for chunk in chunks).strip()

    return transcribe


def main() -> int:
    parser = argparse.ArgumentParser(description="按运行时链路跑一段 wav")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--engine", choices=["sensevoice", "whisper"], default="sensevoice")
    parser.add_argument("--out", type=Path, default=None, help="把逐段文本写到文件")
    parser.add_argument("--limit", type=float, default=0.0, help="只跑前 N 秒，0 = 全段")
    args = parser.parse_args()

    audio = read_wav(args.wav)
    if args.limit > 0:
        audio = audio[: int(args.limit * config.SAMPLE_RATE)]

    started = time.perf_counter()
    pieces = segments_of(audio)
    segment_s = time.perf_counter() - started

    if args.engine == "whisper":
        transcribe = whisper_engine()
    else:
        if not asr_sensevoice.load():
            raise SystemExit("SenseVoice 模型未安装（models/sherpa-sense-voice-small）")
        transcribe = asr_sensevoice.transcribe

    decode_started = time.perf_counter()
    lines: list[str] = []
    for start, end, chunk in pieces:
        text = (transcribe(chunk) or "").strip()
        if text:
            lines.append(f"[{int(start)//60:02d}:{int(start)%60:02d}] {text}")
    decode_s = time.perf_counter() - decode_started

    body = "\n".join(lines)
    if args.out:
        args.out.write_text(body + "\n", encoding="utf-8")

    texts = [line.split("] ", 1)[1] for line in lines if "] " in line]
    lengths = [len(text) for text in texts]
    punctuated = sum(1 for text in texts if text and text[-1] in "。，？！.,?!")
    tiny = sum(1 for length in lengths if length <= 6)
    duration = len(audio) / config.SAMPLE_RATE
    total_chars = sum(lengths)

    print(f"{args.wav.name}: 音频 {duration:.1f}s  分段 {len(pieces)} 段（{segment_s:.1f}s）  "
          f"定稿 {decode_s:.1f}s（RTF {decode_s/max(duration,1e-9):.2f}，引擎 {args.engine}）")
    if texts:
        print(f"  正文 {total_chars} 字  平均段长 {statistics.mean(lengths):.1f}  中位 {statistics.median(lengths):.0f}  "
              f"标点收尾 {punctuated}/{len(texts)} ({punctuated*100//len(texts)}%)  "
              f"碎段 {tiny}/{len(texts)} ({tiny*100//len(texts)}%)  字/秒 {total_chars/duration:.2f}")
        print("  首段:", texts[0][:60])
        print("  末段:", texts[-1][:60])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
