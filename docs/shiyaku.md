# 古典の一節の訳の作り方（M8b・D-037）

論語・自省録・方丈記のような古典の一節を動画で使うときの、日本語の作り方と確かめ方です。

## 決まり

- 一節の日本語は、次の順で決まります（コードが選びます）。
  1. 保護期間の切れた日本語訳（人が画像で確かめて renderings.csv に verified で書いたもの）があれば、その訳を出典つきで使います。AI の訳は作りません（D-014）。
  2. 無ければ、AI が訳を下書きし、確かめてから使います。
- 画面と説明欄の表示はコードが付けます（台本の AI には書かせません）。

| 何から訳したか | 種類 | 表示 |
|---|---|---|
| 外国語の原文（論語の白文など。人が影印から起こしたもの） | 試訳（shiyaku） | 「試訳」（R5） |
| 保護期間の切れた外国語の訳（ロングの英訳など） | relay | 台帳の credit（「ロングの英訳（1862）」）だけ。「試訳」とも AI で訳したとも書かない（D-024） |
| 日本の古文（方丈記の原文など） | modern | 何も出さない（D-023） |

- AI に見せるのは、原文と、保護期間の切れた時代の解釈（訓読・古い訳・注釈）だけです。今の本（S4）は見せません。
- 漢文・古文の訳には、互いに独立した時代の解釈が2つ以上要ります（based_on・tradition でつながる資料は1つと数えます。settings.toml の shiyaku.min_versions）。英訳からの訳は、元の英訳があれば作れます。
- 電子版（Standard Ebooks・Gutenberg）の英訳は、取り込んだまま（imported）でも訳の元にできます。漢文の白文と古文の原文は、人が確かめた（verified）ものだけです。

## 流れ

1. 訳を作ります（一節ごと。何度でも付けられます）。

   ```sh
   cd /Users/bunkoko/koten
   uv run koten shiyaku draft --passage lunyu:1.1
   ```

   - Opus が下書きします。
   - コードが確かめます（「試訳」や記号の書き込み、時代の解釈の日本語の写し、元に無い数など）。
   - Sonnet の判定役が、1文ずつ、支える版と判定を返します。合否はコードが決めます。
   - 通らなければ、2回まで書き直します。
   - 最後に、訳の ID（S-lunyu-1.1-1 など）と状態が出ます。

2. 読みます。

   ```sh
   uv run koten shiyaku show --id S-lunyu-1.1-1
   ```

   - 元の本文・時代の解釈・訳が並び、訳の文ごとに判定役の答え（○ 支えあり・△ 読みが分かれる・× 支えなし）が付きます。

3. 承認します（台本で使えるようになります）。

   ```sh
   uv run koten shiyaku approve --id S-lunyu-1.1-1
   ```

   - 判定役が止めた訳（flagged）は、読んで問題ないと判断したときだけ、`--note` に理由を書いて承認できます。
   - 読み方が分かれる一節だと確かめたら `--divided` を付けます（語りで「読み方が分かれています」と言えるようになります）。
   - 使わないときは `uv run koten shiyaku reject --id S-lunyu-1.1-1 --note 理由` です。承認したあとでも外せます。

4. 今の本との照合（任意。D-037）：言い回しが今の訳本に近すぎないかを、手元の本で確かめられます。手順は docs/check_guide.md と同じで、`--episode` の代わりに `--shiyaku` を付けます。

   ```sh
   uv run koten check list --shiyaku S-lunyu-1.1-1
   ```

   - 出た番号（T1…）で `koten check run --shiyaku S-lunyu-1.1-1 --claim T1 --book …` を打ちます。
   - 食い違い・言い回しが近いと出た訳は承認できません。訳し直して、新しい訳を同じ本で照合し直します。承認したあとで出たときは、その訳を使った回の公開も保留になります。

- 承認したあとで元の本文（原文・元の英訳）が変わったら、その訳は使われなくなります（訳し直します）。
- 自前の訳を使った回は、台本のときに人の確認（G6）が立ちます。

## 検索の候補にする

資料の本文がある一節を、悩みの検索の候補にします（手元の Ollama が要旨・テーマ・悩みを書きます）。

```sh
uv run koten canon build
uv run koten embed
uv run koten index build
```

- 候補にするのは、台本で日本語を出せる一節だけです（古い日本語訳か承認済みの訳がある、または訳の材料がそろっている）。
- 企画で論語などが選ばれたら、台本の前に、その一節の訳を作って承認します（`koten script` の G2 が案内します）。
- 作品ごとに、古い日本語訳の無い一節の割合を数えます（D-023 の見積もりの確かめ）。

  ```sh
  uv run koten canon coverage
  ```

## Project Gutenberg の英訳を取る（D-037）

1. 本体のサイト（www.gutenberg.org）からは取りません。config/sources.yaml の gutenberg.mirror に、公式のミラーの一覧（gutenberg.org/MIRRORS.ALL。ブラウザで見る）から選んだミラーの URL を書きます。
2. 使う版を台帳（ref_works.csv・ref_people.csv）に書きます。source は gutenberg、credit は「カソーボンの英訳（1634）」の形（「Project Gutenberg」の名前は D-004 を確かめるまで書きません）。`uv run koten ref check` で使えると出ることを確かめます。
3. sources.yaml の gutenberg.editions に、本の番号・ファイル名・作品記号・節の始まりの正規表現・巻の見出しの正規表現を書きます（例は sources.yaml にあります。版の中身を見てから書きます）。
4. 取り込みます。

   ```sh
   uv run koten ref fetch
   uv run koten ref build
   ```

- 節の番号はその版のものです。標準の番号と合うかは人が確かめます（自省録の Standard Ebooks 版は段落の番号 2.p1 なので、Gutenberg 版の 2.1 とは自動ではつながりません）。
