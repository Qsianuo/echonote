"""把 opus-mt 的 SentencePiece 词表编译成 Android 能直接读的文本。

为什么需要这一步：ONNX 里的 token id 不是 SentencePiece 的 id。Marian 用的是
**合并词表**（source 的 32000 个片段 + target 的 32000 个片段，去重后 65000），
而 ``.spm`` 里的 id 从 0 数到 31999——两者对不上，直接喂会得到一串错位的词。

所以这里「一次算清」：

* ``*-src.txt``：``片段<TAB>合并词表 id<TAB>分数``，编码用（unigram Viterbi
  需要分数）；
* ``*-tgt.txt``：``id<TAB>片段``，解码用。

Android 侧只读文本、不解析 protobuf，也不依赖 sentencepiece 库。

用法::

    python tools/export_mt_vocab.py model-dist/mt-onnx/mt-zh-en models/mt-zh-en
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sentencepiece as spm


def _vocab_map(vocab_json: Path) -> dict[str, int]:
    raw = json.loads(vocab_json.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"{vocab_json} 不是 token→id 的映射")
    return {str(token): int(index) for token, index in raw.items()}


def _header(vocab: dict[str, int]) -> str:
    """特殊 id 写进文件头，Android 侧不必把 Marian 的惯例值硬编码进代码。"""
    def of(token: str, fallback: int) -> int:
        return vocab.get(token, fallback)

    return (
        f"# eos={of('</s>', 0)} unk={of('<unk>', 1)} pad={of('<pad>', 65000)}\n"
    )


def build_source(spm_path: Path, vocab: dict[str, int]) -> tuple[str, list[str]]:
    """编码词表：片段、合并 id、分数。缺 id 的片段丢弃（模型不认识它）。"""
    sp = spm.SentencePieceProcessor(model_file=str(spm_path))
    lines: list[str] = []
    dropped: list[str] = []
    for index in range(sp.get_piece_size()):
        if sp.is_control(index):
            continue
        piece = sp.id_to_piece(index)
        target = vocab.get(piece)
        if target is None:
            dropped.append(piece)
            continue
        lines.append(f"{piece}\t{target}\t{sp.get_score(index):.4f}")
    return _header(vocab) + "\n".join(lines) + "\n", dropped


def build_target(spm_path: Path, vocab: dict[str, int]) -> str:
    """解码词表：id → 片段。只保留 id 在合并词表里的片段。"""
    sp = spm.SentencePieceProcessor(model_file=str(spm_path))
    rows: list[tuple[int, str]] = []
    for index in range(sp.get_piece_size()):
        if sp.is_control(index):
            continue
        piece = sp.id_to_piece(index)
        target = vocab.get(piece)
        if target is None:
            continue
        rows.append((target, piece))
    rows.sort()
    return _header(vocab) + "\n".join(f"{i}\t{p}" for i, p in rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Android 用的 opus-mt 词表")
    parser.add_argument("onnx_dir", type=Path, help="optimum 导出目录（含 vocab.json）")
    parser.add_argument("ct2_dir", type=Path, help="原始 ctranslate2 目录（含 *.spm）")
    args = parser.parse_args()

    vocab = _vocab_map(args.onnx_dir / "vocab.json")

    src, dropped = build_source(args.ct2_dir / "source.spm", vocab)
    tgt = build_target(args.ct2_dir / "target.spm", vocab)

    (args.onnx_dir / "mt-src-vocab.txt").write_text(src, encoding="utf-8", newline="\n")
    (args.onnx_dir / "mt-tgt-vocab.txt").write_text(tgt, encoding="utf-8", newline="\n")

    print(f"合并词表 {len(vocab)} 个 token")
    print(f"编码表 {src.count(chr(10))} 行 · 解码表 {tgt.count(chr(10))} 行")
    if dropped:
        print(f"丢弃 {len(dropped)} 个不在合并词表里的片段，例如 {dropped[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
