"""照合用の正規化（SPEC §8.2 text_norm、§8.8 G1）。

空白・改行・全角半角の揺れだけを吸収し、漢字は変えない。
NFKC は互換漢字を変えるので、全体には使わない。
"""

import re
import unicodedata

_HALFWIDTH_KANA = re.compile(r"[｡-ﾟ]+")


def normalize_for_match(text: str) -> str:
    out = []
    for ch in text:
        if ch.isspace():
            continue
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:  # 全角英数・記号 → 半角
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    joined = "".join(out)
    return _HALFWIDTH_KANA.sub(lambda m: unicodedata.normalize("NFKC", m.group()), joined)


def normalize_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """normalize_for_match と同じ正規化をし、正規化後の1文字ごとに元の位置（開始, 終了）を返す。"""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        m = _HALFWIDTH_KANA.match(text, i)
        if m:
            for ch in unicodedata.normalize("NFKC", m.group()):
                out.append(ch)
                spans.append((m.start(), m.end()))
            i = m.end()
            continue
        ch = text[i]
        if not ch.isspace():
            code = ord(ch)
            out.append(chr(code - 0xFEE0) if 0xFF01 <= code <= 0xFF5E else ch)
            spans.append((i, i + 1))
        i += 1
    return "".join(out), spans


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """quote が text のどこにあるかを返す。

    まず完全一致で探し、無ければ空白・改行・全角半角の揺れだけを無視して探す。
    戻り値は text 上の（開始, 終了）で、text[開始:終了] が保存する引用文になる。
    """
    quote = quote.strip()
    if not quote:
        return None
    idx = text.find(quote)
    if idx >= 0:
        return idx, idx + len(quote)
    norm_quote = normalize_for_match(quote)
    norm_text, spans = normalize_with_spans(text)
    idx = norm_text.find(norm_quote) if norm_quote else -1
    if idx < 0:
        return None
    return spans[idx][0], spans[idx + len(norm_quote) - 1][1]
