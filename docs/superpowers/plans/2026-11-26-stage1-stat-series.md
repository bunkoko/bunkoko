# 段1 統計の系列の取り込み Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** e-Stat の API とファイル（CSV・Excel）から、同じ調査・同じ問いの年ごとの数を、プログラムが写して台帳（config/stat_series.csv・stat_points.csv）と SSD に入れ、スマホで系列ごとに確かめられるようにする。

**Architecture:** 系列の定義は Claude が下書きし（クラウド）、数は Mac の `koten stats fetch` が net.py 経由で写す。appId は Mac の .env にだけあり、net.py の secret_params で送るだけで、どこにも残さない。系列は今の stats 表には写さず、別の表（stat_series・stat_points）に入れる（段2 の resolve_key が読む）。確かめは今の sources の流れ（下書き → page → merge → todo）に「series」の種類を足す。

**Tech Stack:** Python 3.12・httpx・sqlite3・typer・pytest・openpyxl（新しく足す）

**Spec:** docs/superpowers/specs/2026-11-26-stats-first-episodes-design.md（§5 が段1。§3 の決まりはどの task にも当たる）

**この計画に入れないもの：** §5.10（説明欄の出典の行・「加工して作成」・API のクレジットを説明欄に入れる・折れ線のカード）と、回で使った統計の記号を系列にまとめる `sources todo` の変更は、台本が系列を使い始める段2 の計画で作る（使う所が段2 にあるため）。

## Global Constraints

- appId の環境変数の名前は `KOTEN_ESTAT_APPID`。値は Mac の .env にだけ置く。マニフェスト・キャッシュの名前・保存した応答・エラーの文・CLI の出力・docs/estat・CSV のどこにも出さない。
- e-Stat への取得は必ず net.py の Fetcher を通す。間隔は settings の `[rate_limit.min_interval_sec]` に `"e-stat.go.jp" = 2.0`。
- API の土台：`https://api.e-stat.go.jp/rest/3.0/app/json`。`lang=J`。取得は `getMetaInfo` と `getStatsData`。`limit=100000`、続きは `RESULT_INF.NEXT_KEY` を `startPosition` に。`metaGetFlg=Y`・`cntGetFlg=N`・`explanationGetFlg=N`・`annotationGetFlg=Y`・`replaceSpChar=0`。
- STATUS：0〜2 は正常（1 は該当データなし。その系列の失敗として扱う）、100 以上はエラー（控えを消して止める）。
- ファイル：`https://www.e-stat.go.jp/stat-search/file-download?statInfId={statInfId}&fileKind={fileKind}`（登録なし）。CSV は utf-8-sig、だめなら cp932。xlsx は openpyxl（read_only=True, data_only=True）。.xls は扱わない。
- 年・年度だけ。月・四半期の時間軸は断る。
- 数は元の文字のまま（`,` を除いて Decimal で読めること）。特殊文字（mark）は数にせず、0 にしない。
- 既存の config/stats.csv・`load_stats(conn, csv_path)`・stats 表・tests/test_worries.py の 53 行は変えない。
- 文書・メッセージ・コメントは日本語（リポジトリの決まり）。結果の報告は「やったこと」「わかったこと／問題」「次にやること」。
- e-Stat の API のクレジットの文（説明欄で使うのは段2。定数だけ今作る）：「このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、サービスの内容は国によって保証されたものではありません。」

## Review Focus

1. JSON で CLASS・VALUE・NOTE・CLASS_OBJ が1件だけのとき、並びでなく1つの dict で来る（XML→JSON の決まり）→ どれも並びとして読めること（Task 4 に test）。
2. 数に `1,234` や `-`・`…`・`x`・`X`・`***`・`*` が来る → value は `1234` 扱いで読める／mark として残り、value_num は NULL、0 にならない（Task 2・4 に test）。
3. 時間の名前が「平成25年」「2013年度」「2013年1月」「2013年1-3月期」→ 和暦は西暦に、年度は年度に、月・期は断る（Task 4 に test）。
4. HTTP 403・接続の失敗のとき、httpx の例外の文に appId 入りの URL が入る → FetchError の文に appId が無い（Task 1 に test）。
5. 取り直して1つの数が変わる → check_sha が変わり、owner_ok が False になる（Task 2・3 に test）。

---

## ファイルの地図

| ファイル | 役目 |
|---|---|
| src/koten/net.py（変更） | secret_params・秘密を消した例外・e-Stat のエラーの見分け |
| src/koten/stats/__init__.py（新） | 空 |
| src/koten/stats/series.py（新） | 系列と点の形、CSV の読み書き、決まりの確かめ、区切り・sha・owner_ok・trend の確かめ |
| src/koten/stats/estat.py（新） | e-Stat の応答の読み方、名前から符号を決める、ページ送り、元の資料との照らし合わせ |
| src/koten/stats/files.py（新） | e-Stat のファイル（CSV・xlsx）から点を読む |
| src/koten/db.py（変更） | stat_series・stat_points の表、`load_series` |
| src/koten/cli.py（変更） | `koten stats meta/fetch/list`、`db init` が系列も読む |
| src/koten/doctor.py（変更） | appId が設定されているか（値は出さない） |
| src/koten/sources/check.py・page.html（変更） | 下書き・ページ・取り込み・回の確かめに「series」 |
| config/stat_series.csv・stat_points.csv（新、見出しだけ） | 台帳 |
| docs/estat/<series_id>.json（新。系列ごとに1つ。R17） | 表の情報（選んだ分類だけ） |
| config/settings.toml・.env.example・pyproject.toml（変更） | 設定・依存 |
| docs/stat_series.md（新）・docs/sources_check.md・docs/decisions.md・CLAUDE.md（変更） | 手順と記録 |

