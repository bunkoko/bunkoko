"""カードとサムネイルの画像（docs/m6_design.md・SPEC §8.10・§8.11）。

リポジトリの templates/ の HTML に中身を差し込み、Playwright の Chromium で撮る。

- 差し込みは string.Template（$name）。値はすべて html.escape する（一節・出典は本文のまま）
- 外のフォント・画像・スクリプトは読まない。文字は手元のフォントを font-family で並べ、写真は
  data: で渡す。念のため、ページからの通信はすべて止める
- 長い一節は、文字を段階的に小さくして枠（#fit）に収める。いちばん小さくしても収まらなければ、
  切らずにそのまま撮って、警告を出す
- ブラウザは1回だけ起こして、全部を撮る
"""

from __future__ import annotations

import base64
import html
import re
import string
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from koten.media.model import CardSpec, ThumbSpec

CARD_TEMPLATES = {
    "quote_card": "quote_card.html",
    "title_card": "title_card.html",
    "chart": "chart_card.html",
}
THUMB_TEMPLATES = {"a": "thumb_a.html", "b": "thumb_b.html", "c": "thumb_c.html"}

# 文字の大きさ（--fs）の段。大きい順に試し、収まった最初の大きさで撮る。
# カードは高さ 1080 の画面のときの px（ほかの大きさでは画面ごと拡大・縮小して撮る）。
SIZES = {
    "tate": (72, 66, 60, 54, 48, 44, 40, 36, 33, 30, 27, 24),  # 縦書きの一節
    "yoko": (60, 54, 49, 44, 40, 36, 33, 30, 27, 24),  # 横書きの一節（英訳など）
    "title_card": (112, 100, 88, 78, 68, 60, 52, 46),
    "chart": (52, 48, 44, 40, 36, 32, 28, 25),
    "thumb": (150, 136, 124, 112, 100, 90, 80, 72, 64, 56),
}
DESIGN_HEIGHT = 1080  # カードのテンプレートの基準の高さ
THUMB_SIZE = (1280, 720)

PHOTO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

HELP = (
    "カードを画像にするための Playwright（Chromium）が使えません。"
    "uv sync --extra ml --extra video のあと、"
    "uv run playwright install chromium を一度だけ打ってください"
)

# 枠（#fit）の中身が、はみ出さずに収まっているか
_FITS_JS = """() => {
  const box = document.getElementById('fit');
  const el = box && box.firstElementChild;
  if (!el) return true;
  const b = box.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const w = Math.max(r.width, el.scrollWidth);
  const h = Math.max(r.height, el.scrollHeight);
  return w <= b.width + 1 && h <= b.height + 1;
}"""
_SET_SIZE_JS = "px => document.documentElement.style.setProperty('--fs', px + 'px')"


class CardError(RuntimeError):
    """カードを作れないとき（Playwright・ブラウザが無い、テンプレートが無い、中身が足りない）。"""


def fill(template: str, values: dict[str, Any]) -> str:
    """テンプレートの $name に値を差し込む。値はすべてエスケープする。"""
    safe = {k: html.escape("" if v is None else str(v), quote=True) for k, v in values.items()}
    try:
        return string.Template(template).substitute(safe)
    except (KeyError, ValueError) as e:
        raise CardError(f"テンプレートの差し込みに失敗しました（{e}）") from e


def is_yoko(text: str) -> bool:
    """横書きにする一節か。ラテン文字（英訳など）が日本語の字より多ければ横書きにする。"""
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    cjk = sum(1 for c in text if ord(c) >= 0x3000)
    return latin > cjk


def stat_source(stat: dict[str, Any]) -> str:
    """統計の出典の1行：「出典：調査名（年）」。説明欄と同じ書き方。"""
    survey = str(stat.get("survey") or "").strip()
    year = str(stat.get("year") or "").strip().removesuffix("年")
    if not survey:
        return ""
    return f"出典：{survey}（{year}年）" if year else f"出典：{survey}"


def card_page(spec: CardSpec, template: str) -> tuple[str, tuple[int, ...]]:
    """CardSpec から、差し込み済みの HTML と、試す文字の大きさの段を作る。"""
    if spec.type == "quote_card":
        text = spec.text.strip()
        if not text:
            raise CardError(f"{spec.seg_id}：引用カードの一節が空です")
        mode = "yoko" if is_yoko(text) else "tate"
        values = {
            "mode": mode,
            "text": text,
            "source": spec.source.strip(),
            "label": spec.label.strip(),
            "license": spec.license_note.strip(),
        }
        return fill(template, values), SIZES[mode]
    if spec.type == "title_card":
        title = spec.title.strip()
        values = {"title": title, "mode": "" if title else "empty"}  # 空なら色だけの画像
        return fill(template, values), SIZES["title_card"]
    if spec.type == "chart":
        stat = spec.stat or {}
        value = "" if stat.get("value") is None else str(stat["value"]).strip()
        source = stat_source(stat)
        if not value or not source:
            # 数字と出典のどちらかが無い統計は画面に出さない（数字は DB の値だけ。出典は必ず出す）
            raise CardError(f"{spec.seg_id}：統計のカードに数字か出典（調査名）がありません")
        values = {
            "label": str(stat.get("label") or "").strip(),
            "value": value,
            "unit": str(stat.get("unit") or "").strip(),
            "source": source,
        }
        return fill(template, values), SIZES["chart"]
    raise CardError(f"{spec.seg_id}：カードの種類「{spec.type}」は作れません")


