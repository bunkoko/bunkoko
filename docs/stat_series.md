# 統計の系列（e-Stat から複数年の数を取る）

段1 の手順です。回で「今と昔の数」を比べるための、年ごとの数の台帳を作ります。
（回の企画・台本・グラフ・説明欄に使うのは 段2 です。ここは台帳づくりまで）

## 系列とは

- 系列 ＝ 1つの調査 × 1つの問い × 1つの集団 × 1つの単位 を、年ごとに並べたもの。
- 例：国民生活基礎調査の「生活が苦しいと感じる世帯の割合」を、年ごとに並べる。
- **数はプログラムが写します**（e-Stat の API か、e-Stat のファイル）。AI は数を打ちません。API の系列は、調査名・作成機関・表の題・単位も、e-Stat のとおりにプログラムが写します。
- Claude が書くのは「どの表のどの分類・どのセルか」と「何の数か（label）」と「調べ方の変わり目（breaks）」だけです。別の AI の見直し役が確かめます。
- あなたが確かめるのは、**回で使った系列を、1系列ずつ、スマホで**。
  - 何の数か（label）
  - 分類（どの集団・どの問いか）
  - 調べ方の変わり目（この年から方法が違う、など）
  - 回で読む文
- 数そのものは確かめません（プログラムが写したものだからです）。

## どこに何があるか

| 場所 | 中身 | 書く人 |
|---|---|---|
| config/stat_series.csv | 系列の台帳（label・単位・調査名・作成機関・選び方・変わり目・確かめの状態） | `koten sources merge`（Claude の下書きの取り込み）・`koten stats fetch`（e-Stat の調査名・作成機関・表の題・単位を写す）・`koten stats hold`（保留）。**手で開いて書かない**（Numbers や Excel で保存すると壊れることがある） |
| config/stat_points.csv | 年ごとの数 | **`koten stats fetch` だけ**（手で書かない） |
| docs/estat/<系列の ID>.json | 表の情報（調査名・作成機関・表題・最終更新日・選んだ分類）。系列ごとに1つ | `koten stats meta`・`stats fetch` |
| HDD の raw/estat | e-Stat から取ったままの応答とファイル（Mac のキャッシュ。Git に入れない） | net.py |
| data/estat-cache | HDD が無いときのキャッシュ（クラウドと、HDD をつないでいない Mac。Git に入れない） | net.py |
| SSD の stat_series・stat_points | 上の CSV から作る表（区切り・確かめの状態・古さを計算） | `koten db init` |

### e-Stat の ID（appId）

- 名前は `KOTEN_ESTAT_APPID`。**Mac の .env にだけ**書きます（.env.example に名前だけあります）。
- チャット・GitHub・ログには貼りません。プログラムも、取得の記録・キャッシュの名前・エラーの文・docs/estat・CSV に出しません。台帳のどの欄に `appId=` があっても、取り込みと `db init` が断ります。
- e-Stat の表を Claude に伝えるときは、API の URL（appId つき）でなく、e-Stat のサイトの表の URL（`https://www.e-stat.go.jp/dbview?sid=…` など）を伝えます。
- `uv run koten doctor` は「設定済み」か「未設定」だけを出します（値は出しません）。
- クラウドには ID がありません。API の系列を取るのは Mac だけです。
- **数を書くのは Mac だけ**です（API もファイルも）。クラウドの Claude は、ファイルの系列を `stats fetch --check`・`--drafts` で確かめるだけにします（書かない）。

### クラウドの連絡先（KOTEN_CONTACT）

- e-Stat のファイルを取るとき（`stats fetch --check`・`--drafts`・`sources page`）は、User-Agent に連絡先が要ります。
- クラウドの Claude は、個人の情報を含まない汎用の連絡先を使います（R10）。値は次のとおりです：

  ```sh
  export KOTEN_CONTACT=https://claude.com/claude-code
  ```

- Mac では、.env の KOTEN_CONTACT（メールアドレスか URL）を使います。

## 下書きの系列の項目

下書きは `docs/research/<日付>-source-drafts.json` の `series` です（統計・研究の下書きと同じファイル。形は src/koten/sources/check.py の先頭）。Claude がファイル名を伝えます。