---

### Task 0: e-Stat の規約と API の仕様を読み直して記録する（CLAUDE.md の要確認）

**Files:**
- Modify: `docs/decisions.md`（要確認の表に D-039、末尾に記録）

- [ ] **Step 1:** 次のページを curl で取り（www.e-stat.go.jp はこの環境で開ける）、原文を確かめる：`https://www.e-stat.go.jp/terms-of-use`、`https://www.e-stat.go.jp/api/terms-of-use`、`https://www.e-stat.go.jp/api/api-info/credit`、`https://www.e-stat.go.jp/api/api-dev/faq`、`https://www.e-stat.go.jp/api/api-info/e-stat-manual3-0`。
- [ ] **Step 2:** D-039 の行を書く：商用可（FAQ の答えの原文）、出典の書き方と「〜を加工して作成」（利用規約 1) の原文）、API 規約 第2・3・5・7・8条の原文、クレジットの文、仕様（Global Constraints の引数・STATUS の意味・NEXT_KEY）、読んだ日 2026-11-26。docs/research/2026-11-26-bulk-sources.md を根拠に足す。
- [ ] **Step 3:** Commit：`git commit -m "D-039：e-Stat の規約と API の仕様を読み直した"`

### Task 1: net.py で秘密の引数を送り、どこにも残さない

**Files:**
- Modify: `src/koten/net.py`（Fetcher.fetch・fetch_json、新しい FetchError）
- Test: `tests/test_net.py`（新）

**Interfaces:**
- Produces:
  - `Fetcher.fetch(url, rel_path, *, license=None, refresh=False, secret_params: dict[str, str] | None = None) -> tuple[Path, bool]`。送る URL は `httpx.URL(url).copy_merge_params(secret_params)`。マニフェストの url・rel_path は引数の url（秘密なし）。
  - `Fetcher.fetch_json(url, subdir, *, secret_params=None, error_of: Callable[[object], str | None] | None = None) -> object`。error_of が文を返したら控えを消して `ApiError(f"{url}：{msg}")`。今の MediaWiki の `"error"` の見分けは error_of が None のときだけ（今の呼び出しは変えない）。
  - `class FetchError(RuntimeError)`：httpx の例外（HTTPStatusError を含む）を、秘密の値を `***` に置き換えた文で投げ直す（`from None`）。
  - 保存する本文の中に秘密の値と同じバイト列があれば `b"***"` に置き換えてから sha と保存。

- [ ] **Step 1: Write the failing tests**

```python
SECRET = "abcdef0123456789abcdef0123456789abcdef01"  # 40 字の偽の ID

def test_secret_param_is_sent_but_never_stored(tmp_path):
    seen = []
    def server(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"echo": str(req.url)})
    f = make_fetcher(tmp_path, server)          # MockTransport の Fetcher（per_host_per_sec=0）
    data = f.fetch_json("https://api.e-stat.go.jp/rest/3.0/app/json/getMetaInfo?statsDataId=1",
                        "estat", secret_params={"appId": SECRET})
    assert "appId=" + SECRET in seen[0]
    assert SECRET not in json.dumps(data)
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert SECRET not in p.read_text("utf-8", errors="ignore")
            assert SECRET not in p.name

def test_http_error_text_has_no_secret(tmp_path):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(403, text="forbidden"))
    with pytest.raises(FetchError) as e:
        f.fetch("https://api.e-stat.go.jp/x?y=1", "x.json", secret_params={"appId": SECRET})
    assert SECRET not in str(e.value)

def test_error_of_drops_the_cached_answer(tmp_path):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(200, json={"S": 100}))
    with pytest.raises(ApiError):
        f.fetch_json("https://api.e-stat.go.jp/x", "estat",
                     error_of=lambda d: "認証に失敗" if d["S"] >= 100 else None)
    assert not list((tmp_path / "estat").glob("*.json"))
```

- [ ] **Step 2:** Run `uv run pytest tests/test_net.py -v` → FAIL（secret_params が無い）。
- [ ] **Step 3:** Implement as in Interfaces。今の呼び出し（ingest・facts・ref・sources locate）は引数を変えない。
- [ ] **Step 4:** Run `uv run pytest tests/test_net.py tests/test_ingest_aozora.py tests/test_facts_draft.py tests/test_sources_locate.py -v` → PASS。
- [ ] **Step 5:** Commit：`git commit -m "net.py：秘密の引数（e-Stat の appId）を送るだけで、控え・ログ・エラーに残さない"`

### Task 2: 系列と点の形（src/koten/stats/series.py）

