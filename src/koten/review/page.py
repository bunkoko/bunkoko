"""確認画面（exports/<回>/review.html）を作る（SPEC §8.12）。

- 1枚の HTML で、外のフォント・画像・スクリプトを読まない（Content-Security-Policy でも止める）
- 動画とサムネイルは、review.html の置き場所からの相対パスで指す（HDD の場所が変わっても開ける）
- 台本・一節・出典などの文は、どれも html.escape してから入れる
- このページからは DB を変えない。承認・差し戻しは、ページに出したコマンドをコピーして端末で打つ
"""

from __future__ import annotations

import os
import shlex
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from koten.media.model import ReviewBundle
from koten.publish_kit.kit import DESCRIPTION_MAX

GATES = ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
GATE_NAMES = {
    "G1": "差し込み",
    "G2": "ライセンス",
    "G3": "忠実さ",
    "G4": "再使用間隔",
    "G5": "安全",
    "G6": "要確認ソース",
    "G7": "尺",
}
STATE_NAMES = {
    "yet": "まだ",
    "planned": "企画まで",
    "script_done": "台本まで",
    "voiced": "音声まで",
    "rendered": "動画まで（確認待ち）",
    "reviewed": "承認済み",
    "published": "公開済み",
    "error": "エラー",
    "discarded": "取り消し",
}
DESCRIPTION_LIMIT = DESCRIPTION_MAX  # YouTube の説明欄の上限（D-036。説明欄を作る側と同じ値）
# 承認できる状態（koten.review.actions.approve と同じ）
APPROVABLE = "rendered"

# 外を読まない：画像・動画は同じ場所（file: の相対パス）と data: だけ。スクリプトは動かさない
CSP = (
    "default-src 'none'; img-src 'self' file: data:; media-src 'self' file:; "
    "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
)

STYLE = """
body { font-family: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Noto Sans JP",
  sans-serif; margin: 0 auto; max-width: 1100px; padding: 16px 20px 80px; line-height: 1.7;
  color: #222; background: #fafafa; }
h1 { font-size: 1.5em; margin: 0.2em 0; }
h2 { font-size: 1.2em; border-bottom: 2px solid #ccc; margin-top: 2em; padding-bottom: 2px; }
.meta { color: #555; }
.box { border-radius: 8px; padding: 12px 18px; margin: 16px 0; }
.blockers { background: #fde8e8; border: 4px solid #c62828; color: #7f0000; }
.blockers h2 { color: #b71c1c; border: none; margin: 0; font-size: 1.4em; }
.blockers li { font-size: 1.1em; font-weight: bold; }
.ok { background: #e8f5e9; border: 2px solid #2e7d32; color: #1b5e20; font-weight: bold; }
.warnings { background: #fff8e1; border: 2px solid #f9a825; }
.warnings h2 { border: none; margin: 0; }
video { width: 100%; max-width: 960px; background: #000; }
.thumbs img { width: 320px; height: 180px; object-fit: cover; margin: 4px; border: 1px solid #999; }
pre { white-space: pre-wrap; word-break: break-word; background: #fff; border: 1px solid #ddd;
  padding: 10px 14px; border-radius: 6px; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { border: 1px solid #ccc; padding: 6px 8px; vertical-align: top; text-align: left; }
th { background: #eee; white-space: nowrap; }
.quote { font-family: "Hiragino Mincho ProN", "Yu Mincho", "Noto Serif JP", serif; }
.fixed { background: #fff3e0; }
.tag { display: inline-block; font-size: 0.85em; padding: 0 6px; border-radius: 4px;
  background: #ef6c00; color: #fff; margin-left: 4px; }
.pass { color: #1b5e20; font-weight: bold; }
.fail { color: #b71c1c; font-weight: bold; }
.hold { color: #e65100; font-weight: bold; }
.none { color: #777; }
.cmd { display: block; user-select: all; -webkit-user-select: all; font-family: Menlo, Consolas,
  monospace; background: #263238; color: #eceff1; padding: 8px 12px; border-radius: 6px;
  margin: 4px 0 12px; white-space: pre-wrap; word-break: break-all; }
.stop { color: #b71c1c; font-weight: bold; }
.small { font-size: 0.9em; color: #555; }
"""


