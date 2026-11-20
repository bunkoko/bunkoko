"""M6 の音声の合成（koten.voice.voicevox・koten.voice.synth）。VOICEVOX は偽物。"""

from __future__ import annotations

import io
import json
import wave

import httpx
import pytest

from koten.media.model import Line, VoiceManifest
from koten.voice.synth import synthesize
from koten.voice.voicevox import VoiceError, VoicevoxClient, to_katakana

ENDPOINT = "http://127.0.0.1:50021"
SEC_PER_CHAR = 0.1  # 偽のエンジンは、読み1字あたり 0.1 秒（speedScale で割る）の無音を返す


def _wav(sec: float, rate: int = 24000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(2 * channels * round(sec * rate)))
    return buf.getvalue()


def _wav_sec(path) -> float:
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth()) == (1, 2)
        return w.getnframes() / w.getframerate()


class FakeEngine:
    """VOICEVOX の偽物。受けた要求を requests に残す。"""

    def __init__(
        self,
        user_dict: dict | None = None,
        status: dict[str, int] | None = None,
        reject: set[str] | None = None,
    ):
        self.requests: list[httpx.Request] = []
        self.dict = dict(user_dict or {})
        self.status = status or {}
        self.reject = reject or set()  # 受け付けない読み（422 を返す）

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, q = request.url.path, request.url.params
        if path in self.status:
            return httpx.Response(self.status[path], json={"detail": "だめ"})
        if path == "/version":
            return httpx.Response(200, json="0.24.1")
        if path == "/user_dict":
            return httpx.Response(200, json=self.dict)
        if path == "/audio_query":
            return httpx.Response(200, json={"kana": q["text"], "speedScale": 1.0})
        if path == "/synthesis":
            body = json.loads(request.content)
            sec = len(body["kana"]) * SEC_PER_CHAR / body["speedScale"]
            rate = body["outputSamplingRate"]
            channels = 1 if body["outputStereo"] is False else 2
            return httpx.Response(200, content=_wav(sec, rate, channels))
        if path == "/user_dict_word":
            if q["pronunciation"] in self.reject:
                return httpx.Response(422, json={"detail": "無効な発音です"})
            uid = f"u{len(self.dict)}"
            self.dict[uid] = {"surface": q["surface"], "pronunciation": q["pronunciation"]}
            return httpx.Response(200, json=uid)
        return httpx.Response(404)

    def paths(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def _client(engine, speaker_id: int = 3) -> VoicevoxClient:
    return VoicevoxClient(
        ENDPOINT, speaker_id, client=httpx.Client(transport=httpx.MockTransport(engine))
    )


def _line(chapter, seg, kind, display, reading, visual, title, role) -> Line:
    return Line(chapter, seg, kind, display, reading, {"type": visual}, title, role)


def _lines() -> list[Line]:
    return [
        _line(
            "ch1",
            "ch1-s1",
            "narration",
            "さうして。二つ目の文。",
            "そうして。二つ目の文。",
            "photo",
            "はじめに",
            "opening",
        ),
        _line("ch1", "ch1-s2", "quote", "三。", "三。", "quote_card", "はじめに", "opening"),
        _line("ch2", "ch2-s1", "narration", "四の文。", "四の文。", "photo", "古典", "classic"),
    ]


def _run(engine, out, **kw) -> VoiceManifest:
    return synthesize(
        _lines(),
        out,
        _client(engine, kw.pop("speaker_id", 3)),
        episode_id="E1",
        log=lambda _m: None,
        **kw,
    )


def test_timings_add_up_from_real_lengths(tmp_path):
    m = _run(FakeEngine(), tmp_path)
    ch1, ch2 = m.chapters
    s1, s2 = ch1.segments
    # 文：「そうして。」0.5秒 → 間 0.25 → 0.6秒。段落の間 0.6。章の終わりの間 1.2
    assert [(x.start, x.end) for x in s1.sentences] == pytest.approx([(0, 0.5), (0.75, 1.35)])
    assert (s1.start, s1.end) == pytest.approx((0, 1.35))
    assert (s2.start, s2.end) == pytest.approx((1.95, 2.15))
    assert ch1.duration == pytest.approx(3.35) and ch1.offset == 0
    assert ch2.offset == pytest.approx(3.35) and ch2.duration == pytest.approx(1.6)
    assert m.total_sec == pytest.approx(4.95)
    # 画面の文は原文、読みは直したもの
    assert s1.sentences[0].display == "さうして。" and s1.sentences[0].reading == "そうして。"
    assert (ch1.title, ch1.role, ch1.wav) == ("はじめに", "opening", "voice/ch1.wav")
    assert s2.visual == {"type": "quote_card"}
    # 章の wav の長さと manifest の長さが一致し、manifest.json が読み直せる
    assert _wav_sec(tmp_path / "voice/ch1.wav") == pytest.approx(ch1.duration)
    assert _wav_sec(tmp_path / "voice/ch2.wav") == pytest.approx(ch2.duration)
    assert VoiceManifest.load(tmp_path / "voice/manifest.json") == m
    assert (m.engine_version, m.speaker_id, m.sample_rate) == ("0.24.1", 3, 24000)


def test_same_speaker_for_audio_query_and_synthesis(tmp_path):
    engine = FakeEngine()
    m = _run(engine, tmp_path, speaker_id=7, speed_scale=2.0, sample_rate=48000)
    queries, synths = engine.paths("/audio_query"), engine.paths("/synthesis")
    assert len(queries) == len(synths) == 4
    assert {r.url.params["speaker"] for r in queries + synths} == {"7"}
    assert [r.url.params["text"] for r in queries][0] == "そうして。"
    body = json.loads(synths[0].content)
    assert (body["speedScale"], body["outputSamplingRate"], body["outputStereo"]) == (
        2.0,
        48000,
        False,
    )
    # speed 2.0 で文は半分の長さ
    assert m.chapters[0].segments[0].sentences[0].end == pytest.approx(0.25)
    assert m.speaker_id == 7 and m.sample_rate == 48000


def test_cache_reuses_sentence_wavs(tmp_path):
    first = _run(FakeEngine(), tmp_path)
    assert len(list((tmp_path / "voice/cache").glob("*.wav"))) == 4
    again = FakeEngine()
    assert _run(again, tmp_path) == first
    assert again.paths("/audio_query") == [] and again.paths("/synthesis") == []
    # 速さ・声が変われば作り直す
    faster = FakeEngine()
    _run(faster, tmp_path, speed_scale=1.5)
    assert len(faster.paths("/synthesis")) == 4
    other = FakeEngine()
    _run(other, tmp_path, speaker_id=8)
    assert len(other.paths("/synthesis")) == 4
    # 辞書に読みを足したら、同じ読みでも作り直す
    with_word = FakeEngine({"u0": {"surface": "天心", "pronunciation": "テンシン"}})
    _run(with_word, tmp_path)
    assert len(with_word.paths("/synthesis")) == 4


def test_human_take_wav_is_used_as_is(tmp_path):
    (tmp_path / "voice").mkdir()
    (tmp_path / "voice/take.wav").write_bytes(_wav(2.0))
    lines = _lines()
    lines[1].audio_path = "voice/take.wav"
    lines[1].display = "わたしの話。\nもう一つ。"
    engine = FakeEngine()
    m = synthesize(lines, tmp_path, _client(engine), episode_id="E1", log=lambda _m: None)
    seg = m.chapters[0].segments[1]
    assert (seg.start, seg.end) == pytest.approx((1.95, 3.95))
    assert [s.display for s in seg.sentences] == ["わたしの話。もう一つ。"]
    assert len(engine.paths("/synthesis")) == 3  # 人の一言は合成しない
    # サンプリング周波数が違えば、ffmpeg での直し方を案内する
    (tmp_path / "voice/take.wav").write_bytes(_wav(1.0, rate=44100))
    with pytest.raises(VoiceError, match="ffmpeg.*-ar 24000"):
        synthesize(lines, tmp_path, _client(FakeEngine()), episode_id="E1", log=lambda _m: None)
    lines[1].audio_path = "voice/none.wav"
    with pytest.raises(VoiceError, match="ありません"):
        synthesize(lines, tmp_path, _client(FakeEngine()), episode_id="E1", log=lambda _m: None)


def test_timings_are_contiguous_and_match_the_wav(tmp_path):
    """文・段落は重ならずに並び、章の offset は前の章の長さの合計。章は最初に出てきた順。"""
    lines = _lines()
    lines.insert(
        1, _line("ch2", "ch2-s0", "narration", "先に。", "先に。", "photo", "古典", "classic")
    )
    lines.append(_line("ch3", "ch3-s1", "quote", "＊", "", "quote_card", "空", "summary"))
    lines.append(_line("ch4", "ch4-s1", "narration", "終。", "終。", "photo", "おわり", "summary"))
    m = synthesize(lines, tmp_path, _client(FakeEngine()), episode_id="E1", log=lambda _m: None)
    assert [c.chapter_id for c in m.chapters] == ["ch1", "ch2", "ch4"]  # ch3 は読む段落が無い
    assert [s.seg_id for s in m.chapters[1].segments] == ["ch2-s0", "ch2-s1"]
    total = 0.0
    for ch in m.chapters:
        assert ch.offset == pytest.approx(total)
        total += ch.duration
        last = 0.0
        for seg in ch.segments:
            assert last <= seg.start < seg.end
            assert seg.sentences[0].start == seg.start and seg.sentences[-1].end == seg.end
            for a, b in zip(seg.sentences, seg.sentences[1:], strict=False):
                assert a.end <= b.start
            last = seg.end
        assert last < ch.duration  # 章の終わりの間は段落に入れない
        assert _wav_sec(tmp_path / ch.wav) == pytest.approx(ch.duration, abs=1e-9)
    assert m.total_sec == pytest.approx(total)
    assert not (tmp_path / "voice/ch3.wav").exists()


def test_bad_chapter_id_stops_before_synthesis(tmp_path):
    lines = _lines()
    lines[-1].chapter_id = "../ch2"
    engine = FakeEngine()
    with pytest.raises(ValueError, match="章の ID"):
        synthesize(lines, tmp_path, _client(engine), episode_id="E1", log=lambda _m: None)
    assert engine.paths("/synthesis") == [] and not (tmp_path / "voice").exists()


def test_empty_reading_is_skipped(tmp_path):
    lines = _lines()
    lines[1].reading = ""
    logs: list[str] = []
    m = synthesize(lines, tmp_path, _client(FakeEngine()), episode_id="E1", log=logs.append)
    assert [s.seg_id for s in m.chapters[0].segments] == ["ch1-s1"]
    assert m.chapters[0].duration == pytest.approx(1.35 + 1.2)
    assert any("ch1-s2" in x and "飛ばし" in x for x in logs)


def test_errors_say_what_to_do(tmp_path):
    def refused(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(VoiceError, match="起動"):
        _client(refused).version()
    with pytest.raises(VoiceError, match=r"speaker_id（3）"):
        _client(FakeEngine(status={"/audio_query": 422})).audio_query("文。")
    with pytest.raises(VoiceError, match="speaker_id"):
        _run(FakeEngine(status={"/synthesis": 404}), tmp_path)
    with pytest.raises(VoiceError, match="127.0.0.1"):
        VoicevoxClient("https://voicevox.example.com", 3)
    with pytest.raises(ValueError):
        synthesize([], tmp_path, _client(FakeEngine()), episode_id="E1")


def test_register_words_adds_only_new_surfaces():
    engine = FakeEngine({"a": {"surface": "ＡＢＣ", "pronunciation": "エービーシー"}})
    client = _client(engine)
    words = [
        ("ABC", "えーびーしー"),  # 全角で入っているので足さない
        ("岡倉天心", "おかくら てんしん"),
        ("覚三", "カクゾウ"),
        ("読み無し", ""),
        ("岡倉天心", "オカクラテンシン"),  # 同じ表層形は1回だけ
    ]
    assert client.register_words(words) == 2
    sent = [r.url.params for r in engine.paths("/user_dict_word")]
    assert [(p["surface"], p["pronunciation"]) for p in sent] == [
        ("岡倉天心", "オカクラテンシン"),
        ("覚三", "カクゾウ"),
    ]
    assert {(p["accent_type"], p["word_type"]) for p in sent} == {("0", "PROPER_NOUN")}
    assert client.register_words([("覚三", "かくぞう")]) == 0


def test_register_words_skips_bad_readings_without_stopping():
    engine = FakeEngine(reject={"ァァ"})
    client = _client(engine)
    logs: list[str] = []
    words = [
        ("漱石", "夏目sōseki"),  # カタカナにできない読み
        ("Ｋの昇天", "けーのしょうてん――あるいは"),  # 書誌の読みに記号が混ざる
        ("妙な語", "ぁぁ"),  # エンジンが 422 を返す
        ("それから", "それから"),  # かなの表層形で読みが同じ（ふだんの語のアクセントを変えない）
        ("にごりゑ", "にごりえ"),  # かなでも読みが違えば足す
        ("ルソー", "じゃん＝じゃっく・るそー"),  # 「＝」は落とす
    ]
    assert client.register_words(words, log=logs.append) == 2
    sent = [
        (r.url.params["surface"], r.url.params["pronunciation"])
        for r in engine.paths("/user_dict_word")
    ]
    assert sent == [
        ("妙な語", "ァァ"),
        ("にごりゑ", "ニゴリエ"),
        ("ルソー", "ジャンジャックルソー"),
    ]
    assert len(logs) == 3 and all("飛ばし" in x for x in logs)
    assert any("漱石" in x for x in logs) and any("妙な語" in x for x in logs)
    # 422 以外の失敗（エンジンの故障）は止める
    broken = _client(FakeEngine(status={"/user_dict_word": 500}))
    with pytest.raises(VoiceError, match="500"):
        broken.register_words([("漱石", "そうせき")])


def test_to_katakana_and_version():
    assert to_katakana("おかくら・ゔぁ ゝ") == "オカクラ・ヴァ ヽ"
    assert _client(FakeEngine()).version() == "0.24.1"


def test_own_client_ignores_proxy_settings(monkeypatch):
    # 台本を手元のエンジンの外に出さないよう、環境変数のプロキシは使わない
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    client = VoicevoxClient(ENDPOINT, 3)
    try:
        assert client._client.trust_env is False
    finally:
        client.close()