**Files:**
- Create: `src/koten/stats/__init__.py`、`src/koten/stats/series.py`、`config/stat_series.csv`（見出しだけ）、`config/stat_points.csv`（見出しだけ）
- Test: `tests/test_stat_series.py`

**Interfaces:**
- Produces:
  - `SERIES_COLUMNS = ["series_id","themes","label","unit","survey","publisher","source_kind","period_kind","selector","table_title","url","latest_known","breaks","trend","trend_from","trend_to","trend_note","status","checked_by","checked_at","checked_sha","where","note"]`
  - `POINTS_COLUMNS = ["series_id","period","value","mark","part","src_label","src_unit","fetched_at","src_sha"]`
  - `SERIES_FILE = "stat_series.csv"`、`POINTS_FILE = "stat_points.csv"`、`META_DIR = "docs/estat"`（REPO_ROOT から）
  - `@dataclass(frozen=True) class Break: before: str; note: str`
  - `@dataclass(frozen=True) class Series`：列と同じ名前の属性。themes は `tuple[str, ...]`、selector は `dict`、breaks は `tuple[Break, ...]`。
  - `@dataclass(frozen=True) class Point`：POINTS_COLUMNS と同じ名前の属性（全部 str）。
  - `class SeriesError(ValueError)`
  - `read_series_rows(config_dir) -> list[dict[str,str]]`、`read_point_rows(config_dir) -> list[dict[str,str]]`（無ければ []）
  - `series_problems(series_rows, point_rows, *, themes: set[str], legacy_ids: set[str], blocked: dict[str, list[str]], metas: dict[str, dict]) -> list[str]`（「何行目：何が」の文。空なら読める）
  - `parse_series(rows) -> list[Series]`、`parse_points(rows) -> list[Point]`（problems が空の前提）
  - `value_num(p: Point) -> Decimal | None`
  - `period_label(period: str, period_kind: str) -> str`（"2004" と "年度" → "2004年度"）
  - `segments(s: Series, points: list[Point]) -> dict[str, int]`（period → 区切りの番号。0 から）
  - `comparable(s, points, a: str, b: str) -> tuple[bool, str]`
  - `verify_trend(s, points) -> tuple[bool, str]`
  - `check_sha(s: Series, points: list[Point], meta: dict | None) -> str`（16 桁の16進）
  - `owner_ok(s: Series, sha: str) -> bool`（checked_by が HUMAN_CHECKER かつ checked_sha == sha）
  - `is_stale(s, points) -> bool`（最新の数のある年 < latest_known）
  - `write_points(config_dir, series_ids: set[str], new: list[Point]) -> None`（その系列の行だけ入れ替え、(series_id, period) の順で書く）
  - `load_meta(meta_dir: Path, stats_data_id: str) -> dict | None`
  - `parse_year(text: str) -> tuple[str, str]`：「2013年」「2013年度」「平成25年」「平成25年度」「令和元年」（元号は明治・大正・昭和・平成・令和、元年は1） → (YYYY, "年"|"年度")。「月」「期」「四半期」を含めば SeriesError（v1 は年・年度だけ）。読めなければ SeriesError。Task 4・5 が使う。

決まり（series_problems が返す）：
- series_id は `[a-z0-9][a-z0-9_]*`、重複なし、stats.csv の stat_id と同じ名前は不可。
- themes は `;` 区切り 1〜3 個、themes.yaml の語。
- label・unit・survey・publisher・url・selector は空にしない。url は http(s)。
- label に年を入れない：`(?:19|20)\d{2}`・`(?:明治|大正|昭和|平成|令和)\s*[0-9０-９元]+` に当たれば問題。
- label・table_title・survey が除外カテゴリの言葉（keyword_block）に当たれば問題。
- source_kind は estat_api・estat_file、period_kind は 年・年度。
- selector の形：estat_api は `statsDataId` と、`cd` で始まる鍵が1つ以上（値は1つの符号。`,` を含まない）。estat_file は `parts` が1つ以上で、各 part に statInfId・fileKind・sheet（CSV は ""）・values、periods_cells か periods のどちらか。
- breaks は `[{"before": "YYYY", "note": "…"}]`（note は空にしない）。
- trend は空か 増加・減少。書いたら trend_from・trend_to（YYYY）・trend_note も要る。
- status は空か 保留。checked_by は空・AI_CHECKER・HUMAN_CHECKER。checked_by を書いたら checked_at（YYYY-MM-DD）。
- 点：series_id が系列にある、period は YYYY、(series_id, period) が重複しない、value と mark はどちらか一方だけ、value は `,` を除いて Decimal で読める。

区切り（segments）：period の順に見て、breaks の before 以上に入ったところ、または src_label が両方空でなく前の点と違うところで番号を1つ進める。

check_sha：`json.dumps([s.label, s.unit, s.selector, meta_names, sorted((p.period, p.value, p.mark, p.src_label) for p in points), [(b.before, b.note) for b in s.breaks]], ensure_ascii=False, sort_keys=True)` の sha256 の頭16桁。meta_names は meta があれば `[meta["title"], meta["chosen"]]`、無ければ None。

verify_trend：trend が空なら (False, "trend なし")。trend_from〜trend_to の数のある点が2つ以上、同じ区切り、増加なら厳しく増え続け・減少なら厳しく減り続けるときだけ (True, "")。抜けた年は理由の文に並べる（失敗にはしない）。