def commands(episode_id: str) -> dict[str, str]:
    """確認画面に出すコマンド（コピーして端末で打つ）。CLI の koten episode と同じ形。"""
    e = shlex.quote(episode_id)
    head = "uv run koten episode"
    return {
        "approve": f"{head} approve --episode {e}",
        "reject_script": f'{head} reject --episode {e} --to script_done --note "直すところ"',
        "reject_plan": f'{head} reject --episode {e} --to planned --note "直すところ"',
        "take_text": f'{head} take --episode {e} --text "人の一言"',
        "take_audio": f"{head} take --episode {e} --audio voice/take.wav",
        "take_skip": f"{head} take --episode {e} --skip",
    }


def build_review_html(bundle: ReviewBundle, out_path: Path) -> Path:
    """確認画面を out_path に書き、そのパスを返す。"""
    base = out_path.parent
    parts = [
        _header(bundle),
        _status(bundle),
        _section("動画", _video(bundle.video, base)),
        _section("サムネイル", _thumbs(bundle.thumbnails, base)),
        _section("台本の全文", _script(bundle.script_lines)),
        _section("一節の原文と読み上げ", _quotes(bundle.quotes)),
        _section("出典とライセンス", _sources(bundle.sources)),
        _section("ゲートの結果（G1〜G7）", _gates(bundle.gates)),
        _section("説明欄", _description(bundle.description)),
        _section("チャプター", _chapters(bundle.chapters)),
        _section("広告位置の候補", _ad_breaks(bundle.ad_breaks)),
        _section("人の一言", _human_take(bundle)),
        _section("承認・差し戻しのコマンド", _commands(bundle)),
    ]
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_e(CSP)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(bundle.episode_id)} の確認</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n" + "\n".join(parts) + "\n</body>\n</html>\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    tmp.write_text(html, "utf-8")
    tmp.replace(out_path)
    return out_path


# --- 部分 ---------------------------------------------------------------------


