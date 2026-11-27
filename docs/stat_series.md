# 統計の系列（e-Stat から複数年の数を取る）

段1 の手順です。回で「今と昔の数」を比べるための、年ごとの数の台帳を作ります。
（回の企画・台本・グラフ・説明欄に使うのは 段2 です。ここは台帳づくりまで）

## 系列とは

- 系列 ＝ 1つの調査 × 1つの問い × 1つの集団 × 1つの単位 を、年ごとに並べたもの。
- 例：国民生活基礎調査の「生活が苦しいと感じる世帯の割合」を、年ごとに並べる。
- **数はプログラムが写します**（e-Stat の API か、e-Stat のファイル）。AI は数を打ちません。
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
| config/stat_series.csv | 系列の台帳（label・単位・調査名・作成機関・選び方・変わり目・確かめの状態） | `koten sources merge`（Claude の下書き → 取り込み） |
| config/stat_points.csv | 年ごとの数 | **`koten stats fetch` だけ**（手で書かない） |
| docs/estat/<系列の ID>.json | 表の情報（調査名・作成機関・表題・最終更新日・選んだ分類）。系列ごとに1つ | `koten stats meta`・`stats fetch` |
| HDD の raw/estat | e-Stat から取ったままの応答とファイル（Mac のキャッシュ。Git に入れない） | net.py |
| data/estat-cache | 同じキャッシュのクラウド側（Git に入れない） | net.py |
| SSD の stat_series・stat_points | 上の CSV から作る表（区切り・確かめの状態・古さを計算） | `koten db init` |

### e-Stat の ID（appId）

- 名前は `KOTEN_ESTAT_APPID`。**Mac の .env にだけ**書きます（.env.example に名前だけあります）。
- チャット・GitHub・ログには貼りません。プログラムも、取得の記録・キャッシュの名前・エラーの文・docs/estat・CSV に出しません。
- `uv run koten doctor` は「設定済み」か「未設定」だけを出します（値は出しません）。
- クラウドには ID がありません。API の系列を取るのは Mac だけです。ファイルの系列はどこでも取れます。

## 流れ

1. **クラウド（Claude）**：e-Stat のサイトで表やファイルを探し、系列の下書きを作ります。下書きは `docs/research/2026-11-27-series-drafts.json` のような名前です（日付は日によって変わります。Claude がファイル名を伝えます）。別の AI の見直し役が label と変わり目を確かめます。
2. **Mac（API の系列があるとき）**：分類の名前から符号を決めて、下書きに書き戻します。

   ```sh
   cd /Users/bunkoko/koten
   git pull
   uv run koten stats meta --drafts docs/research/2026-11-27-series-drafts.json
   git add docs/estat docs/research
   git commit -m "表の情報"
   git push
   ```

   - 名前が表の分類と合わない系列は、赤で候補の名前を出します。Claude に伝えれば下書きを直します。
3. **クラウド（Claude）**：`sources merge --accept-rest` で、下書きを台帳（config/stat_series.csv）に「AI が確かめた行」として入れます。
4. **Mac**：数を取って、DB に読み込みます。

   ```sh
   cd /Users/bunkoko/koten
   git pull
   uv run koten stats fetch
   uv run koten db init
   git add config/stat_points.csv docs/estat
   git commit -m "統計の系列の数"
   git push
   ```

   - 系列ごとに、点の数・最新の年・抜けた年・mark・単位が出ます。
5. **回で使ったとき**：統計・研究と同じに、回ごとにスマホで確かめます（下の「回で使ったとき」）。

### 取る前に確かめるだけ（何も書かない）

```sh
uv run koten stats fetch --check
```

- 下書きの系列を確かめるときは `--drafts` を付けます。下書きは台帳に書かれません。

  ```sh
  uv run koten stats fetch --drafts docs/research/2026-11-27-series-drafts.json
  ```

- 1系列だけ取るときは `--series 系列の ID`（何度でも付けられます）。
- 一覧を見るには（SSD を読みます）：

  ```sh
  uv run koten stats list
  uv run koten stats list --theme 孤独
  uv run koten stats list --stale
  uv run koten stats list --unchecked
  ```

  - `--stale`：その調査に新しい年が出ているのに、台帳が古い系列。
  - `--unchecked`：あなたがまだ確かめていない系列。

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

## 決まり

### 選び方は変えない（R21）