- [ ] **Step 1: Write the failing tests**（主なもの）

```python
def test_mark_is_never_zero():
    p = point(value="", mark="-")
    assert value_num(p) is None
    assert value_num(point(value="1,234")) == Decimal("1234")

def test_label_with_a_year_is_refused(env):
    probs = series_problems([row(label="2020年の就業率")], [], **env)
    assert any("label" in x and "年" in x for x in probs)

def test_series_id_must_not_collide_with_stats_csv(env):
    probs = series_problems([row(series_id="anzen_r7_amount")], [], **{**env, "legacy_ids": {"anzen_r7_amount"}})
    assert probs

def test_value_and_mark_are_exclusive(env):
    assert series_problems([row()], [prow(value="1", mark="-")], **env)

def test_break_starts_a_new_segment():
    s = series(breaks=(Break("2020", "郵送に変わった"),))
    pts = [point(period=y) for y in ("2018", "2019", "2021")]
    seg = segments(s, pts)
    assert seg == {"2018": 0, "2019": 0, "2021": 1}
    assert comparable(s, pts, "2018", "2021")[0] is False
    assert comparable(s, pts, "2018", "2019")[0] is True

def test_label_drift_in_files_is_a_break():
    pts = [point(period="2021", src_label="強い不安"), point(period="2022", src_label="強い不安やストレス")]
    assert segments(series(), pts) == {"2021": 0, "2022": 1}

def test_check_sha_changes_when_a_value_changes_and_owner_check_lapses():
    s = series(checked_by=HUMAN_CHECKER)
    a = check_sha(s, [point(value="10")], None)
    s2 = replace(s, checked_sha=a)
    assert owner_ok(s2, a)
    b = check_sha(s2, [point(value="11")], None)
    assert a != b and not owner_ok(s2, b)

def test_trend_needs_strict_monotone_in_one_segment():
    s = series(trend="増加", trend_from="2015", trend_to="2020", trend_note="同じ設問")
    assert verify_trend(s, pts_values({"2015": "1", "2018": "2", "2020": "3"}))[0]
    assert not verify_trend(s, pts_values({"2015": "1", "2018": "1", "2020": "3"}))[0]

def test_write_points_replaces_only_the_fetched_series(tmp_path):
    ...  # 2 系列のうち1つだけ入れ替え、もう1つの行がそのまま・並びが (series_id, period)
```

- [ ] **Step 2:** Run `uv run pytest tests/test_stat_series.py -v` → FAIL（モジュールが無い）。
- [ ] **Step 3:** Implement the module as in Interfaces。config/stat_series.csv・stat_points.csv は見出しの1行だけで作る。
- [ ] **Step 4:** Run `uv run pytest tests/test_stat_series.py -v` → PASS。
- [ ] **Step 5:** Commit：`git commit -m "統計の系列と点の形（stat_series.csv・stat_points.csv）と決まり"`

### Task 3: SSD に系列を入れる（db.py・db init）

**Files:**
- Modify: `src/koten/db.py`（SCHEMA に2表、SCHEMA_VERSION を 9 に、`load_series`）、`src/koten/cli.py`（`db_init`）
- Test: `tests/test_stat_series.py`（読み込みの test を足す）

**Interfaces:**
- Consumes: Task 2 の全部。`koten.worries.master.load_blocked_keywords`、config/themes.yaml、config/stats.csv の stat_id。
- Produces:
  - 表 `stat_series`（SERIES_COLUMNS ＋ `check_sha TEXT`・`owner_ok INTEGER`・`latest_period TEXT`・`stale INTEGER`・`trend_ok INTEGER`。themes・selector・breaks は JSON の文字）
  - 表 `stat_points`（POINTS_COLUMNS ＋ `value_num REAL`・`segment INTEGER`・`period_label TEXT`、PRIMARY KEY (series_id, period)）
  - `load_series(conn, config_dir: Path, meta_dir: Path) -> tuple[int, int]`：問題があれば `StatsError` で1件も書かない。両表を入れ替える。trend_ok は verify_trend、owner_ok は check_sha と比べて。
  - `db init` の表示：今の1行に続けて「統計の系列 N 件・年ごとの数 M 件を読み込みました（config/stat_series.csv・stat_points.csv）」。どちらかが問題なら赤で出して終わる（今の stats と同じ）。

- [ ] **Step 1: Write the failing tests**

```python
def test_load_series_is_all_or_nothing(tmp_conn, cfg):
    write(cfg, good_series + [bad_row])
    with pytest.raises(StatsError):
        load_series(tmp_conn, cfg, meta_dir)
    assert tmp_conn.execute("SELECT count(*) FROM stat_series").fetchone()[0] == 0

def test_loaded_points_carry_segment_value_num_and_label(tmp_conn, cfg):
    load_series(tmp_conn, cfg, meta_dir)
    r = tmp_conn.execute("SELECT * FROM stat_points WHERE period='2021'").fetchone()
    assert r["segment"] == 1 and r["value_num"] is None and r["period_label"] == "2021年"

def test_owner_ok_lapses_after_a_refetch(tmp_conn, cfg):
    # checked_sha を今の sha にして読み込む → owner_ok 1。点の数を1つ変えて読み直す → 0
```

