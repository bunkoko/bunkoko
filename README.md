# survey-coder（副業C：自由回答の分類集計）

アンケートの自由回答や口コミを、Mac のローカルLLMで分類・集計し、
Excel・グラフ・代表意見・精度報告として納品するための仕組みです。
顧客データは Mac の外にも Claude Code にも出しません。

- 計画書と設計案：https://claude.ai/artifact/12nKambwL4jB8cMUkHuZHx
- 技術仕様：[docs/design.md](docs/design.md)
- Claude Code のルール：[CLAUDE.md](CLAUDE.md)
- 顧客データ遮断の設置手順：[ops/claude-guard/README.md](ops/claude-guard/README.md)

## いまの状態

設計と安全対策の土台までです。パイプライン本体（`src/survey_coder/`）と架空データの生成（`tools/samplegen/`）は、
Mac の機種とメモリが決まってから構築係（`.claude/agents/builder.md`）が実装します。

## 最初にやること（Mac で）

1. `ops/claude-guard/README.md` の手順で、顧客データ遮断の管理設定を入れる
2. 見張りファイルで、Claude Code から案件フォルダを読めないことを確かめる
3. `python3 -m unittest discover -s tests/guard` でフックのテストを通す
