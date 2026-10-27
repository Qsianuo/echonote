"""把 opus-mt 的 ONNX 压到手机上能接受的大小（int8 动态量化）。

为什么必须压：fp32 的 encoder 210 MB + decoder 369 MB，一个方向就 580 MB，
两个方向超过 1 GB——放进 APK 不可能，让用户下载也不现实。

为什么连 Gather 一起量：这份模型 77 M 参数里有 33 M 是 65001×512 的词嵌入，
它们走 Gather 而不是 MatMul。只量化 MatMul 的话嵌入仍是 fp32（133 MB），
量化收益砍半。

用法::

    python tools/quantize_mt_onnx.py build/mt-onnx/mt-zh-en
"""

from __future__ import annotations

import argparse
from pathlib import Path

from onnxruntime.quantization import QuantType, quantize_dynamic


# 量化参数单独拎出来，是为了让测试能盯着它们（见 tests/test_mt_onnx.py）。
#
# · Gather 必须一起量：77 M 参数里 33 M 是 65001×512 的词嵌入，走 Gather 不走
#   MatMul，只量 MatMul 的话嵌入仍是 fp32，收益直接砍半。
# · per_channel 必须 False：实测 per_channel=True 时模型能跑、不报错，但首步
#   logits 就被打偏，英文输出退化成 "the the the" 这类碎片；同一份权重改成
#   per-tensor 后与 fp32 语义一致（措辞不同，但都对得上原文）。
# · reduce_range 保持 False：QInt8 全量程，ARM 与 x86 上行为一致。
QUANT_KWARGS = {
    "weight_type": QuantType.QInt8,
    "op_types_to_quantize": ["MatMul", "Gather"],
    "per_channel": False,
    "reduce_range": False,
}


def quantize(src: Path, dst: Path) -> None:
    quantize_dynamic(model_input=str(src), model_output=str(dst), **QUANT_KWARGS)


def main() -> int:
    parser = argparse.ArgumentParser(description="int8 量化 opus-mt ONNX")
    parser.add_argument("dir", type=Path, help="optimum 导出目录")
    args = parser.parse_args()

    # decoder_model_merged.onnx 看着更省（一份顶两份），但它是 If 控制流：
    # use_cache_branch 选分支，量化器进不去子图，实测 352 MB → 352 MB 一点没压。
    # 所以退回「首步 + 增量」两份普通图，各自能压到 25%。
    targets = {
        "encoder_model.onnx": "encoder-int8.onnx",
        "decoder_model.onnx": "decoder-int8.onnx",
        "decoder_with_past_model.onnx": "decoder-past-int8.onnx",
    }
    for name, out in targets.items():
        src = args.dir / name
        if not src.is_file():
            print(f"跳过（不存在）：{src.name}")
            continue
        dst = args.dir / out
        quantize(src, dst)
        before = src.stat().st_size / 1048576
        after = dst.stat().st_size / 1048576
        print(f"{src.name} {before:.0f} MB → {out} {after:.0f} MB（{after / before:.0%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
