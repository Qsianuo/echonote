"""安卓端离线翻译（ONNX 路线）的护栏。

桌面端走 ctranslate2，安卓端走 ONNX Runtime，是两套代码同一个模型。这条链路
上有三处「跑得通但结果是错的」，编译器与类型系统都拦不住，只能靠测试盯着：

1. **量化参数**——per_channel=True 时模型正常加载、正常推理、不报任何错，
   但首步 logits 被权重舍入误差打偏，输出退化成 "the the the"。不写测试的
   话，下次谁「优化」一下参数就会静默坏掉。
2. **decoder 起始符**——HF 的 Marian 用 pad(65000) 而不是 </s> 起头。传错时
   第一步直接吐 eos，表现是「每句都翻出空串」，同样不报错。
3. **文件名**——三份 ONNX 的名字在导出脚本、量化脚本、Kotlin 插件三处出现，
   改一处忘一处就是运行时「模型未安装」。

这里只做静态核对，不加载 226 MB 的模型（CI 上也没有）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

KOTLIN = ROOT / "mobile" / "android" / "app" / "src" / "main" / "java" / "com" / "echonote" / "app"
MT_PLUGIN = KOTLIN / "SherpaMTPlugin.kt"
SP_MODEL = KOTLIN / "SentencePieceModel.kt"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestQuantizeSettings:
    """量化参数是硬编码在工具里的经验值，必须锁住。"""

    def test_per_channel_is_off(self) -> None:
        pytest.importorskip("onnxruntime")
        import quantize_mt_onnx

        assert quantize_mt_onnx.QUANT_KWARGS["per_channel"] is False

    def test_gather_is_quantized(self) -> None:
        pytest.importorskip("onnxruntime")
        import quantize_mt_onnx

        # 词嵌入（65001×512）走 Gather。漏了它，体积只降一半再一半。
        assert "Gather" in quantize_mt_onnx.QUANT_KWARGS["op_types_to_quantize"]
        assert "MatMul" in quantize_mt_onnx.QUANT_KWARGS["op_types_to_quantize"]

    def test_reduce_range_off(self) -> None:
        pytest.importorskip("onnxruntime")
        import quantize_mt_onnx

        # reduce_range=True 只对部分 x86 量化内核友好，会让有效位宽掉到 7 bit，
        # ARM 上纯亏。
        assert quantize_mt_onnx.QUANT_KWARGS["reduce_range"] is False

    def test_merged_decoder_is_not_a_target(self) -> None:
        # decoder_model_merged.onnx 是 If 控制流，量化器进不去子图，
        # 实测 352 MB → 352 MB 一点没压。它若进了待量化清单就是白忙一场。
        source = _read(TOOLS / "quantize_mt_onnx.py")
        targets = source.split("targets = {")[1].split("}")[0]
        assert "decoder_model_merged.onnx" not in targets
        assert "decoder_model.onnx" in targets
        assert "decoder_with_past_model.onnx" in targets


class TestKotlinContract:
    """Kotlin 侧的常量必须与导出产物对齐。"""

    def test_plugin_uses_int8_filenames(self) -> None:
        source = _read(MT_PLUGIN)
        for name in ("encoder-int8.onnx", "decoder-int8.onnx", "decoder-past-int8.onnx"):
            assert name in source, f"插件里找不到 {name}"
        # fp32 单方向 580 MB，手机上不成立；常量里出现它说明有人绕过了量化。
        for name in ('"encoder_model.onnx"', '"decoder_with_past_model.onnx"'):
            assert name not in source, f"插件仍引用未量化的 {name}"

    def test_start_token_is_pad_not_eos(self) -> None:
        source = _read(MT_PLUGIN)
        assert 'special("pad", 65000)' in source
        # 曾经写成 startId = eosId，症状是「每句翻出空串」。
        assert "startId = eosId" not in source

    def test_direction_map_covers_both_ways(self) -> None:
        source = _read(MT_PLUGIN)
        assert '"en" to "zh-en"' in source
        assert '"zh" to "en-zh"' in source

    def test_plugin_is_registered(self) -> None:
        activity = _read(KOTLIN / "MainActivity.java")
        # 注册漏了会构建成功、运行时「插件不可用」——踩过一次。
        assert "registerPlugin(SherpaMTPlugin.class)" in activity

    def test_tokenizer_reads_pad_from_header(self) -> None:
        source = _read(SP_MODEL)
        assert "pad=" in source
        assert "Normalizer.Form.NFKC" in source


class TestVocabHeader:
    """encode / decode 两侧都靠词表首行的特殊 id，格式变了要立刻发现。"""

    def test_header_reports_eos_unk_pad(self) -> None:
        source = _read(TOOLS / "export_mt_vocab.py")
        assert re.search(r"eos=\{[^}]*\} unk=\{[^}]*\} pad=\{[^}]*\}", source), (
            "词表首行的 eos/unk/pad 三元组不见了，Kotlin 取不到起始符"
        )


class TestGlossaryDataFresh:
    """术语纠错数据是生成的，源文件改了忘了重跑就是静默失效。"""

    def test_generated_data_matches_glossary(self) -> None:
        pytest.importorskip("pypinyin")
        import gen_glossary_data

        out = ROOT / "web" / "glossary-data.js"
        assert out.is_file(), "web/glossary-data.js 不存在，请跑 tools/gen_glossary_data.py"
        assert gen_glossary_data.generate() == out.read_text(encoding="utf-8"), (
            "web/glossary-data.js 与 models/glossary.txt 不同步，请重跑 tools/gen_glossary_data.py"
        )
