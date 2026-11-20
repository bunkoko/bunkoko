"""VOICEVOX のエンジンとの話（docs/m6_design.md・SPEC §8.9）。

エンジンは手元（127.0.0.1）で動かす VOICEVOX だけ。外のサイトには台本を送らない。
audio_query と synthesis には必ず同じ speaker ID を渡す（CLAUDE.md）。そのために speaker ID は
VoicevoxClient を作るときに1つだけ決め、どちらのメソッドも引数に取らない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

# VOICEVOX のユーザー辞書が受け付ける読み（カタカナと長音だけ）
_KATAKANA = re.compile(r"[ァ-ヴー]+")


class VoiceError(RuntimeError):
    """VOICEVOX につながらない・合成に失敗したとき。何をすればよいかの文を持つ。"""


def to_katakana(text: str) -> str:
    """ひらがなをカタカナにする（ほかの字はそのまま）。"""
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" or c in "ゝゞ" else c for c in text)


class VoicevoxClient:
    """VOICEVOX のエンジン（HTTP）。テストでは client に httpx.MockTransport の Client を渡す。"""

    def __init__(
        self,
        endpoint: str,
        speaker_id: int,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60,
    ):
        host = urlparse(endpoint).hostname or ""
        if host not in LOCAL_HOSTS:
            raise VoiceError(
                f"VOICEVOX の endpoint（{endpoint}）が手元のエンジンではありません。"
                "settings の [voicevox] endpoint を http://127.0.0.1:50021 にしてください"
                "（台本を外のサイトに送らないため）"
            )
        self.endpoint = endpoint.rstrip("/")
        self.speaker_id = int(speaker_id)
        self.timeout = timeout
        self._own = client is None
        # 環境変数のプロキシ（HTTP_PROXY）は使わない。手元のエンジンにじかにつなぐ
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)

    def close(self) -> None:
        if self._own:
            self._client.close()

    # --- エンジンの API ---------------------------------------------------------

    def version(self) -> str:
        r = self._request("GET", "/version")
        try:
            return str(r.json())
        except ValueError:
            return r.text.strip().strip('"')

    def audio_query(self, text: str) -> dict:
        r = self._request("POST", "/audio_query", params={"text": text, "speaker": self.speaker_id})
        return self._json(r, "/audio_query")

    def synthesis(self, query: dict) -> bytes:
        """wav を返す。speaker は audio_query と同じ（self.speaker_id）。"""
        r = self._request("POST", "/synthesis", params={"speaker": self.speaker_id}, json=query)
        return r.content

    def user_dict(self) -> dict[str, dict[str, Any]]:
        """ユーザー辞書の中身（単語の ID → 表層形・読み・アクセント など）。"""
        return self._json(self._request("GET", "/user_dict"), "/user_dict")

    def register_words(
        self,
        words: list[tuple[str, str]],
        *,
        log: Callable[[str], object] | None = None,
    ) -> int:
        """（表層形, 読み）をユーザー辞書に足す。もうある表層形は足さない。足した数を返す。

        読みはカタカナにして送る（ひらがなは直す。空白・「・」・「＝」は落とす）。読みが空なら送らない。
        かなの表層形で読みが同じもの（作品名「それから」など）は、足しても読みは変わらず、ふだんの語の
        アクセントだけが変わるので足さない。
        カタカナにできない読み（青空文庫の書誌の「――」「！」など）と、エンジンが受け付けない読みは、
        その語だけ飛ばして log に書く（辞書は読みの手助けなので、1語のために合成を止めない）。
        """
        known = {_surface_key(w.get("surface", "")) for w in self.user_dict().values()}
        added = 0
        for surface, reading in words:
            surface = surface.strip()
            pronunciation = re.sub(r"[\s・･＝=]+", "", to_katakana(reading or ""))
            if not surface or not pronunciation or _surface_key(surface) in known:
                continue
            if to_katakana(surface) == pronunciation:
                continue
            if not _KATAKANA.fullmatch(pronunciation):
                _say(
                    log,
                    f"  辞書：「{surface}」の読み「{reading}」はカタカナにできないので飛ばしました",
                )
                continue
            r = self._send(
                "POST",
                "/user_dict_word",
                params={
                    "surface": surface,
                    "pronunciation": pronunciation,
                    "accent_type": 0,
                    "word_type": "PROPER_NOUN",
                },
            )
            if r.status_code == 422:
                _say(
                    log,
                    f"  辞書：「{surface}」の読み「{pronunciation}」を VOICEVOX が受け付けないので"
                    "飛ばしました（小さい字の続き方など）",
                )
                continue
            self._check(r, "/user_dict_word")
            known.add(_surface_key(surface))
            added += 1
        return added

    # --- 中の決まり ------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        return self._check(self._send(method, path, params=params, json=json), path)

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """送って答えを返す（つながらないときだけ VoiceError。答えの番号は見ない）。"""
        try:
            return self._client.request(
                method, f"{self.endpoint}{path}", params=params, json=json, timeout=self.timeout
            )
        except httpx.TimeoutException as e:
            raise VoiceError(
                f"VOICEVOX（{self.endpoint}）が {self.timeout:g} 秒たっても答えませんでした。"
                "VOICEVOX を起動し直してから、もう一度動かしてください"
            ) from e
        except httpx.TransportError as e:
            raise VoiceError(
                f"VOICEVOX（{self.endpoint}）につながりません（{type(e).__name__}）。"
                "VOICEVOX のアプリを起動してから、もう一度動かしてください"
                "（koten doctor で確かめられます）"
            ) from e

    def _check(self, r: httpx.Response, path: str) -> httpx.Response:
        if r.status_code >= 400:
            raise VoiceError(self._status_message(path, r))
        return r

    def _status_message(self, path: str, r: httpx.Response) -> str:
        detail = r.text.strip()[:200]
        msg = f"VOICEVOX が {path} に {r.status_code} を返しました"
        if detail:
            msg += f"（{detail}）"
        if path in ("/audio_query", "/synthesis") and r.status_code in (404, 422):
            return (
                msg + f"。settings の [voicevox] speaker_id（{self.speaker_id}）の声が"
                "VOICEVOX に入っているか確かめてください"
            )
        return msg + "。VOICEVOX を起動し直してから、もう一度動かしてください"

    def _json(self, r: httpx.Response, path: str) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise VoiceError(
                f"VOICEVOX の {path} の答えが JSON ではありません。"
                "endpoint が VOICEVOX のエンジンを指しているか確かめてください"
            ) from e


def _say(log: Callable[[str], object] | None, msg: str) -> None:
    if log is not None:
        log(msg)


def _surface_key(surface: str) -> str:
    """VOICEVOX は表層形を全角にして保存するので、比べるときは NFKC でそろえる。"""
    return unicodedata.normalize("NFKC", surface).strip()
