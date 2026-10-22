# koten-videos（ぶんここ）

悩み×古典の一節で、10分以上のYouTube長尺動画を台本から自動で作るパイプライン。

- 仕様：[docs/SPEC.md](docs/SPEC.md)（マイルストーン M0〜M9 は §13）
- 判断の記録と進捗：[docs/decisions.md](docs/decisions.md)
- Claude Code 向けの前提：[CLAUDE.md](CLAUDE.md)

## 使い方（M2 まで）

```sh
uv sync
cp .env.example .env            # APIキーを書く
# config/settings.toml の paths と http.user_agent を自分の環境に合わせる
uv run koten doctor             # HDD・SSD・VOICEVOX・LLM・埋め込みモデルの状態
uv run koten ingest aozora --limit 20
uv run koten normalize aozora
uv run koten chunk aozora
uv sync --extra ml              # 埋め込みモデルと SudachiPy（M2）
uv run koten augment --limit 20 # ローカルLLM で引用候補を抜き出す（夜間向け）
uv run koten embed && uv run koten index build
uv run koten index query "同期の昇進を喜べない"
uv run koten serve              # 127.0.0.1:8765 の API（SSD だけを読む）
uv run pytest
```