- 台帳にある series_id の「選び方」（API なら分類の符号、ファイルならどのセルを読むか）は、あとから変えません。
- 変えたいときは、**新しい series_id で下書きし直します**。古いほうは、config/stat_series.csv の status 列に `保留` と書きます。
- `uv run koten stats fetch` は、保留の系列を飛ばします（「保留なので飛ばす」）。`--series 系列の ID` で名前を挙げたときだけ取ります。

### e-Stat のファイルは年ごとに増える（R22・R23）

- 調査によっては、年ごとに別のファイルが出ます。そのときは、同じ series_id に「部分」を足して増やします。
- 許されること：
  - 部分を最後に足す。
  - 既存の部分の `updated`（公開日）・`label_cells`・`unit_cell` を直す。
- すでに数を取った部分は動かせません。数をまだ取っていない部分は、あとの下書きで直したり外したりできます。
- 足した部分が前の部分と同じセル・同じ年に当たると、取り込みを断ります（「足した部分が前の部分と重なる」）。
- 増やす下書きの手順：

  ```sh
  cd /Users/bunkoko/koten
  git pull
  uv run koten stats fetch --drafts docs/research/2026-11-27-series-drafts.json
  ```

  - これで足した部分が読めるかを、Mac で先に確かめます。
  - そのあと Claude が `sources merge --accept-rest` を行い、Mac で `uv run koten stats fetch`（数を取る）→ `uv run koten db init` → push です。
- 気をつけること：
  - 増やす下書きを取り込む前に、Claude は **必ず `git pull`** します。
  - **クラウドの取り込みと、Mac の `stats fetch` は同時にしません**（同じ CSV を別々に書き換えて、片方が消えるため）。
  - あなたが確かめ済みの系列に部分を足すと、確かめは「AI が確かめた」に戻ります（年が増えたため、もう一度見るため）。
- 「下書きが台帳より古い」と出たら：下書きの部分が、台帳より少ないという意味です。Git の最新を取り込んでから、下書きを台帳に合わせて直します。

## 出るメッセージの意味

| メッセージ | 意味・すること |
|---|---|
| オーナーの確かめが外れる | `stats fetch` で、数・名前・表の情報が前の確かめと違った。確かめ済みの系列でも、もう一度確かめが要る。何が変わったかは git diff で見える |
| 確かめが外れた（`stats list`） | あなたが確かめたあと、中身が変わった系列。「未」（まだ確かめていない）とは別。回で使うなら、また確かめる |
| 403（「ID が違うか、使えなくなっている」） | appId が違うか、失効している。.env の KOTEN_ESTAT_APPID を見直す。e-Stat が自分のエラーの文を返したときは、その文を読む |
| 保留なので飛ばす | status が 保留 の系列。`--series 系列の ID` で名前を挙げたときだけ取る |
| selector がまだ無い（Mac で uv run koten stats meta） | API の下書きに、分類の符号がまだ入っていない。Mac で `stats meta` を実行する |
| 台帳の … と選び方が違う。新しい series_id で下書きする | R21。台帳にある ID の選び方は変えられない。新しい series_id にして、古いほうを 保留 にする |
| 足した部分が前の部分と重なる | 増やした部分が、ほかの部分と同じセル・同じ年に当たる。下書きを直す |
| 下書きが台帳より古い | 下書きの部分が台帳より少ない。台帳に合わせて下書きを直す |

## 取り直し

- 月に1回ほど、または企画をまとめて作る前に、Mac で取り直します。

  ```sh
  cd /Users/bunkoko/koten
  git pull
  uv run koten stats fetch --refresh
  uv run koten db init
  git diff config/stat_points.csv
  ```

- 変わった数は git diff に出ます。数が変わった系列は、あなたの確かめが自動で外れます。
- `koten update` には入れていません（手で行う）。

## 段1 の受け入れ

- Mac で、API の系列 3 つ以上とファイルの系列 3 つ以上を取り、`uv run koten db init` で読める。
- 国民生活基礎調査の系列で、2020 年の抜けが分かる。
- スマホで、API の系列のカードとファイルの系列のカードを1枚ずつ確かめ、取り込める。
- appId が、リポジトリ・HDD の取得の記録・キャッシュ・ログのどこにも無い。Mac で確かめるには：

  ```sh
  cd /Users/bunkoko/koten
  git grep -n "KOTEN_ESTAT_APPID=" -- ':!.env.example'
  ```

  - 何も出なければ合格。キャッシュ・取得の記録は、.env の ID の値で grep します（値をチャットに貼らないでください）。