- [ ] **Step 2:** Run → FAIL。
- [ ] **Step 3:** Implement。`connect` の `executescript(SCHEMA)` に2表を足す（古い DB には IF NOT EXISTS で足される）。
- [ ] **Step 4:** Run `uv run pytest tests/test_stat_series.py tests/test_worries.py -v` → PASS（53 行の test も通る）。
- [ ] **Step 5:** Commit：`git commit -m "SSD に統計の系列（stat_series・stat_points）を入れる。db init が読む"`

### Task 4: e-Stat の API の応答を読む（src/koten/stats/estat.py）

**Files:**
- Create: `src/koten/stats/estat.py`
- Test: `tests/test_estat.py`（応答は仕様書の形をまねた dict を test の中で作る）

**Interfaces:**
- Consumes: Task 1 の `Fetcher.fetch_json(..., secret_params, error_of)`、Task 2 の Series・Point。
- Produces:
  - `API_BASE = "https://api.e-stat.go.jp/rest/3.0/app/json"`、`APP_ID_ENV = "KOTEN_ESTAT_APPID"`、`ESTAT_API_CREDIT`（Global Constraints の文そのまま）
  - `class EstatError(RuntimeError)`、`class EstatNoData(EstatError)`
  - `as_list(x) -> list`（None → []、dict → [dict]、list → list）
  - `status_error(data: object) -> str | None`（`GET_META_INFO` か `GET_STATS_DATA` の `RESULT.STATUS` が 100 以上なら ERROR_MSG の文。fetch_json の error_of に渡す）
  - `@dataclass class ClassItem: code: str; name: str; level: str; unit: str`
  - `@dataclass class ClassObj: id: str; name: str; items: list[ClassItem]`
  - `@dataclass class TableMeta: stats_data_id, stat_code, stat_name, gov_org_code, gov_org, statistics_name, title, title_no, cycle, survey_date, open_date, updated_date: str; classes: list[ClassObj]`
  - `parse_meta(data: dict) -> TableMeta`（`GET_META_INFO.METADATA_INF` か `GET_STATS_DATA.STATISTICAL_DATA` の TABLE_INF・CLASS_INF。属性は `@id`・`@code`・`@name`・`@level`・`@unit`・`@no`、値は `$`。TITLE は文字か {"@no","$"}）
  - `resolve_selector(meta: TableMeta, names: dict[str, str]) -> dict[str, str]`：names は「分類の名前（CLASS_OBJ の @name）→ 項目の名前（CLASS の @name）」。時間（id "time"）以外の分類ごとに1つの符号を決め、`{"statsDataId": …, "cdTab": …, "cdCat01": …, "cdArea": …}` を返す（鍵は `"cd" + id の頭を大文字`：tab→cdTab、cat01→cdCat01、area→cdArea）。項目が1つだけの分類は名前が無くてもそれを使う。名前が無い・見つからない・2つ当たるときは、その分類の項目の名前を（30 個まで）並べた EstatError。名前は全角半角と空白を揃えて比べる（unicodedata.normalize("NFKC") と空白の除去）。
  - `meta_record(meta: TableMeta, selector: dict) -> dict`：docs/estat/<series_id>.json（R17） に書く形。`{"stats_data_id","stat_code","stat_name","gov_org","statistics_name","title","title_no","cycle","survey_date","open_date","updated_date","chosen": [{"class": 分類名, "item": 項目名, "code": 符号, "unit": 単位}], "times": [{"code","name"}], "fetched_at"}`。
  - `period_of(code: str, name: str) -> tuple[str, str]`：名前を series.parse_year で読み（SeriesError は EstatError にして投げ直す）、符号の頭4桁の年と合わなければ EstatError。
  - `parse_values(data: dict, selector: dict) -> tuple[list[tuple[str, str, str, str]], str | None]`：((period, value, mark, unit) の並び, NEXT_KEY)。VALUE の `$` が数として読めなければ mark にし value は ""。selector の符号と違う `@tab`・`@cat01`・`@area` の行があれば EstatError（1つの問いに決まっていない）。同じ年が2つあれば EstatError。
  - `cross_check(s: Series, meta: TableMeta, units: set[str]) -> list[str]`：unit（`％`→`%` に揃える）・survey == stat_name・publisher == gov_org・table_title == title。違えば何が違うかの文。
  - `class EstatClient: __init__(self, fetcher: Fetcher, app_id: str)`、`meta(self, stats_data_id: str) -> TableMeta`、`points(self, s: Series) -> tuple[list[Point], TableMeta]`（ページを NEXT_KEY で続け、fetched_at と src_sha を付ける。STATUS 1 は EstatNoData）。URL は `f"{API_BASE}/getStatsData?" + urlencode(公開の引数)`、appId は secret_params だけ。

- [ ] **Step 1: Write the failing tests**

