# 今後やること（2026-10-29。4TB の HDD が届いたあとの版）

- 決まっている順番は次のとおりです（D-016）。
  1. M8a（時代の解釈と昔の辞書）を受け入れる
  2. M8b（試訳）
  3. M8c（今の意味）
  4. M6（1本を手で通す）
  5. M7（n8n）
  6. M9（振り返り）
- 進み具合は docs/decisions.md の「マイルストーンの状況」に書きます。
- このページは、誰が何をするかの一覧です。判断の中身と根拠は docs/decisions.md、調べの全文は docs/research/ にあります。

## 0. 今すぐ：HDD へ引っ越す（D-031）

置き場所を SD カードから、4TB の HDD（/Volumes/koten-hdd/koten）に移します。SD カードの中身はそのまま残し、あとで控えに使います。

1. 動いている augment と update を止めます。それぞれの窓で Ctrl-C を押します。ここまでの結果は保存され、あとで続きから再開します。

2. HDD を Mac 用に整えます（**HDD の中身は全部消えます**。届いたばかりで何も入っていないことを確かめてください）。
   - 「ディスクユーティリティ」を開く →「表示」→「すべてのデバイスを表示」
   - 左の一覧で、新しい HDD（4TB）のいちばん上の行を選ぶ →「消去」
   - 名前：`koten-hdd`、フォーマット：APFS、方式：GUID パーティションマップ →「消去」
   - Time Machine に使うかを聞かれたら「使用しない」

3. 最新を取り込み、SD カードから HDD へ写します（SD カードは挿したまま）。

   ```sh
   cd /Users/bunkoko/koten
   git pull
   uv run koten hdd-copy --from "/Volumes/SDカード/古典×悩み"
   ```

   - 最後に「同じ大きさで写っていることを確かめました」と出れば終わりです。止まったときは、同じコマンドをもう一度打つと続きから写します。

4. 確かめます。HDD の行が `/Volumes/koten-hdd/koten` になり、空きが 3.5TB 以上あれば大丈夫です。

   ```sh
   uv run koten doctor
   ```

5. 止めた作業を再開します（新しい窓で。数日かかります）。

   ```sh
   caffeinate -i uv run koten update --index 2>&1 | tee -i -a ~/koten-update.log
   ```

   - 有名な作家と古典の訳を足し、残りの augment（前の分と増えた分）→ 埋め込み → 索引まで、1つで進めます。
   - Mac は電源につなぎ、HDD をつないだまま、ふたを開けたままにします。
   - 進み具合は `tail -n 30 ~/koten-update.log` で見ます。

6. 控えを取ります（週1回。update が動いていても取れます）。

   ```sh
   uv run koten backup --to "/Volumes/SDカード/koten-backup"
   ```

   - augment・埋め込み（作り直しに日数がかかるもの）・SSD の DB・config の台帳を写します。2回目からは変わったものだけを写します。
   - SD カードの空きが足りなくなったら、HDD で1〜2週間問題なく動いたのを確かめてから、SD カードの「古典×悩み」フォルダを消します。

## 1. オーナーが Mac ですること（0 のあと、上から順に）

