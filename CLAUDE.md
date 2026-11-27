# koten-videos

悩み×古典の一節で10分以上の長尺動画を作るパイプライン。仕様は docs/SPEC.md。

## 進め方
- docs/SPEC.md §13 のマイルストーン順に進める。1つ終えるごとにテストを通し、受け入れ条件を確認してから次へ
- 「要確認」の箇所は実装前に公式ドキュメント・利用規約で確かめ、結果を docs/decisions.md に書く
- 判断に迷ったら実装を止めて質問する
- 進捗は docs/decisions.md の「マイルストーンの状況」に書く
- 結果の報告は、「やったこと」「わかったこと／問題」「次にやること」の3つの見出しで書く（2026-11-24 利用者。どのスレッド・どの会話でも同じ）

## 絶対に守ること
- 権利判定（src/koten/license.py、SPEC §3 の R1〜R8）を緩めない・迂回しない
- 引用・出典・統計の数字はDBから差し込む。LLMに書かせない
- 背景（著者の経歴・作品の成り立ち・時代）は、DBの確認済みの事実だけを根拠に語らせる。人名・地名・作品名・年・数は事実の記号（{{fact:ID.項目}}）で差し込む（SPEC R6）
- 昔の辞書の語釈・時代の解釈（訓読・古い訳・注釈）は、DB の確かめ済みの本文を記号（{{def:ID}}・{{interp:ID}} と出典の {{def_src:ID}}・{{interp_src:ID}}）で差し込む。「辞書では」と言えるのはその記号と同じ文だけ。著作権のある今の辞書・訳・注釈書（S4）の本文は保存せず、AI に渡さず、語りで書名を出さない（D-015・D-017）
- 今の本（S4）での照合は、手元の Ollama（127.0.0.1）にだけ本の該当箇所を渡し、判定だけを残す。本の文はファイル・DB・ログ・画面に残さない。食い違い・言い回しが近い文がある回は公開しない（SPEC R8）
- 資料の台帳（config/ref_works.csv・ref_people.csv）の判定は、関わった人全員の没年・団体名義・版・NDL の公開範囲のいちばん厳しい結果。人の確認で使えるようにできるのは NEEDS_REVIEW だけ
- ウィキペディアは背景の事実を探す手がかりだけ。索引にせず、台本を書くLLMと判定役に渡さない。事実は別の資料で人が確かめてから使う（SPEC R2）
- 調査・研究・数字は DB にあるものだけを記号で差し込む。研究・実験・専門家の話は、研究の台帳（config/findings.csv）の記号 {{finding:ID}}・{{finding_src:ID}} と同じ文でだけ。そのあとの「この研究では」は、台帳の調べた人・限界の記号 {{finding_pop:ID}}・{{finding_limits:ID}} の文でだけ（書き手の評価を足させない）。「増え続けています」は、増え方を人が確かめた統計（trend）の隣だけ。有料の論文・民間の報告書の本文や要旨は保存せず、クラウドの AI に渡さない（人が読んで自分の言葉で書いた1文と書誌だけ。D-034・D-035）
- 除外カテゴリ（医療・自傷・お金の判断・法律相談など）の悩みで回を作らない
- 大きなデータはGitに入れない。HDD/SSDのパスは config/settings.toml から読む
- 検索・APIの処理中にHDDを読まない（索引とDBはSSD）
- 外部サイトへのアクセスは必ずレート制限とキャッシュを通す（src/koten/net.py）
- VOICEVOXの audio_query と synthesis には同じ speaker ID を渡す

