"""確認画面（koten.review.page）と、承認・差し戻し・人の一言（koten.review.actions）。

Playwright（Chromium）が無ければ、ブラウザで開いて確かめるテストは skip する。
"""

import json

import pytest

from koten.db import connect
from koten.media.model import ReviewBundle
from koten.review.actions import ReviewError, approve, reject, set_human_take
from koten.review.page import build_review_html, commands

EP = "E20261120-01"


def bundle(tmp_path, **kw):
    media = tmp_path / "hdd" / "media" / EP
    (media / "render").mkdir(parents=True)
    (media / "kit").mkdir(parents=True)
    video = media / "render" / f"{EP}.mp4"
    video.write_bytes(b"")
    thumb = media / "kit" / "thumb_1.png"
    thumb.write_bytes(b"")
    base = {
        "episode_id": EP,
        "title": "同期の昇進を喜べない――古典の一節で考える",
        "state": "rendered",
        "script_lines": ["# ch1 オープニング", "  同期が先に昇進した夜。"],
        "quotes": [
            {
                "seg_id": "ch2-s2",
                "display": "人の己を知らざるを患へず",
                "reading": "人の己を知らざるを患えず",
                "source": "孔子『論語』「学而」",
                "license": "PD_JP",
            },
            {
                "seg_id": "ch3-s1",
                "display": "ゆく河の流れは絶えずして",
                "reading": "ゆく河の流れは絶えずして",
                "source": "鴨長明『方丈記』",
                "license": "PD_JP",
            },
        ],
        "sources": [{"kind": "古典", "text": "孔子『論語』　青空文庫", "license": "PD_JP"}],
        "gates": {
            "G1": {"ok": True, "issues": []},
            "G3": {"ok": False, "issues": ["ch2：根拠の無い主張"], "needs_human": True},
            "G6": {"ok": True, "issues": [], "review_required": True, "reasons": ["NDL 由来"]},
            "G7": {"ok": False, "issues": ["合計 9分30秒 が 10分〜15分 の外"], "total_sec": 570.0},
            "viewing": {"ok": False, "issues": ["冒頭30秒に悩みの言葉が無い"]},
        },
        "video": str(video),
        "thumbnails": [str(thumb)],
        "description": "同期の昇進を喜べない夜に。\n▼目次\n00:00 はじめに",
        "chapters": ["00:00 はじめに", "01:10 論語", "05:00 まとめ"],
        "ad_breaks": [70.4, 300.0],
        "warnings": ["G6：人の確認が要る回です"],
        "blockers": ["G7 不合格：合計 9分30秒 が 10分〜15分 の外"],
        "human_take": {"chapter_id": "ch5", "text": None, "audio_path": None},
    }
    base.update(kw)
    return ReviewBundle(**base)


def out_path(tmp_path):
    return tmp_path / "hdd" / "exports" / EP / "review.html"


# --- 確認画面 -------------------------------------------------------------------


def test_page_escapes_every_text(tmp_path):
    evil = '<script>alert("x")</script>&'
    b = bundle(
        tmp_path,
        title=evil,
        script_lines=[evil],
        quotes=[
            {"seg_id": evil, "display": evil, "reading": evil, "source": evil, "license": evil}
        ],
        sources=[{"kind": evil, "text": evil, "license": evil}],
        gates={"G1": {"ok": False, "issues": [evil]}},
        description=evil,
        chapters=[evil],
        warnings=[evil],
        blockers=[evil],
        human_take={"chapter_id": evil, "text": evil, "audio_path": None},
        audio=[
            {
                "title": evil,
                "path": str(tmp_path / "all.wav"),
                "sec": 1.0,
                "lines": [
                    {"chapter": evil, "t0": 0.0},
                    {"t0": 0.0, "t1": 1.0, "text": evil, "reading": evil, "kind": "quote"},
                ],
            }
        ],
    )
    html = build_review_html(b, out_path(tmp_path)).read_text("utf-8")
    # 文はどれも escape する（動くスクリプトは、このページの1つだけ）
    assert "<script>alert" not in html and html.count("<script") == 1
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;" in html


def test_page_reads_nothing_outside(tmp_path):
    html = build_review_html(bundle(tmp_path), out_path(tmp_path)).read_text("utf-8")
    assert "http://" not in html and "https://" not in html
    assert "<link" not in html and "@import" not in html and "<script src" not in html
    assert "Content-Security-Policy" in html and "default-src &#x27;none&#x27;" in html
    # 動くのは、音声と台本を合わせて見せる自分の1つのスクリプトだけ（ハッシュで許す）
    import base64
    import hashlib

    from koten.review.page import SYNC_JS

    assert html.count("<script") == 1 and f"<script>{SYNC_JS}</script>" in html
    digest = base64.b64encode(hashlib.sha256(SYNC_JS.encode()).digest()).decode()
    assert f"script-src &#x27;sha256-{digest}&#x27;" in html


