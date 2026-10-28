"""非同音错词表：把识别结果里的错串整串替换成正词。

为什么单独一层：`glossary.py` 的判据是**读音相同**，能救「脚距 → 焦距」，
但救不了「魔方匹配 → 模板匹配」——两者的读音完全不同，是模型整词听错。

判据只有一条：**只替换表里写明的串**。不做模糊匹配、不做编辑距离，因为这一
层没有词边界可依，一旦放宽就一定误伤。表在 ``models/term-fixes.txt``，
改完重启即生效（与 glossary.txt 同一个约定），不进代码。

长串优先：表里若同时有「闭预算」和「预算」，必须先用长的，否则短串会先把
一半吃掉，留下「闭运算算」这种残句。
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import config

log = logging.getLogger("echonote.term_fixes")

# (错串, 正词)，长串在前
_RULES: list[tuple[str, str]] | None = None
_RULES_STAMP: tuple[float, int] | None = None


def path() -> Path:
    return Path(config.MODEL_DIR) / "term-fixes.txt"


def parse(text: str) -> list[tuple[str, str]]:
    """解析表文件。坏行跳过并记日志，绝不让一行笔误把整张表废掉。"""
    rules: list[tuple[str, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        wrong, sep, right = line.partition("=>")
        wrong, right = wrong.strip(), right.strip()
        if not sep or not wrong or not right:
            log.warning("term-fixes.txt:%d 不是「错串 => 正词」，已跳过：%r", number, raw)
            continue
        rules.append((wrong, right))
    rules.sort(key=lambda item: len(item[0]), reverse=True)
    return rules


def load() -> list[tuple[str, str]]:
    """读表，按 (mtime, size) 缓存——用户改完文件重启就能生效，不必重打包。"""
    global _RULES, _RULES_STAMP
    file = path()
    try:
        stat = file.stat()
    except OSError:
        _RULES, _RULES_STAMP = [], None
        return _RULES
    stamp = (stat.st_mtime, stat.st_size)
    if _RULES is not None and _RULES_STAMP == stamp:
        return _RULES
    try:
        _RULES = parse(file.read_text(encoding="utf-8"))
    except OSError:
        log.exception("读不到 %s", file)
        _RULES = []
    _RULES_STAMP = stamp
    return _RULES


def apply(text: str, rules: list[tuple[str, str]] | None = None) -> str:
    """把 ``text`` 里出现的错串换成正词。表为空时原样返回。"""
    if not text:
        return text
    table = load() if rules is None else rules
    out = text
    for wrong, right in table:
        if wrong in out:
            out = out.replace(wrong, right)
    return out


def rule_count() -> int:
    return len(load())