def _e(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _section(title: str, body: str) -> str:
    return f"<section>\n<h2>{_e(title)}</h2>\n{body}\n</section>"


def _rel_url(target: str, base: Path) -> str:
    """target（絶対パス）を、base（review.html のあるフォルダ）からの相対 URL にする。"""
    rel = os.path.relpath(os.path.abspath(target), os.path.abspath(base))
    return quote(Path(rel).as_posix())


def _clock(sec: float) -> str:
    total = int(sec)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _header(b: ReviewBundle) -> str:
    state = STATE_NAMES.get(b.state, b.state)
    return (
        f"<header>\n<h1>{_e(b.title) or '（タイトルなし）'}</h1>\n"
        f'<div class="meta">回：{_e(b.episode_id)}　状態：{_e(state)}（{_e(b.state)}）</div>\n'
        '<div class="small">このページは読むだけです。DB は変えません。'
        "承認・差し戻しは、いちばん下のコマンドをコピーして端末で打ってください。</div>\n"
        "</header>"
    )


def _stops(b: ReviewBundle) -> list[str]:
    """承認できない理由。blockers が空でも、動画ができた回（rendered）でなければ止める。"""
    if b.blockers or b.state in (APPROVABLE, "reviewed"):
        return list(b.blockers)
    return [f"状態が {b.state} です。承認できるのは、動画ができた回（{APPROVABLE}）だけです"]


def _status(b: ReviewBundle) -> str:
    out = []
    stops = _stops(b)
    if stops:
        items = "".join(f"<li>{_e(x)}</li>" for x in stops)
        out.append(
            '<div class="box blockers" id="blockers">\n'
            f"<h2>承認できません（{len(stops)}件）</h2>\n"
            "<p>次の理由を直してから、もう一度確認画面を作ってください。</p>\n"
            f"<ul>{items}</ul>\n</div>"
        )
    elif b.state == "reviewed":
        out.append('<div class="box ok">承認済みです。</div>')
    else:
        out.append(
            '<div class="box ok">承認できない理由はありません。'
            "動画と中身を見て、よければ承認してください。</div>"
        )
    if b.warnings:
        items = "".join(f"<li>{_e(x)}</li>" for x in b.warnings)
        out.append(
            '<div class="box warnings">\n'
            f"<h2>注意（{len(b.warnings)}件。承認は止めません）</h2>\n"
            f"<ul>{items}</ul>\n</div>"
        )
    return "\n".join(out)


def _video(video: str | None, base: Path) -> str:
    if not video:
        return '<p class="stop">完成した動画がありません（koten render）。</p>'
    missing = "" if Path(video).exists() else '<p class="stop">動画のファイルが見つかりません。</p>'
    url = _e(_rel_url(video, base))
    return (
        f'{missing}<video controls preload="metadata" src="{url}"></video>\n'
        f'<p class="small">ファイル：<a href="{url}">{_e(Path(video).name)}</a>'
        "（読み違い・間・画面の切り替えを、音を出して確かめる）</p>"
    )


def _thumbs(thumbs: list[str], base: Path) -> str:
    if not thumbs:
        return '<p class="none">サムネイルがありません（koten kit）。</p>'
    imgs = "\n".join(
        f'<a href="{_e(_rel_url(t, base))}"><img src="{_e(_rel_url(t, base))}" '
        f'alt="{_e(Path(t).name)}"></a>'
        for t in thumbs
    )
    return f'<div class="thumbs">\n{imgs}\n</div>'


def _script(lines: list[str]) -> str:
    if not lines:
        return '<p class="none">台本がありません。</p>'
    return f"<pre>{_e(chr(10).join(lines))}</pre>"


def _flat(text: str) -> str:
    return "".join((text or "").split())


def _quotes(quotes: list[dict[str, str]]) -> str:
    if not quotes:
        return '<p class="none">一節がありません。</p>'
    rows = []
    for q in quotes:
        display, reading = q.get("display", ""), q.get("reading", "")
        fixed = _flat(display) != _flat(reading)
        tag = '<span class="tag">読みを直した</span>' if fixed else ""
        row = '<tr class="fixed">' if fixed else "<tr>"
        rows.append(
            f"{row}<td>{_e(q.get('seg_id'))}{tag}</td>"
            f'<td class="quote">{_e(display)}</td>'
            f"<td>{_e(reading)}</td>"
            f"<td>{_e(q.get('source'))}</td>"
            f"<td>{_e(q.get('license'))}</td></tr>"
        )
    return (
        '<p class="small">画面には原文、読み上げには右の文を使います。「読みを直した」行は、'
        "動画の音を聞いて読み違いが無いかを確かめてください。直すときは config/readings.csv に"
        "段落の ID（key）と読み（reading）を書き、音声から作り直します。</p>\n"
        "<table>\n<tr><th>段落</th><th>原文（画面）</th><th>読み上げ</th><th>出典</th>"
        "<th>ライセンス</th></tr>\n" + "\n".join(rows) + "\n</table>"
    )


def _sources(sources: list[dict[str, str]]) -> str:
    if not sources:
        return '<p class="none">出典がありません。</p>'
    rows = "\n".join(
        f"<tr><td>{_e(s.get('kind'))}</td><td>{_e(s.get('text'))}</td>"
        f"<td>{_e(s.get('license'))}</td></tr>"
        for s in sources
    )
    return f"<table>\n<tr><th>種類</th><th>出典</th><th>ライセンス</th></tr>\n{rows}\n</table>"


def _gate_mark(gate: str, v: Any) -> tuple[str, str]:
    """ゲートの結果の言葉と、色の名前（koten.script.gates.summarize と同じ言い方）。"""
    if not isinstance(v, dict):
        return "記録なし", "none"
    if gate == "G3" and v.get("needs_human"):
        return "人の確認へ", "hold"
    if gate == "G6" and v.get("review_required"):
        return "要確認", "hold"
    if gate == "viewing":
        return ("注意あり", "hold") if v.get("issues") else ("問題なし", "pass")
    if v.get("ok") is False:
        return "不合格", "fail"
    if v.get("ok") is True:
        return "合格", "pass"
    return "記録なし", "none"


def _gate_row(gate: str, label: str, v: Any) -> str:
    mark, cls = _gate_mark(gate, v)
    notes: list[str] = []
    if isinstance(v, dict):
        notes += [str(x) for x in v.get("issues") or []]
        notes += [f"要確認の理由：{x}" for x in v.get("reasons") or []]
        if gate == "G7" and isinstance(v.get("total_sec"), int | float):
            notes.append(f"合計 {_clock(v['total_sec'])}")
    body = "".join(f"<li>{_e(n)}</li>" for n in notes)
    return (
        f'<tr><th>{_e(label)}</th><td class="{cls}">{_e(mark)}</td>'
        f"<td>{'<ul>' + body + '</ul>' if body else ''}</td></tr>"
    )


def _gates(gates: dict[str, Any]) -> str:
    rows = [_gate_row(g, f"{g} {GATE_NAMES[g]}", gates.get(g)) for g in GATES]
    if "viewing" in gates:
        # ゲートとは別の「視聴の確かめ」（回を止めない注意）
        rows.append(_gate_row("viewing", "視聴の確かめ", gates["viewing"]))
    return (
        "<table>\n<tr><th>ゲート</th><th>結果</th><th>中身</th></tr>\n"
        + "\n".join(rows)
        + "\n</table>"
    )


def _description(text: str) -> str:
    if not text:
        return '<p class="none">説明欄がまだありません（koten kit）。</p>'
    n = len(text)
    over = (
        f'<p class="stop">上限 {DESCRIPTION_LIMIT}字を超えています。</p>'
        if n > DESCRIPTION_LIMIT
        else ""
    )
    return f'{over}<p class="small">{n}字</p>\n<pre>{_e(text)}</pre>'


def _chapters(chapters: list[str]) -> str:
    if not chapters:
        return (
            '<p class="none">チャプターはありません（YouTube の条件：00:00 から・3つ以上・'
            "各10秒以上を満たさないときも空です）。</p>"
        )
    return f"<pre>{_e(chr(10).join(chapters))}</pre>"


def _ad_breaks(ad_breaks: list[float]) -> str:
    if not ad_breaks:
        return '<p class="none">候補がありません。</p>'
    items = "".join(f"<li>{_e(_clock(t))}</li>" for t in ad_breaks)
    return (
        f"<ul>{items}</ul>\n"
        '<p class="small">章の切れ目です。広告の位置は YouTube Studio で手で設定します。</p>'
    )


def _human_take(b: ReviewBundle) -> str:
    take = b.human_take or {}
    c = commands(b.episode_id)
    where = f"（{_e(take.get('chapter_id'))} の最後）" if take.get("chapter_id") else ""
    if take.get("skip"):
        now = f"この回は省きます{where}。"
    elif take.get("audio_path"):
        now = f"自分の声で入れます{where}：{_e(take['audio_path'])}"
        if take.get("text"):
            now += f"<br>字幕：「{_e(take['text'])}」"
    elif take.get("text"):
        now = f"文字を読み上げます{where}：「{_e(take['text'])}」"
    else:
        now = f'<span class="stop">まだ決まっていません{where}。</span>'
    return (
        f"<p>{now}</p>\n"
        '<p class="small">決める・変えるときは次のどれか（音声や動画を作ったあとに中身を変えると、'
        "音声から作り直しになります）：</p>\n"
        f'<code class="cmd">{_e(c["take_text"])}</code>\n'
        f'<code class="cmd">{_e(c["take_audio"])}</code>\n'
        f'<code class="cmd">{_e(c["take_skip"])}</code>'
    )


def _commands(b: ReviewBundle) -> str:
    c = commands(b.episode_id)
    stops = _stops(b)
    if stops:
        stop = (
            f'<p class="stop">いまは承認できません（<a href="#blockers">上の理由 '
            f"{len(stops)}件</a>を先に直す）。</p>\n"
        )
    elif b.state == "reviewed":
        stop = '<p class="small">承認済みです（もう一度は承認できません）。</p>\n'
    else:
        stop = ""
    return (
        '<p class="small">コマンドをクリックすると全体が選ばれます。'
        "コピーして端末で打ってください。このページからは DB を変えません。</p>\n"
        f"<h3>承認する</h3>\n{stop}"
        f'<code class="cmd">{_e(c["approve"])}</code>\n'
        '<p class="small">メモを残すときは、末尾に <code>--note &quot;気づいたこと&quot;</code> '
        "を足します。</p>\n"
        "<h3>差し戻す（「直すところ」を、何を直すかに書き換えてから打つ）</h3>\n"
        "<p>台本はそのままで、音声から作り直す：</p>\n"
        f'<code class="cmd">{_e(c["reject_script"])}</code>\n'
        "<p>企画はそのままで、台本から書き直す（koten script）：</p>\n"
        f'<code class="cmd">{_e(c["reject_plan"])}</code>'
    )
