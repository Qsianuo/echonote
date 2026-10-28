"""对齐「人工校对文本」与「ASR 导出文本」，直接吐出一张候选错词表。

为什么需要它：术语纠错不能靠猜。要把「抢货 → 阈值」这种映射写进词表，前提是
知道老师原话是什么；而人工逐条比对 33 分钟的稿子不现实。这个工具做两件事：

1. 算 CER（字错率），给一次识别质量一个可比的数字；
2. 把 ASR 与真值的差异聚成「错串 → 正词 ×次数」，按出现次数排序，
   直接可以拿去写术语/错词表。

对齐用字符级 difflib，并且会把被 ≤1 个相同字隔开的差异合并成一条——否则
「扫描间距」被拆成「扫」「间」两个碎片，看不了。

用法::

    # 人工校对稿 + ASR 导出（带 [mm:ss] 时间戳也行，会被去掉）
    python tools/diff_transcript.py 对稿.txt 导出.txt
    python tools/diff_transcript.py 对稿.txt 导出.txt --top 40 --min-count 2

输出第一段是统计，第二段是候选表，格式与 `models/glossary.txt` 的注释区一致，
可以直接抄进词表。
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import Counter
from pathlib import Path

STAMP = re.compile(r"^\s*\[\d+:\d+\]\s*")
PUNCT = set("，。、；：？！…—·「」『』（）《》〈〉,.!?;:\"'()[]{}<>-–—~ \t\n　")
HEADER_KEYS = ("EchoNote", "录音时间", "结束时间", "时长", "模型", "语言")


def clean_text(path: Path) -> str:
    """去时间戳、去导出头、去空白，只留下正文。"""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(line.startswith(key) or key in line[:12] for key in HEADER_KEYS):
            continue
        line = STAMP.sub("", line)
        if line:
            out.append(line)
    return "".join(out)


def is_punctuation_only(text: str) -> bool:
    return all(ch in PUNCT for ch in text)


def merge_short_gaps(opcodes: list[tuple[str, int, int, int, int]], min_gap: int = 2):
    """把被很短的一段「相同」隔开的差异合并。

    difflib 会把「扫描间距」和「扫揭句」对成 扫=扫、描↔揭、间=间、距↔句，
    四个碎片里两个是 equal，直接输出就看不出这是同一个词错了。这里把中间
    equal 长度 < ``min_gap`` 的相邻差异并成一条。

    返回 ``[tag, i1, i2, j1, j2]`` 列表，``tag`` 只有 ``equal`` / ``diff``。
    """
    blocks: list[list] = []
    for tag, i1, i2, j1, j2 in opcodes:
        blocks.append(["equal" if tag == "equal" else "diff", i1, i2, j1, j2])

    out: list[list] = []
    for block in blocks:
        tag, i1, i2, j1, j2 = block
        if tag == "equal":
            short = (i2 - i1) < min_gap and (j2 - j1) < min_gap
            if short and out and out[-1][0] == "diff":
                # 先挂起，等下一个 diff 来了才知道要不要吞掉
                out.append(["glue", i1, i2, j1, j2])
            else:
                out.append(["equal", i1, i2, j1, j2])
            continue
        if out and out[-1][0] == "glue":
            previous = out[-2]
            out = out[:-2] + [["diff", previous[1], i2, previous[3], j2]]
        elif out and out[-1][0] == "diff":
            # 连续差异（理论上 difflib 不会给，防御一下）
            out[-1] = ["diff", out[-1][1], i2, out[-1][3], j2]
        else:
            out.append(["diff", i1, i2, j1, j2])
    # 末尾挂起的 glue 还原成 equal：它后面没有差异可合并了
    return [["equal", *block[1:]] if block[0] == "glue" else block for block in out]


def analyse(ref: str, hyp: str, top: int, min_count: int) -> tuple[float, Counter, int]:
    matcher = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    opcodes = matcher.get_opcodes()

    substitutions = 0
    deleted = 0
    inserted = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "replace":
            # 替换按「较长的那个跨度」计入，与常见 CER 实现一致
            substitutions += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            deleted += i2 - i1
        elif tag == "insert":
            inserted += j2 - j1
    cer = (substitutions + deleted + inserted) / max(len(ref), 1)

    pairs: Counter = Counter()
    for _, i1, i2, j1, j2 in merge_short_gaps(opcodes):
        wrong = hyp[j1:j2].strip()
        right = ref[i1:i2].strip()
        if not wrong or not right:
            continue
        if is_punctuation_only(wrong) and is_punctuation_only(right):
            continue
        if wrong == right:
            continue
        # 长差异多半是整句没对上，不适合进词表
        if len(wrong) > 8 or len(right) > 8:
            continue
        pairs[(wrong, right)] += 1
    return cer, pairs, len(ref)


def main() -> int:
    parser = argparse.ArgumentParser(description="对稿 vs ASR 导出：CER + 候选错词表")
    parser.add_argument("reference", type=Path, help="人工校对文本（真值）")
    parser.add_argument("hypothesis", type=Path, help="ASR 导出文本")
    parser.add_argument("--top", type=int, default=30, help="最多列多少条")
    parser.add_argument("--min-count", type=int, default=1, help="至少出现几次才列出")
    args = parser.parse_args()

    ref = clean_text(args.reference)
    hyp = clean_text(args.hypothesis)
    if not ref or not hyp:
        print("有一边是空的，检查一下输入", file=sys.stderr)
        return 2

    cer, pairs, ref_len = analyse(ref, hyp, args.top, args.min_count)
    print(f"真值 {ref_len} 字 / ASR {len(hyp)} 字  →  CER {cer*100:.1f}%")
    print(f"候选差异 {len(pairs)} 种，合计 {sum(pairs.values())} 次\n")

    shown = 0
    for (wrong, right), count in pairs.most_common():
        if count < args.min_count:
            continue
        if shown >= args.top:
            break
        print(f"{wrong} → {right}   ×{count}")
        shown += 1
    if shown == 0:
        print("（没有达到阈值的候选，可以加 --min-count 1）")
    print(
        "\n提示：不同音的替换（如 抢货 → 阈值）要写进「错词表」而不是同音术语表；"
        "同音的（如 脚距 → 焦距）补进 models/glossary.txt 即可。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
