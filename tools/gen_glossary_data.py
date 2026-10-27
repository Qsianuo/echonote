"""把术语表和拼音表编译成一份 JS 数据文件，供移动端做同音纠错。

为什么移动端要另做一份：桌面端纠错跑在 Python 进程里（app/glossary.py 用
pypinyin），而原生壳里识别全在 JS/Kotlin 侧完成，没有 Python 可用。两端
判据必须一致，否则同一段录音在两个平台会出现「一个纠正了、一个没纠正」。

数据分两块：

* ``TERMS``：models/glossary.txt 里的词，长词在前（「标定板」不能被「标定」
  先吃掉）。
* ``PINYIN``：GB2312 全部 6763 个汉字的**无调**读音，按音节分组存
  （``bao:暴曝报…``）。多音字会出现在多个音节下——JS 端逐字查表，用「读音
  集合相交」代替 Python 的整段 lazy_pinyin（JS 没有词库，拿不到「曝光」读
  bao guang 这种词级上下文），所以必须保留全部读音，否则「暴光」对不上
  「曝光」。

用法：``python tools/gen_glossary_data.py``，产物是 ``web/glossary-data.js``。
改了术语表或升级 pypinyin 后重跑即可；tests/test_glossary_js.py 会检查产物
是否与源文件同步。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pypinyin import Style, pinyin

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = ROOT / "models" / "glossary.txt"
OUT = ROOT / "web" / "glossary-data.js"

# GB2312 一级 + 二级汉字区间（按字节对枚举）。
GB2312_RANGES = ((0xB0, 0xA1, 0xD7, 0xF9), (0xD8, 0xA1, 0xF7, 0xFE))


def gb2312_chars() -> list[str]:
    chars: list[str] = []
    for hi_start, lo_start, hi_end, lo_end in GB2312_RANGES:
        for hi in range(hi_start, hi_end + 1):
            lo_from = lo_start if hi == hi_start else 0xA1
            lo_to = lo_end if hi == hi_end else 0xFE
            for lo in range(lo_from, lo_to + 1):
                try:
                    ch = bytes([hi, lo]).decode("gb2312")
                except UnicodeDecodeError:
                    continue
                if len(ch) == 1:
                    chars.append(ch)
    return chars


def load_terms(path: Path = GLOSSARY) -> list[str]:
    """解析术语表：# 开头是注释，一行内空格分隔多个词。"""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for word in line.split():
            word = word.strip()
            if len(word) < 2 or word in seen:
                continue
            seen.add(word)
            terms.append(word)
    # 长词优先：短词先把位置占了，「标定板」就永远匹配不上。
    terms.sort(key=lambda w: (-len(w), w))
    return terms


def pinyin_groups(chars: list[str]) -> dict[str, list[str]]:
    """char → 全部无调读音（多音字全留），反转为 音节 → 字。"""
    readings = pinyin(chars, style=Style.NORMAL, heteronym=True, errors="ignore")
    groups: dict[str, list[str]] = {}
    for ch, variants in zip(chars, readings):
        for syllable in variants:
            bucket = groups.setdefault(syllable, [])
            if ch not in bucket:
                bucket.append(ch)
    return groups


def render(terms: list[str], groups: dict[str, list[str]]) -> str:
    lines = [
        "// 自动生成，请勿手改 —— 改术语表后跑 tools/gen_glossary_data.py。",
        "// 来源：models/glossary.txt + pypinyin（GB2312 6763 字，无调读音）。",
        "",
        "export const TERMS = [",
    ]
    for i in range(0, len(terms), 8):
        chunk = terms[i : i + 8]
        lines.append("  " + " ".join("'" + w + "'," for w in chunk))
    lines.append("];")
    lines.append("")
    lines.append("// 音节:同音字。查询时建 char → [音节] 的反查表。")
    lines.append("export const PINYIN = [")
    for syllable in sorted(groups):
        lines.append("  '" + syllable + ":" + "".join(groups[syllable]) + "',")
    lines.append("].join('\\n');")
    lines.append("")
    return "\n".join(lines)


def generate() -> str:
    """纯函数：返回应当写出的 JS 文本（不落盘，测试直接比对）。"""
    return render(load_terms(), pinyin_groups(gb2312_chars()))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 web/glossary-data.js")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--check", action="store_true", help="只校验产物是否最新")
    args = parser.parse_args()

    text = generate()
    if args.check:
        actual = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if actual != text:
            print(f"{args.out} 已过期，请重跑 tools/gen_glossary_data.py", file=sys.stderr)
            return 1
        print(f"{args.out} 是最新的")
        return 0

    args.out.write_text(text, encoding="utf-8", newline="\n")
    size_kb = len(text.encode("utf-8")) / 1024
    print(f"写出 {args.out} · {size_kb:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