```python
def test_single_item_lists_come_as_dicts_and_still_parse():
    m = parse_meta(meta_json(classes=[cls("cat01", "性別", one_item("1", "総数"))]))
    assert m.classes[0].items[0].name == "総数"

def test_resolve_by_names_and_auto_fill_single_item_classes():
    m = parse_meta(meta_json(classes=[cls("tab","表章項目",[item("01","割合")]),
                                       cls("cat01","年齢",[item("1","総数"), item("2","20～29歳")]),
                                       cls("area","全国",[item("00000","全国")]),
                                       cls("time","時間軸(年次)",[item("2013000000","2013年")])]))
    assert resolve_selector(m, {"年齢": "20〜29歳"}) == {"statsDataId": m.stats_data_id, "cdTab": "01", "cdCat01": "2", "cdArea": "00000"}

def test_ambiguous_or_missing_name_lists_candidates():
    with pytest.raises(EstatError, match="総数"):
        resolve_selector(m, {})            # 年齢の分類に名前が無い

def test_periods_wareki_fiscal_and_monthly():
    assert period_of("2013000000", "平成25年") == ("2013", "年")
    assert period_of("2013100000", "2013年度") == ("2013", "年度")
    with pytest.raises(EstatError):
        period_of("2013000101", "2013年1月")

def test_marks_and_commas():
    vals, nxt = parse_values(data_json(values=[v("2013","1,234"), v("2014","-"), v("2015","***")]), sel)
    assert vals == [("2013","1234","",""), ("2014","","-",""), ("2015","","***","")] and nxt is None

def test_status_100_drops_cache_and_status_1_is_no_data(tmp_path): ...
def test_next_key_pages_are_joined(tmp_path): ...        # 2 ページの応答を MockTransport で返す
def test_cross_check_reports_unit_survey_publisher_title(): ...
def test_app_id_never_in_files_or_errors(tmp_path): ...  # Task 1 と同じ偽の ID で client.points を回す
```

- [ ] **Step 2:** Run `uv run pytest tests/test_estat.py -v` → FAIL。
- [ ] **Step 3:** Implement。
- [ ] **Step 4:** Run → PASS。
- [ ] **Step 5:** Commit：`git commit -m "e-Stat の API の応答を読む（名前から符号を決める・ページ送り・元の資料と照らす）"`

### Task 5: e-Stat のファイルから点を読む（src/koten/stats/files.py）

**Files:**
- Create: `src/koten/stats/files.py`
- Modify: `pyproject.toml`（dependencies に `"openpyxl>=3.1"`。`uv add openpyxl`）
- Test: `tests/test_stat_files.py`（CSV は cp932・utf-8-sig で書き、xlsx は openpyxl で作る）

**Interfaces:**
- Consumes: Task 1 の Fetcher.fetch、Task 2 の Series・Point。
- Produces:
  - `FILE_URL = "https://www.e-stat.go.jp/stat-search/file-download?statInfId={sid}&fileKind={kind}"`
  - `read_grid(path: Path, sheet: str) -> list[list[str]]`：xlsx は openpyxl（read_only=True, data_only=True）でそのシート、CSV（sheet ""）は utf-8-sig → cp932 の順に試す。セルは文字にし、数は元の表示に近い文字（整数なら "12"、小数はそのまま）。
  - `cells(grid, ref: str) -> list[str]`：`"D12:R12"` や `"A3"` を読む（行か列の一方向の範囲だけ。2次元は SeriesError）。
  - `file_points(s: Series, fetch: Callable[[str, str], Path]) -> list[Point]`：part ごとにファイルを取り、values と periods（periods_cells を period_of の西暦・和暦の読み方で YYYY に。または periods）を並べる。数でないセルは mark。src_label は label_cells の文字を「／」でつないだもの、src_unit は unit_cell。part の番号を part に。年が part どうしで重なれば SeriesError。
  - 年の読み方は Task 2 の `series.parse_year`（estat.period_of と同じ規則）。

- [ ] **Step 1: Write the failing tests**

```python
def test_csv_cp932_and_utf8sig(tmp_path): ...            # 両方で同じ点
def test_xlsx_row_range_with_marks(tmp_path):
    pts = file_points(series_xlsx(values="C5:E5", periods_cells="C3:E3"), fetch_local(xlsx))
    assert [(p.period, p.value, p.mark) for p in pts] == [("2021","39.5",""), ("2022","","-"), ("2023","40.1","")]
def test_two_parts_with_different_label_text_make_a_break(tmp_path):
    pts = file_points(series_two_parts(), fetch_local(f2021, f2022))
    assert segments(series_two_parts(), pts) == {"2021": 0, "2022": 1}
def test_overlapping_years_between_parts_is_refused(tmp_path): ...
```

- [ ] **Step 2:** Run → FAIL。
- [ ] **Step 3:** Implement。
- [ ] **Step 4:** Run `uv run pytest tests/test_stat_files.py -v` → PASS。
- [ ] **Step 5:** Commit：`git commit -m "e-Stat のファイル（CSV・xlsx）から年ごとの数を読む"`

### Task 6: `koten stats` のコマンド、設定、doctor

**Files:**
- Modify: `src/koten/cli.py`（`stats_app`）、`src/koten/doctor.py`、`config/settings.toml`、`.env.example`
- Create: `docs/estat/.gitkeep`
- Test: `tests/test_stats_cli.py`、`tests/test_cli_doctor.py`（足す）

