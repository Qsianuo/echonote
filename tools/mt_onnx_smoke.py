"""在桌面侧把 ONNX 翻译跑通，再去写 Kotlin。

手机上的实现没法反复试错（构建一次 APK 几分钟，还得装模拟器），所以先在这
里用**与 Kotlin 完全相同的算法**（同款 NFKC 归一化、同款 unigram Viterbi、
同款贪心解码、同款 ▁ 还原）验证三件事：

1. 量化后的模型还能翻出通顺的英文；
2. 词表映射（SP id → Marian 合并 id）没错位；
3. 与桌面 ctranslate2 版本的译文差多少。

任何一条不成立，Kotlin 写得再对也没用。

用法::

    python tools/mt_onnx_smoke.py model-dist/mt-onnx/mt-zh-en --text "把焦距调到五十毫米"
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

import numpy as np
import onnxruntime as ort

MARK = "\u2581"
MAX_NEW_TOKENS = 128


def load_source_vocab(path: Path) -> tuple[dict[str, tuple[int, float]], dict[str, int]]:
    table: dict[str, tuple[int, float]] = {}
    specials = {"unk": 1, "eos": 0, "pad": 65000}
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.startswith("#"):
                for token in line.split():
                    name, _, value = token.partition("=")
                    if name in specials and value.isdigit():
                        specials[name] = int(value)
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            table[parts[0]] = (int(parts[1]), float(parts[2]))
    return table, specials


def load_target_vocab(path: Path) -> tuple[dict[int, str], int]:
    table: dict[int, str] = {}
    eos_id = 0
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.startswith("#"):
                for token in line.split():
                    if token.startswith("eos="):
                        eos_id = int(token[4:])
                continue
            head, _, piece = line.partition("\t")
            if not piece:
                continue
            table[int(head)] = piece
    return table, eos_id


def normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("\u00a0", " ").strip()
    if not normalized:
        return ""
    return MARK + "".join(MARK if ch in " \t\n" else ch for ch in normalized)


def encode(text: str, table: dict[str, tuple[int, float]], unk_id: int) -> list[int]:
    s = normalize(text)
    if not s:
        return []
    max_len = max(len(piece) for piece in table)
    min_score = min(score for _, score in table.values())
    n = len(s)
    neg = -1.0e30
    best = [neg] * (n + 1)
    prev = [-1] * (n + 1)
    step = [0] * (n + 1)
    best[0] = 0.0
    for i in range(n):
        if best[i] == neg:
            continue
        single = False
        for length in range(1, min(max_len, n - i) + 1):
            piece = s[i : i + length]
            hit = table.get(piece)
            if hit is None:
                continue
            if length == 1:
                single = True
            score = best[i] + hit[1]
            if score > best[i + length]:
                best[i + length] = score
                prev[i + length] = i
                step[i + length] = hit[0]
        if not single:
            score = best[i] + min_score - 10.0
            if score > best[i + 1]:
                best[i + 1] = score
                prev[i + 1] = i
                step[i + 1] = unk_id
    if prev[n] < 0:
        return []
    out: list[int] = []
    cursor = n
    while cursor > 0:
        out.append(step[cursor])
        cursor = prev[cursor]
    out.reverse()
    return out


def decode(ids: list[int], table: dict[int, str], eos_id: int) -> str:
    pieces: list[str] = []
    for token in ids:
        if token == eos_id:
            break
        piece = table.get(token)
        if not piece:
            continue
        pieces.append(piece)
    text = "".join(pieces).replace(MARK, " ").strip()
    # 与 Kotlin 的 tidy 同规则：收掉标点前的空格。
    for punct in [",", ".", "!", "?", ";", ":", "%)"]:
        text = text.replace(f" {punct}", punct)
    text = text.replace("( ", "(")
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


class Translator:
    def __init__(self, directory: Path, variant: str = "int8") -> None:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        if variant == "fp32":
            names = ("encoder_model.onnx", "decoder_model.onnx", "decoder_with_past_model.onnx")
        else:
            names = ("encoder-int8.onnx", "decoder-int8.onnx", "decoder-past-int8.onnx")
        self.encoder = ort.InferenceSession(str(directory / names[0]), options)
        self.decoder = ort.InferenceSession(str(directory / names[1]), options)
        self.decoder_past = ort.InferenceSession(str(directory / names[2]), options)
        self.source, self.specials = load_source_vocab(directory / "mt-src-vocab.txt")
        self.target, self.eos_id = load_target_vocab(directory / "mt-tgt-vocab.txt")
        self.unk_id = self.specials["unk"]
        # HF 的 Marian 用 pad_id 作 decoder 起始符（不是 </s>）。传错起始符时
        # 解码器第一步就吐 </s>，表现是「一句话翻出一个空串」——踩过。
        self.start_id = self.specials["pad"]

    def translate(self, text: str) -> tuple[str, int]:
        ids = encode(text, self.source, self.unk_id)
        if not ids:
            return "", 0
        source_ids = np.array([[int(i) for i in ids] + [self.eos_id]], dtype=np.int64)
        mask = np.ones_like(source_ids)
        hidden = self.encoder.run(None, {"input_ids": source_ids, "attention_mask": mask})[0]

        produced: list[int] = []
        token = self.start_id
        # cross-attention 的 K/V 只算一次：首步输出后一直复用，增量步不再吐
        # （decoder-past 只有 13 个输出：logits + 6 层 decoder 的 key/value）。
        encoder_past: dict[str, np.ndarray] = {}
        decoder_past: dict[str, np.ndarray] = {}
        steps = 0
        for _ in range(MAX_NEW_TOKENS):
            session = self.decoder if not decoder_past else self.decoder_past
            # 按会话自己声明的输入名填——首步要 encoder_hidden_states，
            # 增量步不要（cross-attention 的 K/V 已在 past 里），多塞一个就报错。
            available = {spec.name for spec in session.get_inputs()}
            feed: dict[str, np.ndarray] = {}
            if "input_ids" in available:
                feed["input_ids"] = np.array([[token]], dtype=np.int64)
            if "encoder_attention_mask" in available:
                feed["encoder_attention_mask"] = mask
            if "encoder_hidden_states" in available:
                feed["encoder_hidden_states"] = hidden
            for name in available:
                if not name.startswith("past_key_values"):
                    continue
                key = name.replace("past_key_values", "present")
                feed[name] = encoder_past[key] if ".encoder." in key else decoder_past[key]
            outputs = session.run(None, feed)
            names = [spec.name for spec in session.get_outputs()]
            values = dict(zip(names, outputs))
            logits = values["logits"]
            token = int(np.argmax(logits[0, -1]))
            for name, value in values.items():
                if not name.startswith("present"):
                    continue
                if ".encoder." in name:
                    encoder_past[name] = value
                else:
                    decoder_past[name] = value
            steps += 1
            if token == self.eos_id:
                break
            produced.append(token)
        return decode(produced, self.target, self.eos_id), steps


def main() -> int:
    parser = argparse.ArgumentParser(description="ONNX 翻译冒烟测试")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--text", action="append", default=None)
    parser.add_argument("--variant", choices=["int8", "fp32"], default="int8")
    args = parser.parse_args()

    samples = args.text or [
        "把焦距调到五十毫米，然后做曝光测试。",
        "环形光源的亮度不够，需要重新打光。",
        "这个模块的返回值是负一，说明通讯超时了。",
        "模板匹配的阈值不要设得太高。",
    ]
    translator = Translator(args.directory, args.variant)
    for sample in samples:
        translated, steps = translator.translate(sample)
        print(f"源: {sample}")
        print(f"译: {translated}   ({steps} 步)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
