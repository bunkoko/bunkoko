# survey-coder（副業C：自由回答の分類集計）

アンケートの自由回答や口コミを、Mac のローカルLLMで分類・集計し、
Excel・グラフ・代表意見・精度報告として納品するための仕組みです。
顧客データは Mac の外にも Claude Code にも出しません。

- 計画書と設計案：https://claude.ai/artifact/12nKambwL4jB8cMUkHuZHx
- 技術仕様：[docs/design.md](docs/design.md)
- Claude Code のルール：[CLAUDE.md](CLAUDE.md)
- 顧客データ遮断の設置手順：[ops/claude-guard/README.md](ops/claude-guard/README.md)

## いまの状態

設計と安全対策の土台、架空データの仕様表（`tools/samplegen/`、出力は `samples/fictional/es_survey_v1/`）までです。
使う Mac は MacBook Pro（M5、32GB、macOS 27.0.1）に決まりました。
次は、人手の見本（Q1 を中心に30〜50件）を用意し、仕様表のバッチに沿って本文書き係（`.claude/agents/sample-writer.md`）が回答本文を書き、
続けてパイプライン本体（`src/survey_coder/`）を構築係（`.claude/agents/builder.md`）が実装します。

## 最初にやること（Mac で）

1. `ops/claude-guard/README.md` の手順で、顧客データ遮断の管理設定を入れる
2. 見張りファイルで、Claude Code から案件フォルダを読めないことを確かめる
3. `python3 -m unittest discover -s tests/guard` でフックのテストを通す
