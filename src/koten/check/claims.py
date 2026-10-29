"""照合する台本の文（主張）を並べる。

主張は語りの1文。番号（C1…）は表示用で、記録には段落の ID と文の順番（ch2-s3#1）と、
差し込んだあとの文の指紋を残す（台本を作り直して文が変わったら気づくため）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from koten.script.gates import split_sentences
from koten.script.placeholders import PLACEHOLDER, Resolver

# 既定で並べる章：一節か語釈・解釈を扱う章（一節の意味の説明は、ここに出てくる）
SKIP_ROLES = ("opening", "ending")


@dataclass(frozen=True)
class Claim:
    number: str  # C1…（表示用）
    key: str  # ch2-s3#1（段落の ID と、段落の中の文の順番）
    chapter_id: str
    role: str
    text: str  # 差し込んだあとの文
    context: str  # その段落全体（差し込んだあと）
    sha: str
    own: str = ""  # 自分たちの言葉だけ（DB から差し込む所は \x00。言い回しの近さはここで数える）


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _resolve(resolver: Resolver, text: str) -> str:
    try:
        return resolver.resolve(text)
    except KeyError:
        return text


def claims(script: dict[str, Any], resolver: Resolver, *, everything: bool = False) -> list[Claim]:
    """台本の語りの文を、番号つきで並べる。

    everything が False なら、一節（quote）か語釈・解釈（refs）のある章だけ。番号は、
    どちらでも同じ文に同じ番号が付くよう、全部の文で数える。
    """
    out: list[Claim] = []
    n = 0
    for ch in script.get("chapters") or []:
        focus = ch["role"] not in SKIP_ROLES and (
            any(seg["kind"] == "quote" for seg in ch["segments"]) or bool(ch.get("refs"))
        )
        for seg in ch["segments"]:
            if seg["kind"] != "narration":
                continue
            context = _resolve(resolver, seg["text"])
            for k, sent in enumerate(split_sentences(seg["text"]), start=1):
                n += 1
                if not (everything or focus):
                    continue
                text = _resolve(resolver, sent)
                out.append(
                    Claim(
                        number=f"C{n}",
                        key=f"{seg['seg_id']}#{k}",
                        chapter_id=ch["chapter_id"],
                        role=ch["role"],
                        text=text,
                        context=context,
                        sha=sha(text),
                        own=PLACEHOLDER.sub("\x00", sent),
                    )
                )
    return out
