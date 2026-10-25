# koten-videos

悩み×古典の一節で10分以上の長尺動画を作るパイプライン。仕様は docs/SPEC.md。

## 進め方
- docs/SPEC.md §13 のマイルストーン順に進める。1つ終えるごとにテストを通し、受け入れ条件を確認してから次へ
- 「要確認」の箇所は実装前に公式ドキュメント・利用規約で確かめ、結果を docs/decisions.md に書く
- 判断に迷ったら実装を止めて質問する
- 進捗は docs/decisions.md の「マイルストーンの状況」に書く

## 絶対に守ること
- 権利判定（src/koten/license.py、SPEC §3 の R1〜R7）を緩めない・迂回しない
- 引用・出典・著者情報・統計の数字はDBから差し込む。LLMに書かせない
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
- uv run koten plan --worry W0001 --format A --dry-run（--format を省くと形式を自動で選ぶ。形式C は --theme 孤独）
- uv run koten eval export → tests/eval/retrieval.csv の grade 列に 1/0（人）
- uv run koten eval grade（Claude Code の Haiku が llm_grade 列を埋める）→ uv run koten eval agreement
- uv run koten eval retrieval（--by llm で LLM の点を使う）
- uv run koten plan --worry W0001 --format A（保存）→ uv run koten script --episode E20261101-01 → --show で読む
