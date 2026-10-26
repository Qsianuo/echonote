"""术语纠错的行为契约。

只测公开接口 ``correct(text, terms)`` 的可观察行为，不碰内部实现。

背景：定稿引擎换成 SenseVoice 后，专业字打得出来了，但同音词仍会错
（「焦距」→「脚距」、「曝光」→「暴光」）。热词对这些词无效——流式模型的
词表只有 2002 个 token，压根没有「焦」「曝」这些字。所以只能在定稿之后
按术语表纠正。
"""

from app.glossary import correct

TERMS = ["焦距", "曝光", "模板匹配", "阈值分割", "景深", "标定", "伺服"]


def test_correct_term_is_left_untouched():
    assert correct("焦距是五十毫米", TERMS) == "焦距是五十毫米"


def test_homophone_is_replaced():
    assert correct("脚距是五十毫米", TERMS) == "焦距是五十毫米"


def test_other_homophone_is_replaced():
    assert correct("交距调到三十五", TERMS) == "焦距调到三十五"


def test_unrelated_text_is_untouched():
    # 词表里没有、读音也对不上的内容一个字都不能动。
    assert correct("今天天气怎么样", TERMS) == "今天天气怎么样"


def test_multiple_terms_in_one_sentence():
    assert correct("模板马匹配之后要暴光", TERMS) == "模板匹配之后要曝光"


def test_punctuation_and_spacing_survive():
    assert correct("先做标定，再做暴光调节。", TERMS) == "先做标定，再做曝光调节。"


def test_empty_glossary_is_a_no_op():
    assert correct("脚距是五十毫米", []) == "脚距是五十毫米"


def test_empty_text_is_safe():
    assert correct("", TERMS) == ""


def test_repeated_hits_are_all_fixed():
    assert correct("脚距一，脚距二", TERMS) == "焦距一，焦距二"


def test_shorter_window_is_not_matched():
    # 「景」单独出现不该被补成「景深」——纠错只能就地替换同长度的同音片段。
    assert correct("这里的景不错", TERMS) == "这里的景不错"


def test_complete_term_is_not_expanded_to_a_longer_one():
    # 说了「标定」就是「标定」，不能因为词表里有「标定板」就给它补出个「板」。
    assert correct("标定完成，开始暴光", ["标定", "标定板", "曝光"]) == "标定完成，开始曝光"
