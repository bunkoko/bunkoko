# koten-videos

悩み×古典の一節で10分以上の長尺動画を作るパイプライン。仕様は docs/SPEC.md。

## 進め方
- docs/SPEC.md §13 のマイルストーン順に進める。1つ終えるごとにテストを通し、受け入れ条件を確認してから次へ
- 「要確認」の箇所は実装前に公式ドキュメント・利用規約で確かめ、結果を docs/decisions.md に書く
- 判断に迷ったら実装を止めて質問する
- 進捗は docs/decisions.md の「マイルストーンの状況」に書く

## 絶対に守ること
- 権利判定（src/koten/license.py、SPEC §3 の R1〜R8）を緩めない・迂回しない
- 引用・出典・統計の数字はDBから差し込む。LLMに書かせない
- 背景（著者の経歴・作品の成り立ち・時代）は、DBの確認済みの事実だけを根拠に語らせる。人名・地名・作品名・年・数は事実の記号（{{fact:ID.項目}}）で差し込む（SPEC R6）
- 昔の辞書の語釈・時代の解釈（訓読・古い訳・注釈）は、DB の確かめ済みの本文を記号（{{def:ID}}・{{interp:ID}} と出典の {{def_src:ID}}・{{interp_src:ID}}）で差し込む。「辞書では」と言えるのはその記号と同じ文だけ。著作権のある今の辞書・訳・注釈書（S4）の本文は保存せず、AI に渡さず、語りで書名を出さない（D-015・D-017）
- 今の本（S4）での照合は、手元の Ollama（127.0.0.1）にだけ本の該当箇所を渡し、判定だけを残す。本の文はファイル・DB・ログ・画面に残さない。食い違い・言い回しが近い文がある回は公開しない（SPEC R8）
- 資料の台帳（config/ref_works.csv・ref_people.csv）の判定は、関わった人全員の没年・団体名義・版・NDL の公開範囲のいちばん厳しい結果。人の確認で使えるようにできるのは NEEDS_REVIEW だけ
- ウィキペディアは背景の事実を探す手がかりだけ。索引にせず、台本を書くLLMと判定役に渡さない。事実は別の資料で人が確かめてから使う（SPEC R2）
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
- uv run koten index query "同期の昇進を喜べない"
- uv run koten serve（127.0.0.1 の API。SSD だけを読む）
- uv run koten db init（SSD の SQLite を作り、stats.csv を読み込む）
- uv run koten worries import --from config/worries_seed.csv && uv run koten worries normalize
- uv run koten worries list（優先度順）／ --status blocked など
- uv run koten retrieve --worry W0001 --explain
- uv run koten plan --worry W0001 --format A --dry-run（--format を省くと形式を、--worry を省くと優先度の高い悩みを自動で選ぶ。形式C は --theme 孤独）
- uv run koten eval export → tests/eval/retrieval.csv の grade 列に 1/0（人）
- uv run koten eval grade（Claude Code の Haiku が llm_grade 列を埋める）→ uv run koten eval agreement
- uv run koten eval retrieval（--by llm で LLM の点を使う）
- uv run koten plan --worry W0001 --format A（保存）→ uv run koten script --episode E20261101-01 → --show で読む
- uv run koten episode list ／ uv run koten episode discard --episode <ID>（試しの回を取り消す）
- uv run koten forget-works --changed（正規化で本文が変わった作品。--apply で付加情報を取り除き、augment で作り直す）
- uv run koten facts build（HDD の書誌と config/facts.csv から、SSD の事実の表を作り直す）／ facts coverage ／ facts list --episode <ID>
- uv run koten facts todo --episode <ID> → facts draft --episode <ID>（AI の下書き）→ facts review（下書きを読む）→ 人が別の資料で確かめ、CSV の accept に 1 → facts accept（--from を省くと最新の下書き）→ facts build → facts attach --episode <ID>（企画に事実を付け直す）→ script
- uv run koten ref check（config/ref_works.csv・ref_people.csv の資料を、関わった人全員の没年・団体名義・版・NDL の公開範囲で1冊ずつ判定する。書き込みはしない）
- uv run koten ref build（台帳と、人が確かめた時代の解釈 renderings.csv・昔の辞書 dict_entries.csv から SSD の表を作り直す）→ ref show --word 普通 ／ --passage lunyu:1.1（手順は docs/ref_sources.md）
- uv run koten ref fetch（台帳で使えると判定された古い英訳を Standard Ebooks から net.py で取り、一節ごとに分けて HDD に置く）→ ref build
- uv run koten ref attach --episode E… --chapter ch2 --id I-…（確かめ済みの時代の解釈・語釈を企画の章に付ける。--remove で外す）。悩みの言葉の語釈は企画が自動で付ける
- uv run koten check list --episode E… → check run --episode E… --claim C5 --book B-… --page 23ページ（本の該当箇所を貼り付けて Ctrl-D。手元の AI で判定だけを残す）→ check status（公開の保留）
