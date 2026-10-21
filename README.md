# koten-videos（ぶんここ）

悩み×古典の一節で、10分以上のYouTube長尺動画を台本から自動で作るパイプライン。

- 仕様：[docs/SPEC.md](docs/SPEC.md)（マイルストーン M0〜M9 は §13）
- 判断の記録と進捗：[docs/decisions.md](docs/decisions.md)
- Claude Code 向けの前提：[CLAUDE.md](CLAUDE.md)

## 使い方（M1 まで）

```sh
uv sync
cp .env.example .env            # APIキーを書く
# config/settings.toml の paths と http.user_agent を自分の環境に合わせる
uv run koten doctor             # HDD・SSD・VOICEVOX・LLM・埋め込みモデルの状態
uv run koten ingest aozora --limit 20
uv run koten normalize aozora
uv run koten chunk aozora
uv run pytest
```