**Interfaces:**
- Consumes: Task 2〜5。今の `_settings`・`_make_fetcher`・`REPO_ROOT`。
- Produces:
  - settings：`[rate_limit.min_interval_sec]` に `"e-stat.go.jp" = 2.0`。新しい `[estat]`：`api_base = "https://api.e-stat.go.jp/rest/3.0/app/json"`、`lang = "J"`。
  - .env.example：`KOTEN_ESTAT_APPID=`（値は書かない）。
  - キャッシュの場所：HDD があれば `<hdd>/raw/estat`、無ければ `data/estat-cache`（.gitignore 済みの data/）。関数 `_estat_cache(s) -> Path`。
  - `koten stats meta --drafts PATH`（Mac。appId が要る）：下書きの series のうち source_kind が estat_api で `selector_names` があるものについて、meta を取り、resolve_selector の結果を下書きの `selector` に書き戻し、`docs/estat/<series_id>.json（R17。系列ごと）` に meta_record を書く。名前が合わない系列は赤で理由（候補の名前）を出し、ほかは続ける。
  - `koten stats fetch [--series ID]... [--refresh] [--check] [--drafts PATH]`：
    - 対象は、--drafts があれば下書きの series、無ければ台帳の系列（--series で絞る）。
    - API の系列は KOTEN_ESTAT_APPID が無ければ「この環境には e-Stat の ID が無いので飛ばす」と出して飛ばす。ファイルの系列はどこでも。
    - 系列ごとに1行：点の数・最新の年・抜けた年・mark・区切り・単位・cross_check の違い・前の stat_points.csv から変わった数（「オーナーの確かめが外れる」）。
    - --check のときは何も書かない。そうでなければ write_points と docs/estat を書き、最後に「uv run koten db init」と「git add config/stat_points.csv docs/estat」を出す。
    - stat_points.csv が 5MB を超えたら黄色で知らせる。
  - `koten stats list [--theme T] [--stale] [--unchecked]`（SSD を読む）：series_id・label・最新の年・点の数・owner_ok・stale。
  - doctor：`check_estat_app_id(env) -> Check`。設定済みなら OK「設定済み（40 字）」、無ければ WARN「未設定（Mac の .env に KOTEN_ESTAT_APPID を書く）」。値は出さない。run_checks に足す。

- [ ] **Step 1: Write the failing tests**

```python
def test_fetch_skips_api_series_without_app_id(cli_env, monkeypatch):
    monkeypatch.delenv("KOTEN_ESTAT_APPID", raising=False)
    r = runner.invoke(app, ["stats", "fetch", "--check", "--settings", cli_env.settings])
    assert "e-Stat の ID が無い" in r.output and r.exit_code == 0

def test_fetch_writes_points_and_never_prints_the_id(cli_env, monkeypatch, mock_estat):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    r = runner.invoke(app, ["stats", "fetch", "--settings", cli_env.settings])
    assert SECRET not in r.output
    assert "2020" in r.output                      # 抜けた年が出る（国民生活基礎調査の形の偽の応答）
    assert read_point_rows(cli_env.config)          # 書いた

def test_doctor_reports_app_id_without_value(monkeypatch):
    monkeypatch.setenv("KOTEN_ESTAT_APPID", SECRET)
    c = check_estat_app_id(os.environ)
    assert c.status == OK and SECRET not in c.detail
```

（mock_estat：CLI の中の Fetcher に MockTransport を渡せるよう、`_make_fetcher` に `client=None` の引数を足すか、test で `koten.cli._make_fetcher` を monkeypatch する。builder が選ぶ）

- [ ] **Step 2:** Run → FAIL。
- [ ] **Step 3:** Implement。
- [ ] **Step 4:** Run `uv run pytest tests/test_stats_cli.py tests/test_cli_doctor.py -v` → PASS。
- [ ] **Step 5:** Commit：`git commit -m "koten stats meta・fetch・list と、doctor の e-Stat の ID の確かめ"`

### Task 7: 確かめのページと台帳への取り込みに「series」を足す

**Files:**
- Modify: `src/koten/sources/check.py`、`src/koten/sources/page.html`、`src/koten/cli.py`（`sources page` の表示に系列の数）
- Test: `tests/test_sources.py`（足す）