## よく使うコマンド
- uv run pytest
- uv run ruff check .
- uv run koten doctor
- uv run koten ingest aozora --limit 5 && uv run koten normalize aozora && uv run koten chunk aozora
- uv sync --extra ml（埋め込みモデルと SudachiPy。M2 以降で必要）
- uv run koten augment --limit 20 && uv run koten embed && uv run koten index build
- uv run koten update --index（留守のあいだ用：青空文庫の取り直し → 正規化 → チャンク化 → 動いている augment が終わるのを待って、増えた分の augment → 埋め込み → 索引。augment は2つ同時に動かさない）
- uv run koten hdd-copy --from "/Volumes/SDカード/古典×悩み" --whole-card（前の置き場所から新しい HDD へ写す。--whole-card で SD カードのほかのものも HDD のいちばん上へ。augment・update を止めてから。--check で写さずに確かめるだけ）／ uv run koten backup --to "/Volumes/SDカード/koten-backup"（augment・emb・SSD の DB・設定の控え。週1）
- uv run koten index query "同期の昇進を喜べない"
- uv run koten serve（127.0.0.1 の API。SSD だけを読む）
- uv run koten db init（SSD の SQLite を作り、stats.csv を読み込む）
- uv run koten worries import --from config/worries_seed.csv && uv run koten worries normalize
- uv run koten worries list（優先度順）／ --status blocked など
- uv run koten retrieve --worry W0001 --explain
- uv run koten plan --worry W0001 --format A --dry-run（--format を省くと形式を、--worry を省くと優先度の高い悩みを自動で選ぶ。形式C は --theme 孤独）
- uv run koten eval export → uv run koten eval label（ターミナルで1行ずつ 1/0 を付ける。--worries 5 で上から5件の悩みだけ。表計算ソフトなら tests/eval/retrieval.csv の grade 列）
- uv run koten eval grade（Claude Code の判定役が llm_grade 列を埋める。既定は opus、--model sonnet・haiku で替える。基準は prompts/grader_v4.md）→ uv run koten eval agreement
- スマホで点を付けるとき：Mac で eval export → tests/eval/retrieval.csv を GitHub に入れる → Claude が uv run koten eval page（--review で見直す行を指定）でページを作り、アーティファクト「検索の評価」に載せる → 人がスマホで押す → Claude がページのデータベースの grades を読んで JSON にし、uv run koten eval merge --from その JSON で grade 列に書いて GitHub に入れる（docs/decisions.md 2026-11-08）
- uv run koten eval retrieval（--by llm で LLM の点を使う。--rerank-model でリランカーを1つずつ足して比べる。export にも同じ指定をして、上に上がった一節にも点を付ける）
- uv run koten plan --worry W0001 --format A（保存）→ uv run koten script --episode E20261101-01 → --show で読む。回の ID の代わりに --episode latest と書くと、いちばん新しい回（取り消した回は除く）。人に渡すコマンドは latest で書けば ID が要らない
- uv run koten episode list ／ uv run koten episode discard --episode <ID>（試しの回を取り消す）
- uv run koten produce --episode E…（M6：台本のあと、音声 → 画面 → 組み立て → 公開用キット → 確認画面を続けて。--from render などで途中から、--to voice で音声だけ（確認画面で台本と音声を聞く）。段ごとに koten voice・visuals・render・kit・review も）→ HDD の exports/<回>/review.html を開く → koten episode take --episode E… --text …（人の一言。--audio・--skip）→ koten episode approve --episode E…（承認。差し戻しは reject --to script_done --note …）。作りは docs/m6_design.md
- uv run koten forget-works --changed（正規化で本文が変わった作品。--apply で付加情報を取り除き、augment で作り直す）
- uv run koten facts build（HDD の書誌と config/facts.csv から、SSD の事実の表を作り直す）／ facts coverage ／ facts list --episode <ID>
- uv run koten facts todo --episode <ID> → facts draft --episode <ID>（AI の下書き）→ facts review（下書きを読む）→ 人が別の資料で確かめ、CSV の accept に 1 → facts accept（--from を省くと最新の下書き）→ facts build → facts attach --episode <ID>（企画に事実を付け直す）→ script
- uv run koten findings build（config/findings.csv の研究の一文を SSD に入れる。書き方は docs/findings.md）／ findings list
- 統計と研究の候補（docs/sources_check.md）：Claude が下書き（docs/research/*-source-drafts.json）を作る → KOTEN_CONTACT=… uv run koten sources locate --drafts その下書き（元の資料の該当ページの画像に印・論文の該当の文。data/sources-img）→ uv run koten sources merge --from 押した結果の JSON --accept-rest（押していない統計と CC BY の研究は AI が確かめた行として台帳へ）。回で使った AI の行は承認の前に人が確かめる：Mac で uv run koten sources todo --episode E…（docs/checks/E….json を GitHub へ）→ Claude が uv run koten sources page --todo docs/checks/E….json でその行だけのページを作り、画像と一緒にアーティファクト「統計と研究の確かめ」に載せる → 人が押す → uv run koten sources merge --todo docs/checks/E….json --from その JSON（民間の調査・有料の論文は人が書いた数字と言葉だけ）
- uv run koten stats meta --drafts docs/research/2026-11-27-series-drafts.json（Mac のみ・API の系列。KOTEN_ESTAT_APPID が .env に要る。分類の名前から符号を決めて下書きと docs/estat の系列ごとのファイル に書く。手順は docs/stat_series.md）
- uv run koten stats fetch（e-Stat から系列の数を取り config/stat_points.csv に書く。--check＝書かずに確かめる・--refresh＝取り直す・--series 系列の ID・--drafts＝下書きを確かめるだけ。保留の系列は飛ばす。API の系列は Mac のみ。続けて db init。docs/stat_series.md）
- uv run koten stats list（SSD の系列の一覧。--theme・--stale＝新しい年が出ているのに古い・--unchecked＝オーナーが未確認。docs/stat_series.md）
- uv run koten ref check（config/ref_works.csv・ref_people.csv の資料を、関わった人全員の没年・団体名義・版・NDL の公開範囲で1冊ずつ判定する。書き込みはしない）
- uv run koten ref build（台帳と、人が確かめた時代の解釈 renderings.csv・昔の辞書 dict_entries.csv から SSD の表を作り直す）→ ref show --word 普通 ／ --passage lunyu:1.1（手順は docs/ref_sources.md）
- uv run koten ref fetch（台帳で使えると判定された古い英訳を Standard Ebooks と Project Gutenberg のミラー（sources.yaml の gutenberg.mirror）から net.py で取り、一節ごとに分けて HDD に置く）→ ref build
- uv run koten ref attach --episode E… --chapter ch2 --id I-…（確かめ済みの時代の解釈・語釈を企画の章に付ける。--remove で外す）。悩みの言葉の語釈は企画が自動で付ける
- uv run koten check list --episode E… → check run --episode E… --claim C5 --book B-… --page 23ページ（本の該当箇所を貼り付けて Ctrl-D。手元の AI で判定だけを残す）→ check status（公開の保留）
- uv run koten shiyaku draft --passage lunyu:1.1（古典の一節の訳を Opus が下書きし、コードと Sonnet の判定役が確かめる。古い日本語訳がある一節は作らない。D-014・D-037）→ shiyaku show --id S-… で読む → shiyaku approve --id S-…（台本で使える）。今の本との照合は任意：check list --shiyaku S-… → check run --shiyaku S-… --claim T1 …
- uv run koten canon build（資料の本文がある古典の一節を検索の候補にする。手元の Ollama。続けて embed → index build）／ canon coverage（古い日本語訳の無い一節の割合）
- uv run koten ref ndl-snapshot --ref R-… --koma all（国会図書館の資料を一度だけまとめて取る。マニフェストで公開範囲を確かめ、3秒あけて取る。一度だけ取る使い方は継続的な利用に当たらないと判断した（D-003）。手順は docs/ref_sources.md）
