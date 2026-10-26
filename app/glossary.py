"""按术语表纠正定稿文本里的同音错字。

为什么需要它：流式识别器（sherpa-zipformer）的词表只有 2002 个 token，
「焦」「曝」「匹」「伺」这些字根本不在表里，所以 hotwords 对工业术语无效
（实测 18 个候选词静默丢弃 7 个）。定稿引擎 SenseVoice 的词表有 25055 个
token，字打得出来，但同音词仍会错——「焦距」成「脚距」、「曝光」成「暴光」。

这里只用一条判据：**读音相同**。可选三种对齐方式，都是就地替换：

1. 等长且拼音完全一致（脚距 → 焦距）
2. 比术语多一个音节（模板马匹配 → 模板匹配）
3. 比术语少一个音节（模板配 → 模板匹配）

不做语义判断：词表之外的文字一个字都不动，所以误伤范围被限制在"与某个术语
同音"的片段上，而这类片段在正常中文里的密度极低。
"""

from __future__ import annotations

from collections.abc import Sequence

from pypinyin import lazy_pinyin


def _all_han(s: str) -> bool:
    """窗口内必须全是汉字——否则「，脚距」会被当成「脚距」多一个音节而连标点一起吞掉。"""
    return all("\u4e00" <= ch <= "\u9fa5" for ch in s)


def _aligns(window: tuple[str, ...], term: tuple[str, ...]) -> bool:
    """window 的读音是否可视为 term 的同音写法。

    等长同音永远允许；多/少一个音节只对 3 字以上的术语放行——2 字术语太容易
    把邻居吃掉（实测「先做标定」里的「做」会被当成多出来的音节，整段拼成
    「标定」）。宁可少纠，不可纠错。
    """
    if window == term:
        return True
    if len(term) < 3:
        return False
    if len(window) == len(term) + 1:
        for skip in range(len(window)):
            if window[:skip] + window[skip + 1 :] == term:
                return True
    return False


def correct(text: str, terms: Sequence[str]) -> str:
    """把 ``text`` 里与 ``terms`` 同音的片段替换成术语本身。"""
    if not text or not terms:
        return text

    table = []
    for term in terms:
        term = (term or "").strip()
        if len(term) < 2:
            continue
        # 整段查，和窗口同一套口径：逐字查会把「曝光」算成 pu guang。
        table.append((term, tuple(lazy_pinyin(term))))
    if not table:
        return text
    # 长术语优先，避免「标定板」被「标定」先吃掉。
    table.sort(key=lambda item: len(item[0]), reverse=True)

    syllables_cache: dict[str, tuple[str, ...]] = {}

    def key_of(snippet: str) -> tuple[str, ...]:
        hit = syllables_cache.get(snippet)
        if hit is None:
            # 必须整段查：单字读音会丢词级上下文（「曝」单字读 pu，但在「曝光」
            # 里读 bao），逐字查会把「曝光」判成 pu guang，同音的「暴光」就对不上。
            hit = tuple(lazy_pinyin(snippet))
            syllables_cache[snippet] = hit
        return hit

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        matched: tuple[int, str] | None = None
        for term, term_key in table:
            size = len(term)
            for width in (size, size + 1, size - 1):
                if width < 2 or i + width > n:
                    continue
                snippet = text[i : i + width]
                if not _all_han(snippet):
                    continue
                if not _aligns(key_of(snippet), term_key):
                    continue
                matched = (width, snippet if snippet == term else term)
                break
            if matched is not None:
                break
        if matched is None:
            out.append(text[i])
            i += 1
        else:
            out.append(matched[1])
            i += matched[0]
    return "".join(out)