**Interfaces:**
- Consumes: Task 2 の read_*・series_problems・check_sha・segments・period_label・load_meta、Task 5 の read_grid（ファイルの抜き出し）。
- Produces:
  - 下書きの JSON に `"series": [{SERIES_COLUMNS の列（status・checked_* を除く）, "selector_names": {…}, "evidence": "…", "check_note": "…"}]`。`read_drafts` は series が無ければ [] にする。`collect_drafts` は series_id で後のファイルを使う。
  - `series_key(d: dict, points: list[Point], meta: dict | None) -> str`：`_key("r", check_sha(...))`（数が無い下書きのうちに押しても、数が入ると鍵が変わり押し直しになる）。
  - page_items の series の項目：`{"k","kind":"series","id","theme","label","unit","survey","publisher","table_title","url","source_kind","period_kind","line"（最新の点の文：format_stat と同じ形。段2 で比べる文が足される）,"meta"（docs/estat の TABLE_INF の項目）,"chosen"（分類名：項目名の並び）,"points": [{"period","label","value","mark","segment","brk"}],"breaks","excerpt"（ファイルの系列：数の行の前後の行の表。{"rows": [[...]], "hl_row": i, "hl_cols": [...], "label_rows": [...]}。無ければ null）,"trend","trend_from","trend_to","trend_note"（下書きが出し、verify_trend が通るときだけ）,"evidence","check_note","f": {"label": …}}`。
  - page.html：`cardSeries(it)`。並べるもの：読む文、元の資料の言葉（meta・chosen またはファイルの見出し）、年ごとの数の表（抜け・mark・区切りに印）、ファイルの抜き出し（数のセルを赤、見出しの行を黄色）、元の表へのリンク（dbview か file-download）、「数はプログラムが e-Stat から写した。確かめるのは、何の数か・分類・変わり目・読む文」の1行。数は直せない。直せるのは label だけ（fix.label）。trend の印は trend があるときだけ。answer() は `{k, kind:"series", v, at, note, fix:{label}, trend}` を残す。
  - merge_checks：
    - series の行は config/stat_series.csv に書く（SERIES_COLUMNS の順）。ok → checked_by HUMAN_CHECKER・checked_at・checked_sha（label の直しを当てたあとの check_sha）。trend は ok で trend の印があり verify_trend が通るときだけ残す。accept_rest で押していない行 → checked_by AI_CHECKER（人が確かめた行は上書きしない）。ng → 入れない（rejected）。
    - 書く前に series_problems で全部を確かめ、問題があれば stats・findings も含めてどれも書かない（今と同じ）。
    - MergeResult に `series_added`・`series_ai` を足し、CLI がそれを出す（「統計の系列を SSD に入れる：uv run koten db init」）。
  - todo_drafts：todo の `"series": [ID]` を台帳（stat_series.csv・stat_points.csv・docs/estat）から作り、下書きの evidence を足す。台帳に無い ID は今と同じく止める。

- [ ] **Step 1: Write the failing tests**

```python
def test_series_card_shows_machine_values_and_only_label_is_editable(config_dir): ...
def test_merge_ok_series_records_owner_and_sha(config_dir):
    res = merge_checks(drafts_with_series, {key: {"v": "ok", "at": AT}}, config_dir)
    row = read_series_rows(config_dir)[0]
    assert row["checked_by"] == HUMAN_CHECKER and row["checked_sha"] == check_sha(...)
def test_accept_rest_series_is_ai_and_keeps_human_rows(config_dir): ...
def test_series_trend_kept_only_when_ticked_and_verified(config_dir): ...
def test_broken_series_writes_nothing_anywhere(config_dir): ...
def test_todo_series_comes_from_the_ledger(config_dir): ...
def test_draft_key_changes_once_points_exist(config_dir): ...
```

- [ ] **Step 2:** Run → FAIL。
- [ ] **Step 3:** Implement。
- [ ] **Step 4:** Run `uv run pytest tests/test_sources.py -v` → PASS。ページの見た目は、偽の下書きで `koten sources page` を作り、Playwright でスマホの幅（390px）の画像を撮って確かめる（deviceScaleFactor 1）。
- [ ] **Step 5:** Commit：`git commit -m "確かめのページと取り込みに統計の系列（series）を足す"`

### Task 8: 文書・記録と、全体の確かめ

**Files:**
- Create: `docs/stat_series.md`（手順：Claude の下書き → Mac の meta → クラウドの merge → Mac の fetch・db init → 回で使ったら確かめ。コマンドは `--episode latest` で書く）
- Modify: `docs/sources_check.md`（series のカード）、`docs/decisions.md`（状況の表に「統計から考える回（形式 S）段1 統計の系列」の行と受け入れの条件、日付の記録）、`CLAUDE.md`（よく使うコマンドに `koten stats meta/fetch/list`）、`docs/SPEC.md` §9（統計の取り込みに e-Stat の系列）・§11（.env に KOTEN_ESTAT_APPID）

- [ ] **Step 1:** 文書を書く。受け入れの条件は設計 §5.11 のとおり。
- [ ] **Step 2:** Run `uv run pytest -q` → 全部 PASS（今は 1003 passed, 1 skipped。それより減らない）。Run `uv run ruff check .` → All checks passed。
- [ ] **Step 3:** appId が残っていないことを確かめる：Run `git grep -n "KOTEN_ESTAT_APPID=" -- ':!.env.example'` → 何も出ない。
- [ ] **Step 4:** Commit と push：`git commit -m "段1 統計の系列：手順と記録"` → `git push -u origin claude/koten-pipeline-m0-m1`。
- [ ] **Step 5:** Claude が最初の系列の下書きを作る（段1 の受け入れ用。W0007 の「仕事の量」のテーマから、API の系列 3 つ以上・ファイルの系列 3 つ以上。国民生活基礎調査の生活意識の系列を1つ入れる）。別の AI の見直し役が確かめる。下書きを push し、オーナーに Mac のコマンドを渡す（`uv run koten stats meta --drafts …` → push → Claude の merge → `uv run koten stats fetch` → `uv run koten db init`）。スマホの確かめは系列のカードを API とファイルで1枚ずつ。