def thumb_page(spec: ThumbSpec, template: str) -> tuple[str, bool]:
    """ThumbSpec から、差し込み済みの HTML を作る。写真が読めたかも返す。"""
    photo = photo_data_uri(Path(spec.photo)) if spec.photo else ""
    values = {
        "words": spec.words.strip(),
        "authors": "・".join(a.strip() for a in spec.authors if a.strip()),
        "photo": photo,
        "bg": "has-photo" if photo else "no-photo",
    }
    return fill(template, values), bool(photo) or not spec.photo


def photo_data_uri(path: Path) -> str:
    """写真を data: URI にする（ページに外のファイルを読ませないため）。

    読めない形式か、ファイルが無ければ空。
    """
    mime = PHOTO_MIME.get(path.suffix.lower())
    if mime is None or not path.is_file():
        return ""
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def safe_name(seg_id: str) -> str:
    """ファイル名に使えない字を _ にする。"""
    return re.sub(r"[^\w.-]", "_", seg_id) or "card"


def _read_template(templates_dir: Path, name: str, cache: dict[str, str]) -> str:
    if name not in cache:
        path = templates_dir / name
        if not path.is_file():
            raise CardError(f"テンプレート {path} がありません")
        cache[name] = path.read_text("utf-8")
    return cache[name]


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


# --- ブラウザ -------------------------------------------------------------------


@contextmanager
def _browser() -> Iterator[Any]:
    """Chromium を1つ起こす。使えなければ CardError（入れ方を案内する）。"""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as e:
        raise CardError(HELP) from e
    try:
        pw = sync_playwright().start()
    except Exception as e:  # Playwright のドライバが起きない
        raise CardError(f"{HELP}（{_first_line(e)}）") from e
    try:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            raise CardError(f"{HELP}（{_first_line(e)}）") from e
        try:
            yield browser
        finally:
            browser.close()
    finally:
        pw.stop()


def _first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0]


def _new_page(browser: Any, width: int, height: int, design_height: int) -> Any:
    """基準の高さで組んだページを、width×height の画像に撮れるようにする。"""
    scale = height / design_height
    page = browser.new_page(
        viewport={"width": round(width / scale), "height": design_height},
        device_scale_factor=scale,
    )
    page.route("**/*", lambda route: route.abort())  # 外へは一切取りに行かない
    return page


def _shoot(page: Any, content: str, sizes: tuple[int, ...]) -> tuple[bytes, bool]:
    """HTML を開き、収まるまで文字を小さくして撮る。収まったかも返す。"""
    page.set_content(content, wait_until="load")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    fitted = False
    for px in sizes:
        page.evaluate(_SET_SIZE_JS, px)
        if page.evaluate(_FITS_JS):
            fitted = True
            break
    return page.screenshot(type="png"), fitted


# --- 外から使う ------------------------------------------------------------------


def render_cards(
    specs: list[CardSpec],
    media: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    templates_dir: Path,
    warnings: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, str]:
    """CardSpec ごとに cards/<seg_id>.png を書き、seg_id → media からの相対パスを返す。

    収まらなかったカードの警告は log に出し、warnings（渡されたとき）にも足す。
    """
    if not specs:
        return {}
    cache: dict[str, str] = {}
    pages = []
    for spec in specs:  # 中身の誤りは、ブラウザを起こす前に見つける
        name = CARD_TEMPLATES.get(spec.type)
        if name is None:
            raise CardError(f"{spec.seg_id}：カードの種類「{spec.type}」は作れません")
        pages.append((spec, *card_page(spec, _read_template(templates_dir, name, cache))))
    out: dict[str, str] = {}
    with _browser() as browser:
        page = _new_page(browser, width, height, DESIGN_HEIGHT)
        for spec, content, sizes in pages:
            png, fitted = _shoot(page, content, sizes)
            rel = f"cards/{safe_name(spec.seg_id)}.png"
            _write(media / rel, png)
            out[spec.seg_id] = rel
            if not fitted:
                _warn(
                    f"{spec.seg_id}：文字をいちばん小さく（{sizes[-1]}px）しても枠に収まりません。"
                    f"切らずに撮りました。{rel} を見て、はみ出していれば一節を短くしてください",
                    warnings,
                    log,
                )
    return out


def render_thumbnails(
    specs: list[ThumbSpec],
    media: Path,
    *,
    templates_dir: Path,
    warnings: list[str] | None = None,
    log: Callable[[str], None] = print,
) -> list[str]:
    """サムネイル（1280×720）を kit/thumb_<n>.png（n は 1 から）に書く。

    media からの相対パスを返す。
    """
    if not specs:
        return []
    cache: dict[str, str] = {}
    pages = []
    for spec in specs:
        name = THUMB_TEMPLATES.get(spec.template)
        if name is None:
            raise CardError(f"サムネイルのテンプレート「{spec.template}」はありません（a・b・c）")
        content, photo_ok = thumb_page(spec, _read_template(templates_dir, name, cache))
        if not photo_ok:
            _warn(
                f"サムネイル {spec.template}：写真 {spec.photo} を読めないので、色の背景にしました"
                "（jpg・png・webp だけ使えます）",
                warnings,
                log,
            )
        pages.append(content)
    width, height = THUMB_SIZE
    out: list[str] = []
    with _browser() as browser:
        page = _new_page(browser, width, height, height)
        for n, content in enumerate(pages, start=1):
            png, fitted = _shoot(page, content, SIZES["thumb"])
            rel = f"kit/thumb_{n}.png"
            _write(media / rel, png)
            out.append(rel)
            if not fitted:
                _warn(
                    f"{rel}：悩みの言葉が長く、いちばん小さい文字でも枠に収まりません",
                    warnings,
                    log,
                )
    return out


def _warn(msg: str, warnings: list[str] | None, log: Callable[[str], None]) -> None:
    if warnings is not None:
        warnings.append(msg)
    log(msg)
