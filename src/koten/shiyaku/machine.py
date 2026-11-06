"""コードでする訳の確かめ（判定役の前。どれかに当たれば書き直させる）。

- 空・長すぎ・記号や注の書き込み・「試訳」「AI」などの表示の言葉（表示はコードが付ける）
- 今の本（S4）の書名を書いていない
- 時代の解釈（S2）の日本語と言い回しがほぼ同じでない（同じなら自前の訳ではない。その資料を
  出典つきで使うか、書き直す。sources_strategy_plan §4-3）
- 数字（算用数字）は、元の本文か時代の解釈にあるものだけ
"""

from __future__ import annotations

import re
from typing import Any

from koten.check.compare import is_close, wording
from koten.textnorm import normalize_for_match

MIN_MAX = 200  # 長さの上限の下限（短い一節でも、これだけは書ける）
LEN_RATIO = 5  # 元の本文の何倍までの長さにするか
HARD_MAX = 1200
# 訳の中に書かないもの（表示・注・記号はコードが付ける。訳の本文だけを書かせる）
NOT_IN_TEXT = re.compile(
    r"\{\{|\}\}|試訳|当チャンネル|AI|ＡＩ|人工知能|訳注|訳者注|（注|\(注|※|＊|\*\*|#"
)
DIGITS = re.compile(r"[0-9０-９]+")


def machine_issues(
    text: str,
    source: dict[str, Any],
    basis: list[dict[str, Any]],
    s4_titles: list[str] = (),
) -> list[str]:
    out: list[str] = []
    text = text.strip()
    if not text:
        return ["訳が空"]
    src = source.get("text") or ""
    limit = min(max(MIN_MAX, LEN_RATIO * len(src)), HARD_MAX)
    if len(text) > limit:
        out.append(f"長すぎる（{len(text)}字。{limit}字まで。説明を足さず、訳だけを書く）")
    if m := NOT_IN_TEXT.search(text):
        out.append(f"「{m.group(0)}」を書かない（表示・注・記号はコードが付ける。訳だけを書く）")
    for title in s4_titles:
        if title and title in text:
            out.append(f"今の本の書名「{title}」を書かない（R8）")
    for r in basis:
        if r["lang"] != "ja":
            continue
        run, share = wording(text, r.get("display_text") or r["text"])
        if is_close(run, share):
            out.append(
                f"時代の解釈 {r['rendering_id']} と言い回しがほぼ同じ（{run}字が同じ並び）。"
                "自前の訳にならない（原文とほかの版から、自分の言葉で書き直す）"
            )
    known = normalize_for_match(src + "".join(r["text"] for r in basis))
    for m in DIGITS.finditer(normalize_for_match(text)):
        if m.group(0) not in known:
            out.append(f"数「{m.group(0)}」が元の本文にも時代の解釈にも無い")
    return out