- どの系列も：`series_id`（英小文字・数字・_）・`themes`・`label`（年と数は入れない）・`unit`・`survey`・`publisher`・`table_title`・`source_kind`（`estat_api` か `estat_file`）・`period_kind`（`年` か `年度`）・`url`（e-Stat のサイトの URL）・`latest_known`・`breaks`・`trend` など・`evidence`・`check_note`。
- API の系列（`estat_api`）：`stats_data_id`（表の番号）と、次のどちらか。
  - `selector_names`：分類の名前 → 項目の名前（同じ名前の項目が複数あるときは `"#符号"`）。
  - `selector`：`{"statsDataId": …, "cdTab": …, "cdCat01": …}` の符号。時間以外の分類ごとに1つ。
  - Mac の `stats meta` が表の情報を取って、名前から符号を決める（または符号を確かめる）。調査名・作成機関・表の題は docs/estat の表の情報と同じにします。
- ファイルの系列（`estat_file`）：`selector.parts` に、ファイルごとの部分を並べます。
  - `statInfId`・`fileKind`：半角の数字の文字（`"000040123456"`）。
  - `sheet`：xlsx のシート名（CSV は `""`）。
  - `values`：数のセル（`"D12:R12"` のように行か列の一方向）。
  - `periods_cells`（年のセル。`values` と同じ列の行の範囲、または同じ行の列の範囲）か、`periods`（`["2021"]` のような 4 桁の年の文字の並び）。
  - `label_cells`（見出しのセルの並び）・`unit_cell`（単位のセル）・`updated`（公開日）は、書くなら文字で（null にしない）。

## 流れ

1. **クラウド（Claude）**：e-Stat のサイトで表やファイルを探し、系列の下書きを `docs/research/<日付>-source-drafts.json` の `series` に書きます。別の AI の見直し役が label と変わり目を確かめます。ファイルの系列は、取る前に確かめられます（書きません）：

   ```sh
   export KOTEN_CONTACT=https://claude.com/claude-code
   uv run koten stats fetch --drafts docs/research/<日付>-source-drafts.json
   ```

2. **Mac（API の系列があるとき）**：分類の名前から符号を決めて、下書きに書き戻します。

   ```sh
   cd /Users/bunkoko/koten
   git pull
   uv run koten stats meta --drafts docs/research/<日付>-source-drafts.json
   git add docs/estat docs/research
   git commit -m "表の情報"
   git push
   ```

   - 名前が表の分類と合わない系列は、赤で候補の名前を出します。Claude に伝えれば下書きを直します。
3. **クラウド（Claude）**：下書きを台帳（config/stat_series.csv）に「AI が確かめた行」として入れ、GitHub に入れます。押した結果がまだ無いので、空の結果 `{}` を渡します。

   ```sh
   git pull
   mkdir -p data && echo '{}' > data/no-taps.json
   uv run koten sources merge --from data/no-taps.json --accept-rest --drafts docs/research/<日付>-source-drafts.json
   git add config/stat_series.csv
   git commit -m "統計の系列の下書きを台帳に入れる"
   git push
   ```

   - 出力の「AI が確かめた行：…・統計の系列 N」の N が、入れた系列の数です。「統計の系列」が出なければ、系列は1つも入っていません。下書きのファイル名と、黄色の「入れなかった：…」の行を見直します。
4. **Mac**：数を取って、DB に読み込みます。

   ```sh
   cd /Users/bunkoko/koten
   git pull
   uv run koten stats fetch
   uv run koten db init
   git add config/stat_points.csv config/stat_series.csv docs/estat
   git commit -m "統計の系列の数"
   git push
   ```

   - 系列ごとに、点の数・最新の年・抜けた年・mark・単位が出ます。
   - 黄色の「…を e-Stat のとおりにする」は、台帳の調査名・作成機関・表の題・単位を、プログラムが e-Stat のとおりに直したという意味です（そのままで大丈夫です）。
   - 赤い行が出たら（`stats fetch` でも `db init` でも）、その行を Claude に伝えます。
5. **回で使ったとき**：統計・研究と同じに、回ごとにスマホで確かめます（下の「回で使ったとき」）。

### 取る前に確かめるだけ（何も書かない）

```sh
uv run koten stats fetch --check
```

- 下書きの系列を確かめるときは `--drafts` を付けます。下書きは台帳に書かれません。

  ```sh
  uv run koten stats fetch --drafts docs/research/<日付>-source-drafts.json
  ```

