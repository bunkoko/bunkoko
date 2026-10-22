"""BM25 用の分かち書き（SPEC §8.4）。本番は SudachiPy、無いときやテストでは文字の2-gram。"""

from __future__ import annotations

from collections.abc import Callable

from koten.textnorm import normalize_for_match

Tokenizer = Callable[[str], list[str]]

_SKIP_POS = {"助詞", "助動詞", "補助記号", "空白", "記号"}


def bigram_tokenize(text: str) -> list[str]:
    s = normalize_for_match(text)
    if len(s) < 2:
        return [s] if s else []
    return [s[i : i + 2] for i in range(len(s) - 1)]


def sudachi_tokenizer() -> Tokenizer:
    try:
        from sudachipy import Dictionary, SplitMode
    except ImportError as e:
        raise RuntimeError(
            "SudachiPy が入っていません。`uv sync --extra ml` を実行するか、"
            'settings.toml の bm25.tokenizer を "bigram" にしてください'
        ) from e
    d = Dictionary()
    # 0.7 以降は tokenizer()、それより前は create()
    tok = d.tokenizer(mode=SplitMode.C) if hasattr(d, "tokenizer") else d.create(SplitMode.C)

    def tokenize(text: str) -> list[str]:
        return [
            m.normalized_form()
            for m in tok.tokenize(text)
            if m.part_of_speech()[0] not in _SKIP_POS
        ]

    return tokenize


def get_tokenizer(kind: str) -> Tokenizer:
    if kind == "sudachi":
        return sudachi_tokenizer()
    if kind == "bigram":
        return bigram_tokenize
    raise ValueError(f"bm25.tokenizer は sudachi か bigram：{kind}")
