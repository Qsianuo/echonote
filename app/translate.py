"""离线翻译：Opus-MT 中英双向，ctranslate2 推理。

为什么单独一层：识别引擎（zipformer / SenseVoice）只做转写，不做翻译。
「选中文、说英文、出中文」本质是两步——先把语音转成英文原文，再把英文原文
翻成中文。前者已经有，后者就是这个模块。

模型是 Opus-MT 的 77M 小模型（Marian），ctranslate2 int8 推理，实测单句
50~100 ms，全程离线。

**一个必须记住的坑**：Marian 要求 source 末尾带 ``</s>``。漏掉它，模型不知道
句子在哪结束，会一直重复生成（实测刷出「real-real-real-real-re-re-V」整行）。
transformers 的 MarianTokenizer 是隐式加的，手写调用时最容易漏。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import config, models

log = logging.getLogger("echonote.translate")

# 方向 -> 模型 spec。目录名、仓库与文件清单只在 models.MT_CATALOG 里写一份：
# 下载器（fetch）和翻译器（这里）引用同一个对象，才不会出现「下到 A 目录、
# 去 B 目录找」这种只能靠用户手动摆放才对得上的错位。
# 要加语言：在 models.MT_CATALOG 加一条，这里跟着加一行。
_PAIRS: dict[tuple[str, str], models.ModelSpec] = dict(models.MT_PAIRS)

# 界面「译文语言」可选的代码，必须与 _PAIRS 的目标语言一致。
TARGETS: tuple[str, ...] = ("zh", "en")

# (src, tgt) -> (source_spm, target_spm, translator) | None（加载失败后记住）
_engines: dict[tuple[str, str], tuple | None] = {}
_load_lock = threading.Lock()
_decode_lock = threading.Lock()


def model_dir(source: str, target: str) -> Path | None:
    """Direction's model directory, or None when the pair has no model."""
    spec = _PAIRS.get((source, target))
    return None if spec is None else Path(config.MODEL_DIR) / spec.dirname


def is_installed(source: str, target: str) -> bool:
    """True when every file the translator needs is on disk.

    判据跟着 spec 走（含 ``source.spm`` / ``target.spm``）：只查权重会放过
    「下载到一半」的目录，那种情况一调用就崩，比明确缺失更难查。
    """
    spec = _PAIRS.get((source, target))
    if spec is None:
        return False
    directory = Path(config.MODEL_DIR) / spec.dirname
    return all((directory / name).is_file() for name in spec.required_files)


def available() -> bool:
    """True when at least one direction can run (the UI hides the control otherwise)."""
    return config.MT_ENABLED and any(is_installed(s, t) for s, t in _PAIRS)


def targets() -> list[str]:
    """Translation targets the UI may offer, given what is actually installed."""
    if not config.MT_ENABLED:
        return []
    return [t for t in TARGETS if any(is_installed(s, t) for s in ("zh", "en"))]


def detect_language(text: str) -> str:
    """Coarse zh/en guess from the text itself. "" when there is nothing to judge.

    Weighted rather than a plain count: one Han character carries roughly the
    information of two or three Latin letters, so a mixed sentence like
    「这个 API 的 timeout 是多少」 has to still land on zh.
    """
    han = 0
    latin = 0
    for ch in text or "":
        if "\u4e00" <= ch <= "\u9fff":
            han += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    if not han and not latin:
        return ""
    return "zh" if han * 2 >= latin else "en"


def _load(source: str, target: str) -> tuple | None:
    """Build (or fetch) the direction's engine. None when it cannot be built."""
    key = (source, target)
    with _load_lock:
        if key in _engines:
            return _engines[key]
        directory = model_dir(source, target)
        entry: tuple | None = None
        if directory is None or not is_installed(source, target):
            log.info("No translation model for %s->%s", source, target)
        else:
            try:
                import ctranslate2
                import sentencepiece as spm

                src_spm = spm.SentencePieceProcessor()
                src_spm.Load(str(directory / "source.spm"))
                tgt_spm = spm.SentencePieceProcessor()
                tgt_spm.Load(str(directory / "target.spm"))
                translator = ctranslate2.Translator(
                    str(directory), device="cpu", compute_type="int8"
                )
                entry = (src_spm, tgt_spm, translator)
                log.info("Translation engine ready (%s->%s)", source, target)
            except Exception:  # noqa: BLE001 - a missing model must not break dictation
                log.exception("Failed to build the translation engine %s->%s", source, target)
                entry = None
        _engines[key] = entry
        return entry


def translate(text: str, target: str, source: str | None = None) -> str | None:
    """Translate ``text`` into ``target``. None means "leave the original alone".

    None covers every reason not to translate: empty input, no model for the
    pair, source already equals target, or the decode blew up. Callers keep the
    transcription either way, so translation can never lose a sentence.
    """
    text = (text or "").strip()
    if not text or not target:
        return None
    src = source or detect_language(text)
    if not src or src == target:
        return None
    entry = _load(src, target)
    if entry is None:
        return None

    src_spm, tgt_spm, translator = entry
    tokens = src_spm.EncodeAsPieces(text) + ["</s>"]
    try:
        with _decode_lock:
            result = translator.translate_batch(
                [tokens],
                beam_size=config.MT_BEAM_SIZE,
                no_repeat_ngram_size=3,
                max_decoding_length=config.MT_MAX_DECODING_TOKENS,
            )
    except Exception:  # noqa: BLE001
        log.exception("Translation failed (%s->%s)", src, target)
        return None
    if not result:
        return None

    hypothesis = list(result[0].hypotheses[0])
    if hypothesis and hypothesis[-1] == "</s>":
        hypothesis.pop()
    return tgt_spm.DecodePieces(hypothesis).strip() or None
