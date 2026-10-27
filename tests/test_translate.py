"""翻译层与协议字段的单元测试。

CI 上没有模型权重（``models/`` 被 gitignore），所以凡是要真跑权重的用例都
skipif 掉；语种判定、降级路径、协议字段这些不碰权重的必须一直跑。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config, protocol, translate

# conftest 把 ECHONOTE_MODEL_DIR 指到一个不存在的目录，保证 CI 不依赖权重。
# 真实翻译用例因此必须自己把目录指回仓库的 models/，并按文件是否存在决定跑不跑。
REAL_MODELS = Path(__file__).resolve().parent.parent / "models"
MODELS_READY = (REAL_MODELS / "mt-zh-en" / "model.bin").is_file() and (
    REAL_MODELS / "mt-en-zh" / "model.bin"
).is_file()


@pytest.fixture
def real_models(monkeypatch):
    monkeypatch.setattr(config, "MODEL_DIR", str(REAL_MODELS))
    translate._engines.clear()
    yield
    translate._engines.clear()


class TestDetectLanguage:
    def test_pure_chinese(self) -> None:
        assert translate.detect_language("这个镜头的焦距是二十五毫米") == "zh"

    def test_pure_english(self) -> None:
        text = "The focal length is twenty five millimeters"
        assert translate.detect_language(text) == "en"

    def test_mixed_sentence_stays_chinese(self) -> None:
        # 一个汉字约抵 2~3 个拉丁字母的信息量，所以夹几个英文词的中文句子
        # 仍然应该判成中文，否则「译文语言=中文」时会把中文再翻一遍。
        assert translate.detect_language("这个 API 的 timeout 是多少") == "zh"

    def test_nothing_to_judge(self) -> None:
        assert translate.detect_language("，。！？") == ""
        assert translate.detect_language("") == ""
        assert translate.detect_language("123 456") == ""


class TestTranslateGuards:
    def test_same_language_is_not_translated(self) -> None:
        # 选中文、说的是中文：不该产生译文。
        assert translate.translate("这是中文", "zh") is None

    def test_empty_input(self) -> None:
        assert translate.translate("", "en") is None
        assert translate.translate("   ", "en") is None

    def test_missing_model_degrades_to_none(self, monkeypatch, tmp_path) -> None:
        # 权重不在时只能不翻，绝不能抛错——翻译失败不能连带丢字幕。
        monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path))
        translate._engines.clear()
        try:
            assert translate.translate("这是中文", "en") is None
            assert translate.is_installed("zh", "en") is False
            assert translate.targets() == []
            assert translate.available() is False
        finally:
            translate._engines.clear()

    def test_targets_empty_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "MT_ENABLED", False)
        assert translate.targets() == []


class TestProtocolTranslation:
    def test_final_omits_empty_translation(self) -> None:
        message = protocol.final(final_id="1-1", epoch=1, text="你好", start=0, end=1)
        assert "translation" not in message

    def test_final_carries_translation(self) -> None:
        message = protocol.final(
            final_id="1-1", epoch=1, text="你好", start=0, end=1, translation="Hello"
        )
        assert message["translation"] == "Hello"
        assert message["type"] == "final"

    def test_refine_carries_translation(self) -> None:
        message = protocol.refine(
            final_id="1-1", epoch=1, text="你好", start=0, end=1, translation="Hello"
        )
        assert message["translation"] == "Hello"
        assert message["type"] == "refine"


@pytest.mark.skipif(not MODELS_READY, reason="需要 models/mt-* 权重（不入库）")
class TestRealTranslation:
    def test_zh_to_en(self, real_models) -> None:
        out = translate.translate("这个镜头的焦距是二十五毫米。", "en")
        assert out, "中文到英文应产出译文"
        assert "25" in out, f"数字应保留：{out}"
        assert "focal" in out.lower(), f"焦距应翻成 focal length：{out}"

    def test_en_to_zh(self, real_models) -> None:
        out = translate.translate(
            "The focal length of this lens is twenty five millimeters.", "zh"
        )
        assert out, "英文到中文应产出译文"
        assert "焦距" in out, f"focal length 应翻成焦距：{out}"

    def test_long_sentence_has_no_repeat_loop(self, real_models) -> None:
        # Marian 漏掉 source 末尾的 </s> 时会陷入重复（real-real-real-real…）。
        # 判据必须是「连续同一个词重复 3 次以上」：普通句子本来就含重复词，
        # 拿「词数 == 去重词数」当条件会把正常译文判成 bug。
        text = "今天天气很好，我们开始录音，请把测试数据保存到本地目录。"
        out = translate.translate(text, "en")
        assert out
        words = re.findall(r"[a-z']+", out.lower())
        run = 1
        for prev, cur in zip(words, words[1:]):
            run = run + 1 if cur == prev else 1
            assert run < 3, f"疑似重复循环：{out}"
        assert len(words) <= len(text) * 2, f"译文异常冗长：{out}"
