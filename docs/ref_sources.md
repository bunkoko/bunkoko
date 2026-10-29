# 資料の台帳と、時代の解釈・昔の辞書の入れ方（M8a）

- 方針：docs/sources_strategy_plan.md（§11 の直しを優先）と、docs/decisions.md の D-014〜D-019。
- ここに書くのは、オーナーが Mac で資料を1冊ずつ足す手順です。

## 何を入れるか

| 区分 | 中身 | 例 | 入れる CSV |
|---|---|---|---|
| S1 原文 | 古典そのもの | 論語の白文、百人一首 | renderings.csv（role=original） |
| S2 時代の解釈 | 保護期間の切れた訓読・訳・注釈 | 明治の訓読本、Legge・Long の英訳 | renderings.csv（role=kundoku・translation・commentary） |
| S2 昔の辞書 | 保護期間の切れた辞書の語釈 | 明治の国語辞典 | dict_entries.csv |

- 今の本（著作権のある訳・注釈・辞書。S4）は、どの CSV にも書きません。本文を持たず、AI にも渡しません（D-017 が決まるまでは、人が読んで確かめるだけ）。
- 本文は、ページの画像と照らし合わせて人が書き起こしたものだけです。使う所だけを書きます（解釈は800字、語釈は300字まで）。

## 手順

### 1. 資料を台帳に書く（config/ref_works.csv）

1冊（1冊ものでなければ、使う冊）を1行にします。

| 列 | 書くこと |
|---|---|
| ref_id | `R-` で始まる ID（例：`R-genkai-v4-1891`） |
| source_class | S1 か S2 |
| kind | original・dictionary・kundoku・translation・commentary |
| title・volume・edition | 書名・冊・版や刷り |
| pub_year | **取り込む刷りの刊行年**（画面の「○年の『…』」はこの年になる） |
| first_pub_year | その著作が最初に出た年（分かれば。団体名義の判定に使う） |
| publisher・credited_as | 出版者と、表紙・奥付の名義 |
| corporate | 団体の名義だけで出たものは yes |
| lang | ja・zh・en など |
| source | ndl・standardebooks・gutenberg・aozora・archive・owned |
| pid・url | NDL の PID、または取得元の URL |
| ndl_access・ndl_checked_at | NDL の資料は、公開範囲の表示と、それを見た日。使えるのは `internet_pd`（インターネット公開（保護期間満了））だけ |
| holder_allied | 1941年12月8日の権利者（出版社など）が連合国籍か。日本の出版社なら no。空は「不明」として厳しく扱う |
| tradition・based_on | 注の流れ（古注・新注・徂徠 など）と、元にした資料の ref_id（`;` 区切り）。同じ流れの資料が2つ合っても、別々に確かめたことにはならない |
| credit | 画面と説明欄に出す出典表記 |
| reviewed_* | 判定が NEEDS_REVIEW のとき、人が確かめた結果（下の 3） |

### 2. 関わった人を全員書く（config/ref_people.csv）

- 1人1行。著者・編者・編纂・校訂・訓読・訓点・注釈・講述・筆録・監修・序文・解説・増補・訳者・挿絵の全員です（role は author・editor・compiler・reviser・kundoku・kunten・annotator・lecturer・recorder・supervisor・preface・commentary・supplement・translator・illustrator）。
- 奥付と序文・解説を見て書き出し、没年は Web NDL Authorities などで確かめて、death_source に URL を書きます。紀元前は負の数（孔子なら -479）。名前の分からない人は「（不明）」と書き、没年を空にします（判定は UNKNOWN になります）。
- country が「日本」なら連合国籍ではないとします。外国の人は allied に yes・no を書きます（空は「不明」）。
- Standard Ebooks が足した手入れは CC0 なので、人には入れません。元の訳者と原著者を入れます。

### 3. 判定を見る

```sh
uv run koten ref check
```

- 1冊ずつ、使えるか（PD_JP）と理由を出します。書き込みはしません。
- いちばん厳しい結果を採ります：関わった人全員の没年（1968年以降の没者がいれば保護中）、連合国籍か国籍不明で1955年以降の没者（確認へ）、団体名義（確認へ）、1968年以降の版（確認へ）、NDL の公開範囲。
- NEEDS_REVIEW になった資料は、確かめてから ref_works.csv の reviewed_license に PD_JP（使える）か COPYRIGHTED（使えない）と、reviewed_by・reviewed_at・review_note（何を見て決めたか）を書きます。UNKNOWN と COPYRIGHTED は、人の確認では使えるようにできません。データを直して判定し直します。

### 4. 本文を書き起こす

**時代の解釈（config/renderings.csv）**

| 列 | 書くこと |
|---|---|
| rendering_id | `I-` で始まる ID（例：`I-kanbuntaikei-lunyu-1.1`） |
| passage_key・passage_to | 一節の番号（config/canon.yaml の付け方。論語の学而第一の第1章は `lunyu:1.1`）。注が何章かをまとめて扱うときは passage_to に終わりの番号 |
| ref_id・role | 台帳の資料と、役割（original・kundoku・translation・commentary） |
| text | 資料のままの本文 |
| display_text | 旧字を新字に直すなど、表記を改めて画面に出すときだけ |
| reading | 読み上げの読み（かなだけ）。日本語の本文には必須。文語やカタカナ書きは、VOICEVOX がそのままでは読めないため |
| locator | 資料の中の場所。NDL の資料は「コマ12」のようにコマ番号を入れる |
| status | transcribed（書き起こした）か verified（画像と照らし合わせた）。台本で使えるのは verified だけ |
| checked_by・checked_at | verified にした人と日 |

**昔の辞書（config/dict_entries.csv）**

- entry_id は `J-` で始めます。headword は今の表記の見出し語（例：普通）、headword_kana は辞書の見出しのかな、sense は語義番号です。
- そのほかの列は renderings.csv と同じです。

### 5. SSD に入れて読む

```sh
uv run koten ref build
uv run koten ref show --word 普通
uv run koten ref show --passage lunyu:1.1
```

- 1行でもおかしければ、1件も入れ替えません。使えない資料の本文を書いた行があっても止まります（R2）。
- `ref show` は verified だけを出します。確かめ中のものも見るときは `--all` を付けます。
- あとで台帳の判定が厳しくなった資料の本文は、作り直す前でも台本からは読み出されません。

## 手間の目安

- 資料1冊の権利の確認：30分〜2時間。2〜3割は UNKNOWN のまま残る見込みです。
- 辞書の見出し1つ（画像から書き起こし、読みを付ける）：10〜15分。
- 漢文の1章（白文と、PD の訓読・注1〜2種）：20〜40分。

## まだ無いもの（このあと）

- 古い英訳（Standard Ebooks）の取得と、巻・節への分け方
- 台本での差し込み（`{{def}}`・`{{def_src}}`・`{{interp}}`・`{{interp_src}}`）と、G1 の新しい決まり
- NDL からの OCR・画像の取得（M8a では人が画像から書き起こす）