| # | すること | なぜ | 手順 |
|---|---|---|---|
| 1 | 資料の台帳を書く | 権利の判定の元。台帳で「使える」と判定された資料しか取れない | ref_works.csv・ref_people.csv に書き、`uv run koten ref check`。「要確認」を確かめたら、確認の印を reviewed_hash に写す。団体名義の辞書は1冊ずつ奥付を見る（D-018）。Long 訳『自省録』の review_note には、元の本文のこと（D-030）を書く。手順は docs/ref_sources.md |
| 2 | 国会図書館の資料の公開範囲を画面で見て、一度だけまとめて取る | 「インターネット公開（保護期間満了）」のものだけを使う（D-003） | dl.ndl.go.jp/pid/（PID）を巻ごとに開き、ref_works.csv の ndl_access・ndl_checked_at に書く → `uv run koten ref check` で「使える」を見る → `uv run koten ref ndl-snapshot --ref` のあとに台帳の ref_id を書き、`--koma all` を付けて取る。原寸のまま取ってよい（D-031） |
| 3 | 古い英訳を取る | 『自省録』『論語』の訳の材料と、時代の解釈 | 版の巻末の記録（colophon）で元の本文を確かめる（D-020・D-030）→ `uv run koten ref check` → `uv run koten ref fetch` → `uv run koten ref build` |
| 4 | 昔の辞書の語釈と、論語の訓読を書き起こす | M8a の受け入れ（3冊から10見出し、論語5章に2種以上） | 画像と照らし、dict_entries.csv・renderings.csv に verified で書く → `uv run koten ref build`。論語の白文も、同じページから一緒に起こす（D-020） |
| 5 | 照合用の Ollama を設定する | 本の文がクラウドや記録に出ないようにする | docs/check_guide.md の「Ollama の設定」 |
| 6 | 照合に使う本を台帳に書き、1回試す | ②（判定だけを残す）が使えるかを確かめる | ref_books.csv に書く → `uv run koten ref build` → `uv run koten episode list` で台本のある回の ID を見る → `uv run koten check list --episode` のあとにその ID を書く → `check run`（docs/check_guide.md） |
| 7 | 背景の事実の下書きを確かめる | M5b の残り | `uv run koten facts review` → 別の資料で確かめて accept に 1 → `uv run koten facts accept` → `uv run koten facts build` |
| 8 | 検索の評価の点を付け、リランカーを試す（D-010） | M4 の残りとリランカーの比べ。update（0 の5）が終わってから | 下の「リランカーを試すコマンド」 |
| 9 | 写真が何枚足りないかを見る | 資料の画像をどれだけ足すかを決める（D-028） | `uv run koten episode visuals` |
| 10 | まだ判定不能の古典を見る（D-029） | 没年が空でも、1900年より前に名前が出ている人・1967年までに出た作者不詳の作品は自動で使える | `uv run koten ingest aozora --dry-run` の「没年が空で判定不能」に残った人だけ、config/aozora_review.csv に書く（docs/ref_sources.md）→ `uv run koten update --index` |
| 11 | HDD の使った量を測る（任意） | D-006（テキスト類の容量）を記録する | `du -sh /Volumes/koten-hdd/koten/*` の結果を Claude に貼る |

### リランカーを試すコマンド（#8）

内蔵 SSD の空きが少ないので、試すモデル（3つで約4GB）は HDD に落とします（HF_HOME）。

1. 評価セットを書き出す。候補のリランカーが上に上げた一節も入れる

   ```sh
   HF_HOME=/Volumes/koten-hdd/hf-cache uv run koten eval export --rerank-model hotchpotch/japanese-reranker-small-v2 --rerank-model cl-nagoya/ruri-v3-reranker-310m --rerank-model BAAI/bge-reranker-v2-m3
   ```

2. tests/eval/retrieval.csv の grade 列に、合う 1・合わない 0 を付ける。grader（Claude の Haiku）にも付けさせる

   ```sh
   uv run koten eval grade
   uv run koten eval agreement
   ```

3. 比べる（LLM の点で見るときは `--by llm` を付ける）

   ```sh
   HF_HOME=/Volumes/koten-hdd/hf-cache uv run koten eval retrieval --rerank-model hotchpotch/japanese-reranker-small-v2 --rerank-model cl-nagoya/ruri-v3-reranker-310m --rerank-model BAAI/bge-reranker-v2-m3
   ```

- 「点の無い一節」（unjudged）がほぼ0の行どうしで比べます。0でなければ、手順1〜2をやり直します（前に付けた点は残ります）。
- いちばん良いモデルを settings.toml の [rerank] model に書くと、本番の検索で使います。本番用のモデルは内蔵 SSD に置きます（検索は HDD を読まない決まりのため）。書いたあと、HF_HOME を付けずに次を1回動かすと、内蔵 SSD に落とします。

   ```sh
   uv run koten retrieve --worry W0001 --explain
   ```

あると助かること（任意）：