- クラウドで動かすときは、先に `export KOTEN_CONTACT=https://claude.com/claude-code` をします（R10）。
- 1系列だけ取るときは `--series 系列の ID`（何度でも付けられます）。
- 一覧を見るには（SSD を読みます）：

  ```sh
  uv run koten stats list
  uv run koten stats list --theme 孤独
  uv run koten stats list --stale
  uv run koten stats list --unchecked
  ```

  - `--stale`：その調査に新しい年が出ているのに、台帳が古い系列。
  - `--unchecked`：あなたがまだ確かめていない系列（保留の系列は出しません）。
  - 保留の系列には `[保留]` が付きます。

## 回で使ったとき

- 統計・研究と同じ流れです（docs/sources_check.md の「回で使った行を確かめる」）。

  ```sh
  cd /Users/bunkoko/koten
  uv run koten sources todo --episode latest
  git add docs/checks
  git commit -m "確かめが要る行"
  git push
  ```

- Claude が `sources page --todo` でその回の行だけのページを作り、あなたがスマホで押し、Claude が `sources merge --todo` で取り込みます。
- 系列のカードの見方は docs/sources_check.md の「系列のカード」。
- 一度確かめた系列は、数や名前が変わらないかぎり、ほかの回でもそのまま使えます。
- **今は、回の企画が系列を使う部分がまだありません**（段2）。`sources todo` が回で使った系列を todo に書き出すのも 段2 です。
- 動画の説明欄の出典・グラフ・e-Stat のクレジットの文（API の系列を使った回）も 段2 で入れます。

## 段1 の確かめ（受け入れ）

段1 では、回がまだ無いので、Claude が台帳の系列から確かめのページを作ります。

1. **Mac**：上の流れの 4（数を取って push）までを済ませます。
2. **クラウド（Claude）**：確かめる系列を並べた JSON を作り、その系列だけのページを作って、アーティファクト「統計と研究の確かめ」に載せます。

   ```sh
   git pull
   export KOTEN_CONTACT=https://claude.com/claude-code
   mkdir -p docs/checks
   echo '{"episode_id": "段1", "series": ["API の系列の ID", "ファイルの系列の ID"]}' > docs/checks/stage1.json
   uv run koten sources page --todo docs/checks/stage1.json
   ```

3. **あなた**：スマホで、API の系列のカードとファイルの系列のカードを押します。
4. **クラウド（Claude）**：押した結果を JSON にして取り込み、GitHub に入れます。

   ```sh
   uv run koten sources merge --todo docs/checks/stage1.json --from 押した結果の JSON
   git add config/stat_series.csv docs/checks
   git commit -m "段1 の系列を確かめた"
   git push
   ```

5. **Mac**：取り込んで、確かめが入ったかを見ます。

   ```sh
   cd /Users/bunkoko/koten
   git pull
   uv run koten db init
   uv run koten stats list
   ```

   - 押した系列に「確かめ オーナー」が付けば合格です。

## 決まり

### 選び方は変えない（R21）

- 台帳にある series_id の「選び方」（API なら分類の符号、ファイルならどのセルを読むか）は、あとから変えません。
- 変えたいときは、**新しい series_id で下書きし直します**。古いほうは保留にします（Claude が行います。CSV は手で書きません）：

  ```sh
  uv run koten stats hold --series 古い系列の ID --note "新しい系列の ID に替えた"
  git add config/stat_series.csv
  git commit -m "古い系列を保留にする"
  git push
  ```

- `uv run koten stats fetch` は、保留の系列を飛ばします（「保留なので飛ばす」）。`--series 系列の ID` で名前を挙げたときだけ取ります。
- 保留を外すときは `uv run koten stats hold --series 系列の ID --release` です。

### スマホで「違う」を押した系列は保留にする

- 台帳にある系列のカードで「違う」を押すと、その系列は保留になります（台本で使わず、`stats fetch` も飛ばします）。メモに書いたことは台帳の note に残ります。
- 保留の系列のカードには「保留中」と出ます。「合っている」を押しても保留のままです（外すのは `stats hold --release`）。

### e-Stat のファイルは年ごとに増える（R22・R23）

- 調査によっては、年ごとに別のファイルが出ます。そのときは、同じ series_id に「部分」を足して増やします。
- 許されること：
  - 部分を最後に足す。
  - 既存の部分の `updated`（公開日）・`label_cells`・`unit_cell` を直す。
