"""翻译层与协议字段的单元测试。

CI 上没有模型权重（``models/`` 被 gitignore），所以凡是要真跑权重的用例都
skipif 掉；语种判定、降级路径、协议字段这些不碰权重的必须一直跑。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import config, models, protocol, translate

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


class TestFetchCatalog:
    """下载路径的守护。

    守的是「用户跑一次 fetch-model 到底能不能拿到翻译模型」。坏掉的方式很
    隐蔽：文件下载「成功」但缺 ``source.spm``——文件在、界面控件也在，一点
    就崩。所以清单校验必须跟着 spec 走，不能一律按 whisper 的两个文件判断。
    """

    def test_translation_specs_require_tokenizer(self) -> None:
        assert models.MT_CATALOG, "翻译档不能为空，否则 fetch 无从下载"
        for spec in models.MT_CATALOG:
            assert "model.bin" in spec.required_files
            assert "source.spm" in spec.required_files, f"{spec.key} 缺分词器清单"
            assert "target.spm" in spec.required_files, f"{spec.key} 缺分词器清单"

    def test_whisper_specs_keep_their_own_file_list(self) -> None:
        # 给 ModelSpec 加 required 字段后，whisper 档不能跟着被改成翻译的清单。
        for spec in models.CATALOG:
            assert spec.required_files == models.REQUIRED_FILES

    def test_spec_by_key_covers_both_catalogs(self) -> None:
        assert models.spec_by_key("medium") is models.BY_KEY["medium"]
        assert models.spec_by_key("mt-zh-en") is models.MT_BY_KEY["mt-zh-en"]
        assert models.spec_by_key("mt-en-zh") is models.MT_BY_KEY["mt-en-zh"]
        assert models.spec_by_key("not-a-model") is None

    def test_directory_without_tokenizer_is_rejected(self, tmp_path: Path) -> None:
        # 只有权重 + config 的目录（正是 allow_patterns 漏掉 "*.spm" 时留下的
        # 状态）必须判为不完整，否则下载器会报「完成」，翻译却在运行时崩。
        spec = models.MT_BY_KEY["mt-zh-en"]
        directory = tmp_path / spec.dirname
        directory.mkdir()
        (directory / "model.bin").write_bytes(b"0" * (2 << 20))
        (directory / "config.json").write_text("{}", encoding="utf-8")
        assert not models.is_spec_complete(spec, tmp_path)

        (directory / "source.spm").write_text("x", encoding="utf-8")
        (directory / "target.spm").write_text("x", encoding="utf-8")
        assert models.is_spec_complete(spec, tmp_path)

    def test_pairs_match_ui_targets(self) -> None:
        assert set(models.MT_PAIRS) == {("zh", "en"), ("en", "zh")}
        # 下拉里能选的语言，必须都能翻——否则用户选了却什么都不出。
        assert set(translate.TARGETS) == {target for _, target in models.MT_PAIRS}


class TestFetchCatalog:
    """下载路径的守护。

    守的是「用户跑一次 fetch-model 到底能不能拿到翻译模型」。坏掉的方式很
    隐蔽：文件下载「成功」但缺 ``source.spm``——文件在、界面控件也在，一点
    就崩。所以清单校验必须跟着 spec 走，不能一律按 whisper 的两个文件判断。
    """

    def test_translation_specs_require_tokenizer(self) -> None:
        assert models.MT_CATALOG, "翻译档不能为空，否则 fetch 无从下载"
        for spec in models.MT_CATALOG:
            assert "model.bin" in spec.required_files
            assert "source.spm" in spec.required_files, f"{spec.key} 缺分词器清单"
            assert "target.spm" in spec.required_files, f"{spec.key} 缺分词器清单"

    def test_whisper_specs_keep_their_own_file_list(self) -> None:
        # 给 ModelSpec 加 required 字段后，whisper 档不能跟着被改成翻译的清单。
        for spec in models.CATALOG:
            assert spec.required_files == models.REQUIRED_FILES

    def test_spec_by_key_covers_both_catalogs(self) -> None:
        assert models.spec_by_key("medium") is models.BY_KEY["medium"]
        assert models.spec_by_key("mt-zh-en") is models.MT_BY_KEY["mt-zh-en"]
        assert models.spec_by_key("mt-en-zh") is models.MT_BY_KEY["mt-en-zh"]
        assert models.spec_by_key("not-a-model") is None

    def test_directory_without_tokenizer_is_rejected(self, tmp_path: Path) -> None:
        # 只有权重 + config 的目录（正是 allow_patterns 漏掉 "*.spm" 时留下的
        # 状态）必须判为不完整，否则下载器会报「完成」，翻译却在运行时崩。
        spec = models.MT_BY_KEY["mt-zh-en"]
        directory = tmp_path / spec.dirname
        directory.mkdir()
        (directory / "model.bin").write_bytes(b"0" * (2 << 20))
        (directory / "config.json").write_text("{}", encoding="utf-8")
        assert not models.is_spec_complete(spec, tmp_path)

        (directory / "source.spm").write_text("x", encoding="utf-8")
        (directory / "target.spm").write_text("x", encoding="utf-8")
        assert models.is_spec_complete(spec, tmp_path)

    def test_pairs_match_ui_targets(self) -> None:
        assert set(models.MT_PAIRS) == {("zh", "en"), ("en", "zh")}
        # 下拉里能选的语言，必须都能翻——否则用户选了却什么都不出。
        assert set(translate.TARGETS) == {target for _, target in models.MT_PAIRS}
