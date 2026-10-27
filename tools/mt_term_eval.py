"""术语命中率评测：77M 小模型到底错在哪、以及是不是能修。

背景：端到端跑下来，`ring light` 会被翻成「戒指光」。这不是量化的锅
（fp32 与 int8 一字不差），也不是词表错位，而是 77M Marian 的短语表
本身没收录这个搭配——它按词素直译：ring→戒指、light→光。

这个脚本把「到底多差」量化出来：固定一组带术语的英文句子，检查译文
里有没有出现期望的中文术语。同一份句子可以分别用

* ``--backend onnx``  —— 与安卓端完全相同的**贪心解码**；
* ``--backend ct2``   —— 桌面端的 ctranslate2，**beam search + no_repeat_ngram**。

两者的差就是「解码策略」能拿回多少分：如果 beam 能把「戒指光」修成
「环形灯」，那说明瓶颈在解码而不在模型，安卓端把贪心换成小 beam 就有
确定收益；如果两者都错，才是模型能力上限，只能换模型。

用法::

    python tools/mt_term_eval.py model-dist/mt-onnx/mt-en-zh --backend onnx
    python tools/mt_term_eval.py models/mt-en-zh --backend ct2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# (英文原句, 可接受的中文译法)
#
# 这里刻意区分两类失败，因为对策完全不同：
#   * 「同义不同词」（line scan camera → 线扫描相机 vs 线阵相机）——行业里两种
#     都有人用，不算错，不该为了对齐一张词表去改模型；
#   * 「真的翻错」（ring light → 戒指灯、exposure time → 接触时间）——按词素
#     直译出来的假朋友，必须修。
# 每组给出全部可接受写法，命中任一即算过。
CASES: list[tuple[str, tuple[str, ...]]] = [
    ("The ring light is too dim, we need to light it again.", ("环形灯",)),
    ("Set the focal length to fifty millimeters.", ("焦距",)),
    ("The depth of field of this lens is too shallow.", ("景深",)),
    ("We use a telecentric lens for measurement.", ("远心镜头",)),
    ("The exposure time should not exceed ten milliseconds.", ("曝光时间",)),
    ("The line scan camera is triggered by an encoder.", ("线阵相机", "线扫描相机", "线扫描摄像机")),
    ("Keep the working distance between eighty and one hundred millimeters.", ("工作距离",)),
    ("The threshold for template matching should not be too high.", ("模板匹配",)),
    ("Put the calibration board under the camera.", ("标定板", "校准板")),
    ("The color temperature of the light source is 6500K.", ("色温",)),
    ("Check the image sensor for dust before assembly.", ("图像传感器", "图像感应器")),
    ("This defect detection model was trained on ten thousand images.", ("缺陷检测",)),
]


def run_onnx(directory: Path, text: str) -> str:
    from mt_onnx_smoke import Translator

    translator = Translator(directory, "int8")
    return translator.translate(text)[0]


def run_ct2(directory: Path, text: str) -> str:
    import ctranslate2
    import sentencepiece as spm

    src_spm = spm.SentencePieceProcessor()
    src_spm.Load(str(directory / "source.spm"))
    tgt_spm = spm.SentencePieceProcessor()
    tgt_spm.Load(str(directory / "target.spm"))
    translator = ctranslate2.Translator(str(directory), device="cpu", compute_type="int8")
    tokens = src_spm.EncodeAsPieces(text) + ["</s>"]
    result = translator.translate_batch(
        [tokens], beam_size=4, no_repeat_ngram_size=3, max_decoding_length=128
    )
    hypothesis = list(result[0].hypotheses[0])
    if hypothesis and hypothesis[-1] == "</s>":
        hypothesis.pop()
    return tgt_spm.DecodePieces(hypothesis).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="翻译术语命中率评测")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--backend", choices=["onnx", "ct2"], default="onnx")
    args = parser.parse_args()

    if args.backend == "onnx":
        sys.path.insert(0, str(Path(__file__).parent))
        run = lambda text: run_onnx(args.directory, text)  # noqa: E731
    else:
        run = lambda text: run_ct2(args.directory, text)  # noqa: E731

    hits = 0
    for english, options in CASES:
        translated = run(english)
        hit = next((opt for opt in options if opt in translated), None)
        hits += bool(hit)
        label = "OK  " if hit else "MISS"
        note = "" if not hit else ("" if hit == options[0] else f" (同义: {hit})")
        print(f"[{label}] 期望术语: {' / '.join(options)}{note}")
        print(f"       源: {english}")
        print(f"       译: {translated}")
    total = len(CASES)
    print(f"\n术语命中 {hits}/{total} = {hits * 100 // total}%  (backend={args.backend})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