- すでに数を取った部分は動かせません。数をまだ取っていない部分は、あとの下書きで直したり外したりできます。
- 部分がほかの部分と同じセル・同じ年に当たると、取り込みを断ります（「足した部分が前の部分と重なる」。新しい系列でも同じ）。
- 部分を足す下書きで台帳に入るのは、選び方（selector）・latest_known・breaks・where・note だけです。label などは台帳のまま（あなたが直した言い方を残す。「ラベルは台帳のまま」と出ます）。
- 増やす手順：
  1. **クラウド（Claude）**：最新を取り込んでから、部分を足した下書きを作り、取れるかを確かめ、取り込んで GitHub に入れます。

     ```sh
     git pull
     export KOTEN_CONTACT=https://claude.com/claude-code
     uv run koten stats fetch --drafts docs/research/<日付>-source-drafts.json
     mkdir -p data && echo '{}' > data/no-taps.json
     uv run koten sources merge --from data/no-taps.json --accept-rest --drafts docs/research/<日付>-source-drafts.json
     git add config/stat_series.csv docs/research
     git commit -m "統計の系列に部分を足す"
     git push
     ```

  2. **Mac**：足した部分の数を取って、GitHub に入れます。

     ```sh
     cd /Users/bunkoko/koten
     git pull
     uv run koten stats fetch
     uv run koten db init
     git add config/stat_points.csv config/stat_series.csv docs/estat
     git commit -m "統計の系列の数"
     git push
     ```

- 気をつけること：
  - クラウドの取り込みは、GitHub の config/stat_points.csv（どの部分の数をもう取ったか）を見て、変えてよい部分を決めます。Mac で取った数をまだ push していないうちに取り込むと、古い数で決めてしまいます。**Mac の push が済んでから、クラウドで取り込みます**（同時にしない）。
  - あなたが確かめ済みの系列に部分を足すと、確かめは「AI が確かめた」に戻ります（年が増えたため、もう一度見るため）。
- 「下書きが台帳より古い」と出たら：下書きの部分が、台帳より少ないという意味です。Git の最新を取り込んでから、下書きを台帳に合わせて直します。

## 出るメッセージの意味

| メッセージ | 意味・すること |
|---|---|
| …を e-Stat のとおりにする（黄色） | `stats fetch` が、台帳の調査名・作成機関・表の題・単位を e-Stat のとおりに直した。そのままでよい（確かめ済みの系列は確かめが外れる） |
| オーナーの確かめが外れる | `stats fetch` で、数・名前・単位・表の情報が前の確かめと違った。確かめ済みの系列でも、もう一度確かめが要る |
| 確かめが外れた（`stats list`） | あなたが確かめたあと、中身が変わった系列。「未」（まだ確かめていない）とは別。回で使うなら、また確かめる |
| 数の単位が年ごとに違う | e-Stat の数の単位が年でそろっていない。その系列は取らない。Claude に伝える（表章項目か分類の選び方を見直す） |
| e-Stat の単位 … と unit … が違う | `db init`・取り込みが止まる。台帳の unit が e-Stat の単位と違う（下書きで unit を変えたなど）。Claude が下書きの unit を e-Stat の単位に直す |
| … が表の情報の … と違う（`db init`・取り込み） | 台帳の調査名・作成機関・表の題が、docs/estat の表の情報と違う。Claude が下書きの名前を表の情報（docs/estat/<ID>.json）のとおりに直して取り込む。Mac の `stats fetch` は e-Stat のとおりに写すので、取り直してもよい |
| 前に数があった年が、取り直すと無い／数が1つも取れなかった | 取り直したら、前に数のあった年が消えた。その系列は書かない（前の数・確かめは残る）。表かセルの位置が変わったかもしれないので、赤い行を Claude に伝える |
| HTML（エラーやお知らせのページ）が返った／PDF が返った | e-Stat がファイルの代わりに別のものを返した。前のファイルと数は残る。時間をおいて取り直すか、Claude に伝える |
| 見出しが変わった | 取り直すと、同じ年の見出しのセルの文字が変わった。何の数かが変わったかもしれないので、Claude に伝える |
| 数字を含む印 | 数として読めなかったセル（△1.5・全角の数字など）。数は抜けている。Claude が CSV の版か別のセルを選ぶ |
| 403（「ID が違うか、使えなくなっている」） | appId が違うか、失効している。.env の KOTEN_ESTAT_APPID を見直す。e-Stat が自分のエラーの文を返したときは、その文を読む |
| 該当データなし（STATUS 1） | その選び方の数が無い。selector の符号と表の番号を Claude に見直してもらう（次の実行で取り直す） |
| 保留なので飛ばす | status が 保留 の系列。`--series 系列の ID` で名前を挙げたときだけ取る |
| selector がまだ無い（Mac で uv run koten stats meta --drafts <下書き>） | API の下書きに、分類の符号がまだ入っていない。Mac で `stats meta --drafts` を実行する |
| selector_names も、selector の符号も無い | `stats meta` が表の情報を書けない。Claude が下書きに selector_names か符号を書き足す |
| 台帳の … と選び方が違う。新しい series_id で下書きする | R21。台帳にある ID の選び方は変えられない。新しい series_id にして、古いほうを `stats hold` で保留にする |
| 足した部分が前の部分と重なる | 部分が、ほかの部分と同じセル・同じ年に当たる（新しい系列でも出る）。下書きを直す |
| ラベルは台帳のまま | 部分を足す下書きの label は入れなかった（台帳の言い方を残した） |
| 下書きが台帳より古い | 下書きの部分が台帳より少ない。台帳に合わせて下書きを直す |
| N 系列が失敗。赤の行を Claude に伝える | 赤い行の系列は取れなかった（ほかの系列は書いた）。赤い行を Claude に伝える |

