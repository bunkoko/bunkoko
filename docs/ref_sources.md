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
- HDD（SD カード）が無くても動きます。そのときは、取り込んだ古い英訳は SSD にあるもの（使える資料の分）を残します。
- `ref show` は verified だけを出します。確かめ中のものも見るときは `--all` を付けます。
- あとで台帳の判定が厳しくなった資料の本文は、作り直す前でも台本からは読み出されません。

## 古い英訳（Standard Ebooks）を取る

いま取れるのは、George Long 訳『自省録』と James Legge 訳『論語』です（config/sources.yaml の standardebooks）。

1. 台帳に書きます（ref_works.csv に1行ずつ、ref_people.csv に原著者と訳者）。没年は自分で確かめて death_source を書きます。

   | ref_id | source_class | kind | title | pub_year | lang | source | holder_allied | credit |
   |---|---|---|---|---|---|---|---|---|
   | R-long-meditations | S2 | translation | Meditations | （下の注） | en | standardebooks | （確かめて） | George Long 訳『自省録』（Standard Ebooks 版） |
   | R-legge-analects | S2 | translation | The Analects | （下の注） | en | standardebooks | （確かめて） | James Legge 訳『論語』（Standard Ebooks 版） |

   - pub_year は「使う版」の年です。Standard Ebooks の版（colophon の年）を書くと、1968年以降の版なので NEEDS_REVIEW になります。Standard Ebooks の手入れが CC0 であることを確かめたら、reviewed_license に PD_JP と理由を書きます。first_pub_year には訳が最初に出た年を書きます。
   - 人：原著者（マルクス・アウレリウス、孔子）と訳者（Long、Legge）。外国の人なので allied に yes か no を書きます。
2. 取って分けます。

   ```sh
   uv run koten ref check
   uv run koten ref fetch
   uv run koten ref build
   uv run koten ref show --passage lunyu:1.1 --all
   ```

   - `ref fetch` は、台帳で使える（PD_JP）と判定された資料だけを取ります。GitHub の raw ファイル（content.opf と本文）を net.py で1秒1件ずつ取り、HDD の raw/standardebooks/ に置きます。分けた本文は HDD の clean/ref/<ref_id>.parquet です。
   - 論語は Legge の章立て（chapter-篇-章）のまま `lunyu:篇.章` にします（499章）。
   - 『自省録』は、Standard Ebooks 版に節の番号が無く、段落と節が一致しません（第11巻は段落55・節39など）。節の番号を作らないよう、`meditations:巻.p段落`（例 `meditations:2.p1`）にします（505段落）。引用（blockquote）とそれをつなぐ短い段落は前の段落にまとめ、巻の終わりの場所の書き添え（「This in Carnuntum.」など）は除きます。節の番号は、番号のある版で人が確かめてから renderings.csv に書きます。
   - 取り込んだ本文の status は imported です。台本の引用には使わず、試訳（M8b）の材料にします。引用するときは、人が確かめて renderings.csv に verified で書きます。
   - 台帳で使えなくなった資料の、取り込んだ本文と原本は、次の `ref build` で消します（R2）。

## 手間の目安

- 資料1冊の権利の確認：30分〜2時間。2〜3割は UNKNOWN のまま残る見込みです。
- 辞書の見出し1つ（画像から書き起こし、読みを付ける）：10〜15分。
- 漢文の1章（白文と、PD の訓読・注1〜2種）：20〜40分。

## 台本で使う

- **昔の辞書の語釈**：悩みの文（一般化した悩み）に見出し語（2字以上）が出てくる、確かめ済み（verified）の語釈があれば、企画（`koten plan`）が「悩みの解像度を上げる」章に1つ付けます（古い辞書から。章の目安に20〜30秒を足す）。
- **時代の解釈**：一節と古典の対応はまだ自動ではないので、人が章に付けます。

  ```sh
  uv run koten ref attach --episode E20261101-01 --chapter ch2 --id I-kanbuntaikei-lunyu-1.1
  ```

  - 付けられるのは、確かめ済み（verified）で、台帳で使える資料のものだけです。外すときは `--remove` を付けます。
  - 台本ができた回に付けるときは `--force` を付け、そのあと `koten script --episode … --force` で作り直します。
- 台本では、本文は「」の中に記号だけを置き、同じ段落に出典の記号を置きます。差し込むと次のようになります。

  > 1891年（明治24年）の『言海』は、「普通」を「（語釈の本文）」と説明しています。

  - 出典の年は、取り込んだ刷りの刊行年です（Standard Ebooks など打ち直した電子版は、訳・著作が最初に出た年 first_pub_year）。1873年以降なら和暦を添えます。
  - 昔の説明は、その時代の説明として紹介します。今の意味・正しい意味とは言いません。
  - 「辞書では」「本来の意味」「語源」は、語釈・解釈の記号と同じ文のときだけ言えます。「本当の意味」「通説」「多くの学者」、今の辞書・訳注の名前（広辞苑など）、「明治の人は」のような言い方は G1 で止めます。
- 判定役（prompts/judge_v3.md）は、語釈・解釈の本文と出典を見て1文ずつ確かめます。語釈・解釈のせいで通らない章は、それを外して書き直します（「視聴の注意」に残ります）。
- `koten script --episode … --show` は、語釈・解釈を使った段落に【資料｜ID】を付け、最後に資料のままの本文・出典とコマ・表記を改めたか・読み上げの読みを並べます。
- NDL の資料の本文を使った回は、G6 で人の確認になります（画面のページ画像と照らし合わせる）。
- 企画のあとで本文が変わった・使えなくなった・確かめ中に戻った語釈・解釈も、人の確認になります。

## まだ無いもの（このあと）

- 古典の一節（論語の章など）を検索の候補にすること。今は、時代の解釈を人が章に付ける
- 試訳（M8b）：取り込んだ英訳（imported）を材料に、AI が下書きし、判定役と人が確かめる
- NDL からの OCR・画像の取得（M8a では人が画像から書き起こす）
- 画面の資料カード（資料のまま・出典・ページ画像）と、読み上げの読みの差し込み（M6）