def test_video_and_thumbnails_use_relative_paths(tmp_path):
    out = build_review_html(bundle(tmp_path), out_path(tmp_path))
    html = out.read_text("utf-8")
    assert f'<video controls preload="metadata" src="../../media/{EP}/render/{EP}.mp4">' in html
    assert f'<img src="../../media/{EP}/kit/thumb_1.png"' in html
    assert str(tmp_path) not in html  # 絶対パスを入れない
    assert (out.parent / f"../../media/{EP}/render/{EP}.mp4").resolve().exists()


def test_relative_paths_with_japanese_and_spaces_are_encoded(tmp_path):
    root = tmp_path / "SDカード" / "古典 悩み"
    b = bundle(tmp_path)
    thumb = root / "media" / EP / "kit" / "サムネ 1.png"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"")
    b.thumbnails = [str(thumb)]
    b.video = None
    html = build_review_html(b, root / "exports" / EP / "review.html").read_text("utf-8")
    assert f"../../media/{EP}/kit/%E3%82%B5%E3%83%A0%E3%83%8D%201.png" in html
    assert "完成した動画がありません" in html


def test_blockers_are_shown_first_and_loud(tmp_path):
    html = build_review_html(bundle(tmp_path), out_path(tmp_path)).read_text("utf-8")
    assert "承認できません（1件）" in html
    assert 'class="box blockers"' in html
    # 動画より前に出す
    assert html.index("承認できません") < html.index("<video")
    assert "G7 不合格：合計 9分30秒 が 10分〜15分 の外" in html
    assert "いまは承認できません" in html
    assert "G6：人の確認が要る回です" in html  # 注意


def test_no_blockers_says_it_can_be_approved(tmp_path):
    html = build_review_html(bundle(tmp_path, blockers=[]), out_path(tmp_path)).read_text("utf-8")
    assert "承認できない理由はありません" in html
    assert 'class="box blockers"' not in html
    assert "いまは承認できません" not in html


@pytest.mark.parametrize("state", ["script_done", "voiced", "error"])
def test_page_does_not_offer_approval_before_rendered(tmp_path, state):
    # 渡された blockers が空でも、動画ができた回でなければ「承認できる」と出さない
    b = bundle(tmp_path, state=state, blockers=[])
    html = build_review_html(b, out_path(tmp_path)).read_text("utf-8")
    assert "承認できない理由はありません" not in html
    assert "承認できません（1件）" in html
    assert f"状態が {state} です" in html
    assert "いまは承認できません" in html


def test_reviewed_page_says_approved(tmp_path):
    b = bundle(tmp_path, state="reviewed", blockers=[])
    html = build_review_html(b, out_path(tmp_path)).read_text("utf-8")
    assert "承認済みです。" in html
    assert 'class="box blockers"' not in html


def test_commands_are_shown_for_copying(tmp_path):
    html = build_review_html(bundle(tmp_path), out_path(tmp_path)).read_text("utf-8")
    c = commands(EP)
    assert c["approve"] == f"uv run koten episode approve --episode {EP}"
    assert c["reject_script"].startswith(
        f"uv run koten episode reject --episode {EP} --to script_done"
    )
    assert c["reject_plan"].startswith(f"uv run koten episode reject --episode {EP} --to planned")
    for key in ("approve", "reject_script", "reject_plan", "take_text", "take_skip"):
        assert f'<code class="cmd">{c[key].replace(chr(34), "&quot;")}</code>' in html


def test_page_shows_gates_quotes_and_kit(tmp_path):
    html = build_review_html(bundle(tmp_path), out_path(tmp_path)).read_text("utf-8")
    assert "G3 忠実さ" in html and "人の確認へ" in html
    assert "G6 要確認ソース" in html and "要確認の理由：NDL 由来" in html
    assert "G7 尺" in html and "合計 09:30" in html
    assert "G2 ライセンス" in html and "記録なし" in html  # 記録の無いゲートも並べる
    assert "視聴の確かめ" in html and "注意あり" in html
    # 旧仮名を直した一節には印を付ける。直していない一節には付けない
    assert html.count("読みを直した</span>") == 1
    assert "人の己を知らざるを患えず" in html
    assert "01:10" in html and "05:00" in html  # 広告位置の候補（章の切れ目）
    assert "00:00 はじめに" in html
    assert "まだ決まっていません" in html  # 人の一言


