# 副業C：自由回答の分類集計サービス（survey-coder）

アンケートの自由回答や口コミを、Mac のローカルLLMで分類・集計して納品するための仕組みです。
計画書：https://claude.ai/artifact/12nKambwL4jB8cMUkHuZHx （タブ「計画」「設計案」）
技術仕様：`docs/design.md`

## 絶対に守ること

1. **顧客データを扱わない。** 顧客データはリポジトリの外の案件フォルダ（`~/fukugyo-c-jobs/`）にだけあります。
   受け取った原本が一時的に置かれる `~/Downloads/` も同じ扱いです。
   読む・開く・一覧する・検索する・コマンドの引数に渡す、のどれもしないでください。
   ユーザーが案件のエラー出力やログを貼ってきたら、内容を使わずに、架空データで再現する方法を提案してください。
   開発・テスト・デバッグは架空データ（`samples/fictional/`、`tests/fixtures/`）だけで行います。
2. **案件の処理を実行しない。** 実在の案件ID に対する `survey-coder` の各コマンドは、
   ユーザーが Claude Code の外のターミナルで実行します。手順を書いて渡すのはかまいません。
3. **回答本文を外に出すコードを書かない。** LLM は `127.0.0.1` の llama-server だけを使い、
   外部の AI や API に回答本文を送る処理は書きません。処理中の通信は 127.0.0.1 と ::1 以外を拒否します。
4. **回答本文をログに出さない。** ログ・例外メッセージ・標準出力には回答ID、件数、時間、ハッシュだけを出します。
   `print` は使わず（ruff の T20）、回答本文が出ないことをテストで確かめます。
5. **架空データ以外の CSV / Excel をコミットしない。** `.gitignore` で除外しています。

これらは `.claude/settings.json` の読み取り禁止ルールとフック（`.claude/hooks/guard_customer_data.py`）、
Mac 全体の管理設定（`ops/claude-guard/`）でも止めています。フックに拒否されたら、回避せずに架空データで進めてください。
survey-coder 自身も、環境変数 `CLAUDECODE` か `CLAUDE_CODE_CHILD_SESSION` が `1` のときは実在の案件フォルダを開きません。

フックを直すときは、このセッションにもすぐ効き、壊れると全ツールが止まることに注意します。
新版を `.claude/hooks/.guard_customer_data.py.new` に書き、
`GUARD_HOOK=.claude/hooks/.guard_customer_data.py.new python3 -m unittest discover -s tests/guard` を通してから、1回の `mv` で置き換えます。
Bash のコマンドは全文が調べられるので、案件フォルダの名前を含むファイルは Edit / Write で直します。

## サブエージェント（`.claude/agents/`）

| 名前 | 役割 | モデル |
| --- | --- | --- |
| builder（構築係） | パイプラインの実装とテスト | sonnet、effort high |
| report-designer（レポート係） | 納品Excel・グラフ・精度報告のテンプレート | sonnet |
| qa-checker（検品係） | 架空データで全工程を流し、食い違いと伏せ漏れを調べる。コードは直さない | opus、effort high |
| listing-writer（出品文係） | ココナラの出品ページ、規約、定型の返信文 | sonnet |
| sample-writer（本文書き係） | 架空アンケートの回答本文だけを書く | sonnet |

呼び出すときに model や effort を指定しないでください（定義ファイルの値が使われなくなります）。
検品係の作業の前後で `git status --porcelain` を比べ、検品係がファイルを変えていないことを確かめます。

## 開発の進め方

- Python 3.14 と uv を使います（`.python-version` で固定。3.15 は torch が未対応）。
- 依存の追加は `uv add`、実行は `uv run`。lock ファイル（`uv.lock`）はコミットします。
- テストは `uv run pytest`、静的検査は `uv run ruff check` と `uv run ruff format --check`。
- フックのテストは `python3 -m unittest discover -s tests/guard`。
- 文字列を Excel に書くときは `strings_to_formulas=False` と `strings_to_urls=False` を必ず指定します。
- CSV の読み込みは `dtype=str, keep_default_na=False` に統一します（「N/A」などを空欄にしないため）。

## 架空データのルール

- メールアドレスは `example.jp` / `example.co.jp` / `example.com` だけを使います。
- 電話番号は `03-0XXX-XXXX`、`090-0XXX-XXXX`、`070-0XXX-XXXX` の形だけを使います（実在しない番号）。
  `06-0`、`080-0`、`060` で始まる形は実在しうるので使いません。
- 実在の企業名・人名・商品名は使いません。社名は「A社」とします。
- Claude が書いた架空の文章は、ローカルLLMの追加学習（ファインチューニング）には使いません。
