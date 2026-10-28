"""非同音错词表（app/term_fixes.py）的行为契约。

这一层没有词边界可依，误伤的代价比漏纠大得多，所以用例的重点不是「能纠」，
而是「不该动的绝不动」：

* 表里没有的串一个字不改；
* 光秃秃的「预算」不能被改（只有带开/闭/IP/B 前缀的才认）；
* 长串优先——「闭预算」不能被「预算」类规则先吃掉一半。
"""

from __future__ import annotations

from pathlib import Path

from app import term_fixes

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT / "models" / "term-fixes.txt"


def test_bundled_table_parses_and_is_sorted_longest_first():
    rules = term_fixes.parse(TABLE.read_text(encoding="utf-8"))
    assert rules, "错词表是空的"
    lengths = [len(wrong) for wrong, _ in rules]
    assert lengths == sorted(lengths, reverse=True), "长串必须排在前面"
    wrongs = {wrong for wrong, _ in rules}
    # 光秃秃的「预算」不许进表：它在正常中文里太常见
    assert "预算" not in wrongs


def test_template_matching_is_fixed():
    rules = term_fixes.parse("魔方匹配 => 模板匹配\n")
    assert term_fixes.apply("这边用魔方匹配找特征", rules) == "这边用模板匹配找特征"


def test_binarisation_variants_are_fixed():
    rules = term_fixes.parse("二次化 => 二值化\n按次化 => 二值化\n")
    assert term_fixes.apply("做一下二次化处理", rules) == "做一下二值化处理"
    assert term_fixes.apply("按次化之后", rules) == "二值化之后"


def test_longest_rule_wins():
    rules = term_fixes.parse("预算 => 预算占位\n闭预算 => 闭运算\n")
    rules.sort(key=lambda item: len(item[0]), reverse=True)
    assert term_fixes.apply("再加一个闭预算", rules) == "再加一个闭运算"


def test_bare_budget_is_untouched():
    rules = term_fixes.parse("开预算 => 开运算\n闭预算 => 闭运算\n")
    text = "这个月的预算还没批，开预算和闭预算都要走流程"
    out = term_fixes.apply(text, rules)
    assert out.startswith("这个月的预算还没批"), out
    assert "开运算" in out and "闭运算" in out


def test_unknown_text_is_untouched():
    rules = term_fixes.parse("魔方匹配 => 模板匹配\n")
    for text in ["今天天气不错", "", "模板匹配本来就是对的"]:
        assert term_fixes.apply(text, rules) == text


def test_bad_lines_are_skipped_not_fatal():
    rules = term_fixes.parse("这一行没有箭头\n魔方匹配 => 模板匹配\n=> 只有右边\n")
    assert rules == [("魔方匹配", "模板匹配")]


def test_missing_table_means_no_change(monkeypatch, tmp_path):
    monkeypatch.setattr(term_fixes, "_RULES", None)
    monkeypatch.setattr(term_fixes, "_RULES_STAMP", None)
    monkeypatch.setattr(
        term_fixes, "path", lambda: tmp_path / "nope.txt"
    )
    assert term_fixes.load() == []
    assert term_fixes.apply("魔方匹配") == "魔方匹配"


def test_qianghuo_trigger_is_sentence_scoped():
    # 2026-10-28 用户确认「抢货→强度」，但触发串必须带完整句式：
    # 放宽到「抢货的话」会误伤正常语境（见反例）。
    rules = term_fixes.parse("抢货的话是什么意思 => 强度的话是什么意思\n")
    assert term_fixes.apply("大家抢货的话是什么意思呢？", rules) == "大家强度的话是什么意思呢？"
    assert term_fixes.apply("双11大家抢货的话要趁早。", rules) == "双11大家抢货的话要趁早。"


def test_heibai_stutter_is_fixed_as_one_span():
    rules = term_fixes.parse(
        "黑台电黑台电话 => 黑白变化\n黑台电话 => 黑白变化\n黑台电 => 黑白变化\n"
    )
    out = term_fixes.apply("是黑台电黑台电话的那个梯度。", rules)
    assert "黑白变化" in out and "黑台" not in out


def test_haiganghui_maps_to_grayscale():
    rules = term_fixes.parse("海港灰 => 灰度化\n")
    assert term_fixes.apply("海港灰的话，目前的话就是按第三个。", rules).startswith("灰度化的话")


def test_qianghuo_trigger_is_sentence_scoped():
    # 2026-10-28 用户确认「抢货→强度」，但触发串必须带完整句式：
    # 放宽到「抢货的话」会误伤正常语境（见反例）。
    rules = term_fixes.parse("抢货的话是什么意思 => 强度的话是什么意思\n")
    assert term_fixes.apply("大家抢货的话是什么意思呢？", rules) == "大家强度的话是什么意思呢？"
    assert term_fixes.apply("双11大家抢货的话要趁早。", rules) == "双11大家抢货的话要趁早。"


def test_heibai_stutter_is_fixed_as_one_span():
    rules = term_fixes.parse(
        "黑台电黑台电话 => 黑白变化\n黑台电话 => 黑白变化\n黑台电 => 黑白变化\n"
    )
    out = term_fixes.apply("是黑台电黑台电话的那个梯度。", rules)
    assert "黑白变化" in out and "黑台" not in out


def test_haiganghui_maps_to_grayscale():
    rules = term_fixes.parse("海港灰 => 灰度化\n")
    assert term_fixes.apply("海港灰的话，目前的话就是按第三个。", rules).startswith("灰度化的话")