@pytest.mark.parametrize(
    ("take", "want"),
    [
        ({"chapter_id": "ch5", "skip": True}, "この回は省きます"),
        ({"chapter_id": "ch5", "text": "私も昇進を逃しました"}, "文字を読み上げます"),
        (
            {"chapter_id": "ch5", "audio_path": "voice/take.wav", "text": "字幕"},
            "自分の声で入れます",
        ),
    ],
)
def test_human_take_states(tmp_path, take, want):
    html = build_review_html(bundle(tmp_path, human_take=take), out_path(tmp_path)).read_text(
        "utf-8"
    )
    assert want in html
    assert "まだ決まっていません" not in html


def test_page_opens_in_browser_and_loads_thumbnail(tmp_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright がありません")
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    b = bundle(tmp_path)
    open(b.thumbnails[0], "wb").write(png)
    out = build_review_html(b, out_path(tmp_path))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(out.as_uri())
                width = page.eval_on_selector(".thumbs img", "img => img.naturalWidth")
                heading = page.inner_text("#blockers h2")
            finally:
                browser.close()
    except Exception as e:  # ブラウザが起動できない環境
        pytest.skip(f"Playwright（Chromium）が使えません：{e}")
    assert width == 1  # 相対パスの画像が、外を止める決まりの中でも読める
    assert "承認できません" in heading


# --- 承認・差し戻し・人の一言 ------------------------------------------------------


def db(tmp_path, state="rendered", take=None):
    conn = connect(tmp_path / "ssd")
    script = {
        "episode_id": EP,
        "chapters": [],
        "human_take": take or {"chapter_id": "ch5", "text": None, "audio_path": None},
    }
    conn.execute(
        "INSERT INTO episodes(episode_id, format, worry_ids, state, script_json) "
        "VALUES (?, 'A', '[\"W0001\"]', ?, ?)",
        (EP, state, json.dumps(script, ensure_ascii=False)),
    )
    conn.commit()
    return conn


def row(conn):
    return conn.execute("SELECT * FROM episodes WHERE episode_id = ?", (EP,)).fetchone()


def test_approve_moves_rendered_to_reviewed(tmp_path):
    conn = db(tmp_path)
    approve(conn, EP, note="読みも画面もよい", blockers=[])
    r = row(conn)
    assert r["state"] == "reviewed"
    assert r["reviewer_note"] == "読みも画面もよい"
    assert "承認" in r["state_msg"]


def test_approve_refuses_with_blockers(tmp_path):
    conn = db(tmp_path)
    with pytest.raises(ReviewError, match="照合で保留が 1 件") as e:
        approve(conn, EP, blockers=["今の本との照合で保留が 1 件", "VOICEVOX のクレジットが空"])
    assert "VOICEVOX のクレジットが空" in str(e.value)
    assert row(conn)["state"] == "rendered"


@pytest.mark.parametrize("state", ["script_done", "voiced", "reviewed", "published"])
def test_approve_only_from_rendered(tmp_path, state):
    conn = db(tmp_path, state=state)
    with pytest.raises(ReviewError):
        approve(conn, EP, blockers=[])
    assert row(conn)["state"] == state


def test_approve_refuses_when_state_changed_meanwhile(tmp_path, monkeypatch):
    # 状態を確かめたあとに別の操作で変わっていたら、reviewed で上書きしない
    import koten.review.actions as actions

    conn = db(tmp_path, state="voiced")
    monkeypatch.setattr(actions, "_row", lambda c, e: {"state": "rendered", "script_json": "{}"})
    with pytest.raises(ReviewError, match="途中で変わった"):
        approve(conn, EP, blockers=[])
    assert row(conn)["state"] == "voiced"


def test_approve_unknown_episode(tmp_path):
    conn = connect(tmp_path / "ssd")
    with pytest.raises(ReviewError, match="という回はありません"):
        approve(conn, EP, blockers=[])


@pytest.mark.parametrize("to_state", ["script_done", "planned"])
def test_reject_moves_back_with_note(tmp_path, to_state):
    conn = db(tmp_path, state="reviewed")
    reject(conn, EP, note="ch2 の読みが違う", to_state=to_state)
    r = row(conn)
    assert r["state"] == to_state
    assert r["reviewer_note"] == "ch2 の読みが違う"
    assert "差し戻し" in r["state_msg"] and "ch2 の読みが違う" in r["state_msg"]


def test_reject_needs_note_and_valid_target(tmp_path):
    conn = db(tmp_path)
    with pytest.raises(ReviewError, match="理由"):
        reject(conn, EP, note="  ")
    with pytest.raises(ReviewError, match="戻し先"):
        reject(conn, EP, note="直す", to_state="voiced")
    assert row(conn)["state"] == "rendered"


@pytest.mark.parametrize(
    ("state", "to_state"),
    [("published", "script_done"), ("discarded", "planned"), ("planned", "script_done")],
)
def test_reject_refuses_wrong_states(tmp_path, state, to_state):
    conn = db(tmp_path, state=state)
    with pytest.raises(ReviewError):
        reject(conn, EP, note="直す", to_state=to_state)
    assert row(conn)["state"] == state


def test_reject_error_only_to_planned(tmp_path):
    # 台本の検査で止まった回（error）の台本を、検査を通さずに script_done にしない
    conn = db(tmp_path, state="error")
    with pytest.raises(ReviewError, match="planned に戻す"):
        reject(conn, EP, note="直す", to_state="script_done")
    assert row(conn)["state"] == "error"
    reject(conn, EP, note="G1 で止まった章を直す", to_state="planned")
    assert row(conn)["state"] == "planned"


def test_set_human_take_text_updates_both_places_and_redoes_voice(tmp_path):
    conn = db(tmp_path, state="rendered")
    take = set_human_take(conn, EP, text="  私も昇進を逃した年がありました。 ")
    assert take == {
        "chapter_id": "ch5",
        "text": "私も昇進を逃した年がありました。",
        "audio_path": None,
        "skip": False,
    }
    r = row(conn)
    assert json.loads(r["human_take"]) == take
    assert json.loads(r["script_json"])["human_take"] == take
    # 動画には入っていない一言なので、音声から作り直す
    assert r["state"] == "script_done"
    assert "作り直す" in r["state_msg"]


def test_set_human_take_skip_keeps_rendered(tmp_path):
    conn = db(tmp_path, state="rendered")
    take = set_human_take(conn, EP, skip=True)
    assert take["skip"] is True and take["text"] is None
    # もともと一言の無い動画なので、作り直さずに承認へ進める
    assert row(conn)["state"] == "rendered"
    approve(conn, EP, blockers=[])
    assert row(conn)["state"] == "reviewed"


def test_set_human_take_skip_after_voiced_take_redoes_voice(tmp_path):
    old = {"chapter_id": "ch5", "text": "前の一言", "audio_path": None, "skip": False}
    conn = db(tmp_path, state="voiced", take=old)
    set_human_take(conn, EP, skip=True)
    assert row(conn)["state"] == "script_done"


def test_set_human_take_same_content_keeps_state(tmp_path):
    old = {"chapter_id": "ch5", "text": "同じ一言", "audio_path": None, "skip": False}
    conn = db(tmp_path, state="rendered", take=old)
    set_human_take(conn, EP, text="同じ一言")
    assert row(conn)["state"] == "rendered"


def test_set_human_take_same_audio_path_redoes_voice(tmp_path):
    # 同じパスでも録り直したかもしれないので、声の wav を入れたら音声から作り直す
    old = {"chapter_id": "ch5", "text": None, "audio_path": "voice/take.wav", "skip": False}
    conn = db(tmp_path, state="rendered", take=old)
    set_human_take(conn, EP, audio_path="voice/take.wav")
    assert row(conn)["state"] == "script_done"


def test_set_human_take_audio_with_subtitle(tmp_path):
    conn = db(tmp_path, state="script_done")
    take = set_human_take(conn, EP, audio_path="voice/take.wav", text="字幕の文")
    assert take["audio_path"] == "voice/take.wav" and take["text"] == "字幕の文"
    assert row(conn)["state"] == "script_done"


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({}, "--skip"),
        ({"text": "一言", "skip": True}, "省く"),
        ({"audio_path": "take.mp3"}, "wav"),
    ],
)
def test_set_human_take_rejects_bad_input(tmp_path, kw, match):
    conn = db(tmp_path)
    with pytest.raises(ReviewError, match=match):
        set_human_take(conn, EP, **kw)
    assert row(conn)["human_take"] is None


@pytest.mark.parametrize("state", ["planned", "reviewed", "published"])
def test_set_human_take_only_before_approval(tmp_path, state):
    conn = db(tmp_path, state=state)
    with pytest.raises(ReviewError):
        set_human_take(conn, EP, text="一言")
    assert row(conn)["state"] == state


def test_take_compares_with_the_voiced_take_after_script_rewrite(tmp_path):
    """koten script の作り直しで台本の一言が空になっても、音声に入っているのは列に残した一言。
    それを省いたら、音声から作り直す（rendered のままにしない）。"""
    conn = db(tmp_path, state="rendered")
    with conn:
        conn.execute(
            "UPDATE episodes SET human_take = ? WHERE episode_id = ?",
            (json.dumps({"chapter_id": "ch5", "text": "前の一言", "audio_path": None}), EP),
        )
    set_human_take(conn, EP, skip=True)
    assert row(conn)["state"] == "script_done"