- このクラウド環境のネットワーク設定で、次のホストを許可してください。Claude が規約やモデルカードの原文を、ここから読めるようになります。
  - 国会図書館：dl.ndl.go.jp・www.ndl.go.jp・lab.ndl.go.jp
  - 資料の画像（D-028）：colbase.nich.go.jp・kokusho.nijl.ac.jp・www.metmuseum.org・www.artic.edu
  - リランカー（D-010）：huggingface.co
  - アフィリエイト（D-027）：support.google.com・affiliate.amazon.co.jp・www.caa.go.jp
  - 青空文庫（D-025・D-029）：www.aozora.gr.jp
  - 手順：https://code.claude.com/docs/en/cloud-environments#network-access

## 2. 開発（Claude）がすること

### M8a の受け入れ（オーナーの資料がそろったら）

- オーナーの資料で企画と台本を作り、語釈・解釈の入った回が G1〜G6 を通るかを確かめます。
- `ref ndl-snapshot` を本物の NDL で試した結果を見ます（マニフェストの公開範囲の値が、調べた記録どおりか）。

### M8b 試訳

- 古い日本語訳がある一節は、その訳を使います（D-014）。晶子訳『源氏物語』・尾崎士郎訳『平家物語』なども使えるようになりました（D-029）。
- 古典の一節（論語の章など）を検索の候補にし、一節と古典の対応を作ります。対応ができたら、古い訳が無い一節の割合を数えます（D-023 の見積もりの確かめ）。
- 自前の訳の流れ：AI の下書き → 機械の確かめ → 判定役（下書きと別の LLM。D-023）→ ②の照合（言い回しの近さも見る）→ 承認。
- 書き方：英訳から日本語にした文は元の訳の出典だけ（D-024）。漢文の白文からの自前の訳は「試訳」（R5）。日本の古文の自前の訳は「試訳」と書かない（D-023）。
- 『自省録』の節の番号は、番号のある版で人が確かめてから付けます。

### M8c 今の意味

- 自分たちの言葉で書いた1文（`{{gloss}}`）を作り、人が今の辞書2冊で確かめます。
- 日本語WordNet を下書きの材料として入れます（画面には出さない。D-022）。
- ndlngramdata（20.7GB。言葉の歴史の数字）を、net.py で流しながら保存する仕組みを足して一度だけ取ります（D-031）。
- 文化庁「国語に関する世論調査」の数字と、法令・政府の用語集も使います。

### M6 1本を手で通す

- 音声：VOICEVOX。語釈・解釈は人が確かめた読みで読み上げます。
- 画面：引用カード・統計グラフ・自分の写真・資料カード・事実カード。
  - 資料の画像（D-028）：権利に問題が無ければどこから取ってもよい。手は加えない。画像の台帳を作り、年齢制限につながりうる絵は印を付けて、確認画面で人が確かめてから出す。どこに使うかは、ここで動画の構成と一緒に決めます。
- 説明欄：出典、背景の出典、「参考にした本」（アフィリエイトのリンクと「（広告）」・Amazon の文言。D-027）。
- 中間ファイル（音声・画像）も HDD に残します（D-031）。
- そのほか：チャプター・字幕・サムネイル・確認画面。
- 公開の前の関門：G6 の人の確認、照合の保留が無いこと（`koten check status`）、NDL の公開範囲をもう一度見ること、G7（尺）。

### その後

- M7：n8n でつなぎます。
- M8d：論文。任意です。
- M9：コメントの取り込みと振り返り。公開10本ごとに、尺と視聴の要件（D-011）を見直します。
- 新しい形式「ことばを読む」：M8c のあとで1本試します（D-026）。

## 3. オーナーの判断

### 決まったこと（2026-10-29）

