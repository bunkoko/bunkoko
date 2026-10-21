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
