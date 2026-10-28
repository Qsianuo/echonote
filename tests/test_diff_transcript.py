"""对稿差异工具（tools/diff_transcript.py）的行为契约。

这个工具是「建术语/错词表」的第一道工序：它错了，后面整张表都错。所以这里
用几组人造的 真值/识别结果 锁住三件事：

1. 被一个相同字隔开的碎片要被合并成一条（扫·描间距 ↔ 扫·揭句），否则人工
   看到的是四条没意义的碎片；
2. 纯标点差异不进候选表（标点由定稿模型自己补，不是术语问题）；
3. CER 的算法要说得清：替换 + 漏字 + 多字，除以真值字数。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from diff_transcript import analyse, clean_text, merge_short_gaps  # noqa: E402


def test_fragments_separated_by_one_char_are_merged():
    # difflib 会先吃掉共同前缀「扫」，剩下的差异必须合成一条，
    # 而不是「描↔揭」「间=间」「距↔句」三段碎片。
    ref = "扫描间距是什么意思"
    hyp = "扫揭句是什么意思"
    _, pairs, _ = analyse(ref, hyp, 10, 1)
    assert len(pairs) == 1, f"碎片没合并干净: {dict(pairs)}"
    wrong, right = next(iter(pairs))
    assert (wrong, right) == ("揭句", "描间距")


def test_punctuation_only_differences_are_ignored():
    ref = "这个是阈值，那个是二值化。"
    hyp = "这个是阈值那个是二值化"
    _, pairs, _ = analyse(ref, hyp, 10, 1)
    assert not pairs, f"纯标点差异不该进表: {dict(pairs)}"


def test_long_mismatches_are_skipped():
    ref = "完全不一样的一整句话在这里"
    hyp = "另一套说辞也都不是一个东西"
    _, pairs, _ = analyse(ref, hyp, 10, 1)
    assert not pairs, "整句级别的差异不该当成词表候选"


def test_cer_counts_substitutions():
    ref = "阈值分割"
    hyp = "抢货分割"
    cer, pairs, ref_len = analyse(ref, hyp, 10, 1)
    assert ref_len == 4
    assert abs(cer - 2 / 4) < 1e-6  # 两个字都替换
    assert dict(pairs) == {("抢货", "阈值"): 1}


def test_repeated_errors_are_counted():
    ref = "阈值是第一项，阈值不能太高"
    hyp = "抢货是第一项，抢货不能太高"
    _, pairs, _ = analyse(ref, hyp, 10, 1)
    assert dict(pairs) == {("抢货", "阈值"): 2}


def test_clean_text_strips_header_and_timestamps(tmp_path):
    sample = tmp_path / "out.txt"
    sample.write_text(
        "EchoNote 转写记录\n录音时间：2026-10-28 08:33:23\n时长：33:18\n模型：SenseVoice 离线\n\n"
        "[00:03] 第一句。\n[00:33] 第二句。\n",
        encoding="utf-8",
    )
    assert clean_text(sample) == "第一句。第二句。"


def test_merge_keeps_clean_boundaries():
    """两段差异之间隔了 ≥2 个字时不能再合并。"""
    ops = [("replace", 0, 2, 0, 2), ("equal", 2, 9, 2, 9)]
    merged = merge_short_gaps(ops)
    assert [block[0] for block in merged] == ["diff", "equal"]
    assert merged[0][1:5] == [0, 2, 0, 2]
    assert merged[1][1:5] == [2, 9, 2, 9]


def test_short_equal_between_two_diffs_is_absorbed():
    ops = [
        ("replace", 0, 1, 0, 1),
        ("equal", 1, 2, 1, 2),
        ("replace", 2, 4, 2, 3),
        ("equal", 4, 6, 3, 5),
    ]
    merged = merge_short_gaps(ops)
    assert merged[0][0] == "diff"
    assert merged[0][1:5] == [0, 4, 0, 3], merged