| ID | 決めたこと |
|---|---|
| D-003 | 国会図書館の資料は、使うものを一度だけまとめて取る（fetch_enabled = true） |
| D-010 | リランカーは、評価の点を付けてから比べる。まず試す |
| D-017 | 照合は②（判定だけを残す）。弁護士への相談は任意 |
| D-018 | 団体名義の古い辞書は、1冊ずつ人が奥付を見て確かめる |
| D-020 | Standard Ebooks は元が確かな版だけ。ほかの電子テキストは探す手がかりだけ |
| D-022 | 日本語WordNet は、画面に出さず、下書きの材料にだけ使う |
| D-023 | 日本の古文の自前の訳には「試訳」と書かない。別の LLM で意味を確かめる |
| D-024 | 英訳から日本語にした文は、元の訳の出典だけを書く |
| D-025 | 有名な作家の詩歌・小説を青空文庫から足す（`koten update`） |
| D-026 | 「ことばを読む」は M8c のあとで1本試す |
| D-027 | 説明欄にアフィリエイトのリンク（Amazon か楽天）。「（広告）」と Amazon の文言を書く |
| D-028 | 資料の画像は、権利に問題が無ければどこからでも。手は加えない。年齢制限につながりうる絵は人が確かめてから |
| D-029 | 作者不詳は1967年までに出たもの、没年が空の人は1900年より前に名前が出ていれば使う |
| D-030 | Long 訳『自省録』の Standard Ebooks 版を使う |
| D-031 | 4TB の HDD に移す。SD カードは控えに使う |

### まだ決めること

#### D-032 写真と BGM を HDD に移すか

- 何が問題か：内蔵 SSD の空きは約10GB です。自分の写真（数百枚で数GB）と BGM を内蔵（~/Pictures/koten-bg・~/Music/koten-bgm）に置くと、空きがさらに減ります。
- 移す：内蔵の空きが守れます。写真を使うのは動画の書き出し（M6）で、HDD を読んでよいバッチの処理なので、困りません。HDD を外していると、写真の枚数を数えられません。
- 移さない：写真アプリから直接入れやすいです。
- 推奨：移す。決まったら、settings.toml の photos_dir・bgm_dir を /Volumes/koten-hdd/media/ の下に変えます。

#### D-028 の残り：画像をどこに使うか（M6 で決める）

- 資料画像カード（一節・背景の章）だけか、写真の足りない所の背景にも使うか、サムネイルにも使うか。

#### 外した作家を足すか（augment が終わってから）

- 小川未明（約480作品）・岡本綺堂・江戸川乱歩・吉川英治・山本周五郎・宮本百合子は、件数か悩みとの相性で外しました（D-025）。容量は問題でなくなったので、制約は augment の時間（1チャンク約5秒）だけです。今の update が終わって、1回の augment にかかる日数が見えてから決めます。

## 4. 確かめること（規約・資料）

| ID | 確かめること | 誰が | いつまでに |
|---|---|---|---|
| D-003 | NDL の IIIF の「APIの利用について」（dl.ndl.go.jp/ja/help_iiif）を一度読む | オーナー（ブラウザ） | 大きく取る前（任意） |
| D-029 | 台帳（config/aozora_review.csv）に書く年の根拠（辞典・研究書の項目とページ） | オーナー | 台帳に書くとき |
| D-027 | Amazon アソシエイトの運営規約（表示の文言）・楽天で YouTube が使えるか（認定SNS・短縮 URL）・YouTube のブランドコンテンツのヘルプ（アフィリエイトだけなら申告が要らないか） | オーナー（ブラウザ） | 最初にリンクを載せる前 |
| D-028 | 使う取り先ごとの規約のページと出典の書き方 | オーナー（ブラウザ）か、ホストを許可して Claude | その取り先の画像を使う前 |
| D-010 | 候補のモデルカードの license 行と base_model 行 | オーナー（ブラウザ）か、huggingface.co を許可して Claude | リランカーを入れる前 |
| D-022 | 日本語WordNet のクレジットの年の書き方 | どちらでも | WordNet を入れる前 |
| R4 | 戦時加算の国ごとの日数（文化庁の表） | どちらでも | 1955年の境界を見直すとき |
| D-002 | 青空文庫の「収録ファイルの取り扱い規準」 | オーナー（ブラウザ） | M6 まで |
| D-007 | YouTube のチャプターの条件（各10秒以上か） | オーナー（ブラウザ） | M6 まで |
| D-004 | Project Gutenberg の名前を出典に書けるか | どちらでも | M8b まで |
| — | YouTube で、AI を使った内容の申告がこのチャンネルに要るか | オーナー（ブラウザ） | M6 まで |
| — | Mac の側の写しの残り方（ターミナルの復元・クリップボードの履歴） | オーナー | 照合を始める前（docs/check_guide.md） |
