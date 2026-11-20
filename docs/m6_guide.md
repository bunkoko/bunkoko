# 1本を動画にする手順（M6）

台本ができた回（script_done）を、音声・画面・動画・説明欄・確認画面まで作り、確かめて承認するまでの手順です。
作りは docs/m6_design.md にあります。

## はじめに一度だけ

1. 動画づくりの道具を入れます。ml と一緒に入れないと、ml の道具が外れます。

   ```sh
   cd /Users/bunkoko/koten
   git pull
   uv sync --extra ml --extra video
   uv run playwright install chromium
   ```

2. 語りの声を選びます。

   - VOICEVOX のアプリで、落ち着いた大人の声を試聴して選びます。
   - 選んだ声の番号（speaker）を、次のコマンドの一覧で探します。

     ```sh
     curl -s http://127.0.0.1:50021/speakers | python3 -m json.tool | grep -E '"name"|"id"'
     ```

   - config/settings.toml の `[voicevox]` の `speaker_id` をその番号にします。

3. 選んだキャラクターの利用規約を確かめて、クレジットを書きます。

   - キャラクターごとに、使ってよい場面とクレジットの書き方が決まっています（VOICEVOX のサイトの、そのキャラクターのページ）。
   - config/settings.toml の `[voicevox]` の `credit` に、決まった書き方で書きます（例：`credit = "VOICEVOX:ずんだもん"`）。
   - 空のあいだは、確認画面に「承認できない理由」として出ます。
   - 確かめたことは、Claude に伝えて docs/decisions.md に書いてもらいます。

## 1回分を作る

```sh
cd /Users/bunkoko/koten
uv run koten produce --episode E20261107-01
```

- 音声 → 画面 → 組み立て → 説明欄・字幕・サムネイル → 確認画面 の順に動きます。15分ほどの回で、音声に数分、組み立てに数分かかります。
- 途中で止まったら、出た文のとおりに直して、止まった段からやり直します（例：`--from render`）。
- 最後に確認画面の場所が出ます。`open` で始まる行をそのまま打つと、ブラウザで開きます。

## 確認画面で見ること

- いちばん上の赤い枠「承認できません」：直さないと承認できない理由です。
- 黄色い枠「注意」：承認は止めませんが、読んで確かめることです。
- 動画：音を出して、読み違い・間・画面の切り替えを確かめます。
- 一節の原文と読み上げ：旧仮名の一節は、仮名づかいと字体だけを今に直して読んでいます。読み違いがあれば、config/readings.csv に1行足します（列：key・reading・note。key は段落の ID（ch2-s3 など））。足したら `uv run koten produce --episode E… ` でやり直します。
- 説明欄・チャプター・サムネイル・広告位置の候補。

## 人の一言

まとめの章に、あなたの一言を入れる枠があります。入れるか省くかを決めます。

```sh
uv run koten episode take --episode E20261107-01 --text "ここに一言"
```

- 自分の声で録った wav を使うときは `--audio`、入れないときは `--skip` です。
- 文字か声を入れたら、動画に入れるために `uv run koten produce --episode E20261107-01` をやり直します。

## 承認と差し戻し

```sh
uv run koten episode approve --episode E20261107-01 --note "見た"
```

- 承認できない理由が残っていると止まります。
- 直すところがあれば差し戻します（台本はそのまま：`--to script_done`、台本から書き直す：`--to planned`）。

```sh
uv run koten episode reject --episode E20261107-01 --to script_done --note "直すところ"
```

## 尺（G7）が合わないとき

- 合計が10〜15分に入らない、または章が formats.yaml の範囲の外だと、G7 が不合格になります。
- 少しだけなら、config/settings.toml の `[voicevox]` の `speed_scale`（話す速さ。0.9〜1.15 くらい）を変えて、`--from voice` からやり直します。
- 大きくずれるときは、差し戻して台本を書き直します。