## 取り直し

- 月に1回ほど、または企画をまとめて作る前に、Mac で取り直します。

  ```sh
  cd /Users/bunkoko/koten
  git pull
  uv run koten stats fetch --refresh
  uv run koten db init
  git add config/stat_points.csv config/stat_series.csv docs/estat
  git commit -m "統計の系列の取り直し"
  git push
  ```

- 変わった数は `stats fetch` の表示に出ます（「前から変わった数」「オーナーの確かめが外れる」）。数が変わった系列は、あなたの確かめが自動で外れます。
- 変わったものが無いと、git commit が「nothing to commit」と出ますが、そのままで大丈夫です。
- `koten update` には入れていません（手で行う）。

## 段1 の受け入れ

- Mac で、API の系列 3 つ以上とファイルの系列 3 つ以上を取り、`uv run koten db init` で読める。
- 国民生活基礎調査の系列で、2020 年の抜けが分かる。
- スマホで、API の系列のカードとファイルの系列のカードを1枚ずつ確かめ、取り込める（上の「段1 の確かめ（受け入れ）」）。
- appId の値が、リポジトリ・HDD の取得の記録・キャッシュのどこにも無い（ファイルの名前にも中身にも）。HDD をつないだ Mac で、次をそのまま貼って確かめます（値は画面に出ません）。

```sh
cd /Users/bunkoko/koten
uv run python - <<'EOF'
import os
from pathlib import Path
from koten.config import load_settings
s = load_settings()  # .env も読む
v = os.environ.get("KOTEN_ESTAT_APPID", "").strip()
ok = bool(v)
roots = [("リポジトリ", Path("."))]
if s.hdd_root.exists():
    roots.append(("HDD の取得の記録・キャッシュ", s.hdd_root / "raw" / "estat"))
else:
    print(f"HDD（{s.hdd_root}）：つながっていない。確かめになっていない（HDD をつないでやり直す）")
    ok = False
seen, hits = set(), []
for name, root in roots:
    if not root.exists():
        print(f"{name}（{root}）：まだ無い（探すものが無い）")
        continue
    print(f"{name}（{root}）：探した")
    for p in root.rglob("*"):
        if not p.is_file() or {".git", ".venv"} & set(p.parts) or p.name == ".env":
            continue
        key = p.resolve()
        if key in seen:
            continue
        seen.add(key)
        try:
            if v and (v in str(p) or v.encode() in p.read_bytes()):
                hits.append(str(p).replace(v, "***"))
        except OSError:
            pass
print("ID の字数:", len(v))
print("ID が見つかったファイル:", len(hits))
for h in hits:
    print(" ", h)
print("合格" if ok and not hits else "合格ではない（上の行を見る）")
EOF
```

- 最後に「合格」と出れば合格です。「ID の字数」が 0 なら、.env に ID が無いので確かめになっていません。HDD がつながっていないときも、確かめになっていません。
- 見つかったファイルがあれば、その名前を Claude に伝えます（名前の中の ID は *** に置き換えて出ます。値は貼らないでください）。
