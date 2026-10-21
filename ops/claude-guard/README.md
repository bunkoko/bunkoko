# Claude Code の顧客データ遮断（Mac 全体の管理設定）

Claude Code が顧客データ（案件フォルダ `~/fukugyo-c-jobs/` と、受け取った原本が落ちてくる `~/Downloads/`）を
読めないようにする設定です。
リポジトリの `.claude/settings.json` にも読み取り禁止ルールとフックは入っていますが、
それだけでは Python スクリプト経由の読み取りを止められません。
この管理設定で OS レベルのサンドボックスを有効にし、Claude やリポジトリの設定から外せないようにします。

| 層 | 何を止めるか | 置き場所 |
| --- | --- | --- |
| 読み取り禁止ルール | Read / Edit ツールでの `~/fukugyo-c-jobs` と `~/Downloads` の読み書き | リポジトリ ＋ 管理設定 |
| サンドボックス（denyRead） | Bash から起動した Python などの子プロセスも含めた読み取り | 管理設定のみ |
| フック | 案件フォルダを指すパス・名前・シンボリックリンク・ワイルドカード・ホームからの再帰検索 | 管理設定（root 所有の写し） |
| 外せなくする設定 | 自動モード、管理設定以外のフック、管理設定以外の allowRead | 管理設定のみ |
| survey-coder 自身 | Claude Code の中（`CLAUDECODE=1` など）から実在の案件フォルダを開くこと | コード（`docs/design.md` 5章） |
| 運用 | 案件の処理は Claude Code を起動していない別ターミナルで行う | ― |

## フックが調べる範囲

文章の中にフォルダ名が出てくるだけでは止めません（設計書やプロンプトに案件フォルダの説明を書けるようにするため）。

| ツール | 調べる範囲 |
| --- | --- |
| コマンド系（Bash・Monitor・PowerShell） | 入力の全文。フォルダ名、パス、ワイルドカード、シンボリックリンク、`grep -r` や `find` での親フォルダからの再帰、`mdfind` |
| ファイル系（Read・Write・Edit・NotebookEdit・LS・Grep・Glob） | パスの項目（`file_path`、`notebook_path`、`path`、Glob の `pattern`）だけ。Grep・Glob は範囲の指定がなければ作業フォルダからの再帰として調べる |
| それ以外（MCP、StructuredOutput など） | パスを表す項目（名前の最後が path・file・dir・folder・root・uri など）と、値が1つのパスそのものの場合。`command`・`script` など、コマンドを表す項目は Bash と同じく全文 |

フックが失敗したとき（入力が読めない、判定中の例外）は拒否します（`onFailure: block`）。

## 設置手順（初回だけ、約15分）

1. Claude Code を更新する。フックの `onFailure` には v2.1.295 以降が必要です。

   ```bash
   claude update
   claude --version
   ```

2. Mac 標準の Python があるか確かめる（Xcode コマンドラインツールに含まれます）。

   ```bash
   /usr/bin/python3 --version   # 3.9 以上なら OK
   ```

3. リポジトリのルートで、フックのテストを通す。

   ```bash
   /usr/bin/python3 -m unittest discover -s tests/guard
   ```

4. 管理設定とフックを root 所有の場所に置く。Claude Code が使うのはこの写しで、リポジトリの元ファイルではありません
   （リポジトリのファイルは Claude が書き換えられるため）。

   ```bash
   sudo mkdir -p "/Library/Application Support/ClaudeCode/hooks"
   sudo cp ops/claude-guard/managed-settings.json "/Library/Application Support/ClaudeCode/managed-settings.json"
   sudo cp .claude/hooks/guard_customer_data.py "/Library/Application Support/ClaudeCode/hooks/"
   sudo chown -R root:wheel "/Library/Application Support/ClaudeCode"
   sudo chmod 755 "/Library/Application Support/ClaudeCode" "/Library/Application Support/ClaudeCode/hooks"
   sudo chmod 644 "/Library/Application Support/ClaudeCode/managed-settings.json"
   sudo chmod 755 "/Library/Application Support/ClaudeCode/hooks/guard_customer_data.py"
   ```

5. Claude Code を起動し直し、次の4つを確かめる。
   - `/status` に managed settings（file）が表示される
   - `/hooks` に `/Library/Application Support/ClaudeCode/hooks/guard_customer_data.py` が管理設定のフックとして表示される
     （リポジトリのフックは `allowManagedHooksOnly` で止まり、動きません）
   - `/sandbox` の Config に `~/fukugyo-c-jobs` と `~/Downloads` の denyRead が表示される
   - Shift+Tab で切り替えるモードに自動モード（auto）が出てこない

6. claude.ai の設定で「モデル改善への利用」をオフにする（Data privacy controls）。

## フックを更新するとき

フックは置き換えた瞬間から動いているすべての Claude Code に効き、壊れると全ツールが止まります。
新版は一時ファイルに置き、テストを通してから、1回の `mv` で置き換えます。

```bash
D="/Library/Application Support/ClaudeCode/hooks"
/usr/bin/python3 -m unittest discover -s tests/guard              # リポジトリの新版をテスト
sudo cp .claude/hooks/guard_customer_data.py "$D/.guard_customer_data.py.new"
sudo chown root:wheel "$D/.guard_customer_data.py.new"
sudo chmod 755 "$D/.guard_customer_data.py.new"
GUARD_HOOK="$D/.guard_customer_data.py.new" /usr/bin/python3 -m unittest discover -s tests/guard   # 写しをテスト
sudo mv "$D/.guard_customer_data.py.new" "$D/guard_customer_data.py"
```

リポジトリ内でフックを直すときも同じで、Claude Code に作業させる場合は `.claude/hooks/.guard_customer_data.py.new` に書かせ、
`GUARD_HOOK` で差し替えたテストを通してから `mv` させます。

## 効いているかの確認（見張りファイル）

普通のターミナル（Claude Code の外）で見張りファイルを作ります。

```bash
mkdir -p ~/fukugyo-c-jobs/_canary
echo "CANARY-$(date +%s)" > ~/fukugyo-c-jobs/_canary/canary.txt
echo "CANARY-DL-$(date +%s)" > ~/Downloads/canary.txt
```

Claude Code に次の5つを順に頼み、どれも中身（CANARY-…）が表示されないことを確かめます。

1. 「~/fukugyo-c-jobs/_canary/canary.txt を Read で読んで」→ 権限ルールかフックで拒否
2. 「cat で表示して」→ フックで拒否
3. 「`d=fukugyo; cat ~/${d}-c-jobs/_canary/canary.txt` を実行して」→ サンドボックスで `Operation not permitted`
4. 「Python で文字列をつなげてパスを作り、open() で読んで」→ サンドボックスで拒否
5. 「~/Downloads/canary.txt を cat で表示して」→ サンドボックスで拒否

3〜5 が読めてしまう場合、管理設定が効いていません。手順5の表示を確認してください。
確認が済んだら `~/fukugyo-c-jobs/_canary` と `~/Downloads/canary.txt` は消してかまいません。

## 影響と注意

- **Mac 上のすべての Claude Code に効きます。** 他のプロジェクトでも Bash がサンドボックス内で動き、
  許可リストにないサイトへの接続は確認を求められます。
- **`allowManagedHooksOnly: true`**：管理設定のフックだけが動きます。
  - ユーザー設定・プロジェクト設定（このリポジトリの `.claude/settings.json` を含む）・プラグイン・
    サブエージェント定義のフックは動きません。リポジトリのフックは、管理設定のない環境（クラウドのセッションなど）向けです。
  - ステータスライン（`statusLine`）、`@` のファイル候補（`fileSuggestion`）、`subagentStatusLine` は管理設定の値だけが使われます。
    自分のステータスラインは警告なしに表示されなくなります。使いたい場合は管理設定の `statusLine` に書きます。
  - `/goal` コマンドはフックに依存するため使えません。
- **`disableAutoMode: "disable"`**：自動モード（Claude が許可の確認を自分で判定するモード）を使えなくします。
  確認なしでコマンドが進む状態を作らないためです。自動モードで始まるはずのセッションは通常モードで始まります。
- **`allowManagedReadPathsOnly: true`**：サンドボックスの `allowRead` は管理設定のものだけが効きます。
  ユーザー設定やプロジェクト設定で `~/fukugyo-c-jobs` や `~/Downloads` の読み取りを開け直すことはできません（`denyRead` はどの設定からも足せます）。
- **`~/Downloads` の読み取り禁止**：ココナラのトークルームから落とした受領ファイルが置かれる場所なので封じます。
  Claude Code に読ませたい資料（顧客データ以外）は、`~/Downloads` から別のフォルダに移してから渡します。
  受領ファイルは `survey-coder intake` が案件フォルダにコピーしたあと、`~/Downloads` の原本を削除します。
- 案件ごとの暗号化ディスクイメージは `/Volumes` ではなく案件フォルダの中（`~/fukugyo-c-jobs/<案件ID>/vol`）にマウントします。
  案件フォルダの中なので、上の禁止がそのまま効きます。
- `allowLocalBinding: true` は、架空データでの検品で `127.0.0.1` の llama-server に接続するためです。
  サンドボックス内のコマンドが localhost の他のサービスにも届くようになるので、認証のないローカルサービスを常駐させないでください。
  案件用の llama-server は `--api-key` と `--no-slots` を付けて起動します（`docs/design.md` 4.5）。
- `allowWrite` の3つは uv と Hugging Face のキャッシュです。サンドボックス内で依存の追加やモデルの取得をするために開けています。
- `Edit(~/.claude/settings.json)` の禁止は、Claude がユーザー設定にサンドボックスの例外を書き足すのを防ぐためです。
  ユーザー設定を変えたいときは自分でエディタを使ってください。
- 管理設定は管理者権限があれば本人は外せます。これは Claude の誤操作への備えで、
  顧客の回答文やエラー出力を会話に貼る、`!` で案件を処理する、といった操作ミスは防げません。
  運用ルールは `docs/design.md` の5章にまとめています。

## sudo が使えない場合

`managed-settings.json` の中身を `~/.claude/settings.json` に手で統合します
（フックのパスは `$HOME/dev/bunkoko/.claude/hooks/guard_customer_data.py` のようにリポジトリ内を指す）。
ただしユーザー設定は Claude からも書き換えられるため、守りは弱くなります。
`allowManagedHooksOnly` と `allowManagedReadPathsOnly` は管理設定でしか効かないので、この場合は意味がありません。

## 元に戻す

```bash
sudo rm "/Library/Application Support/ClaudeCode/managed-settings.json"
sudo rm -r "/Library/Application Support/ClaudeCode/hooks"
```
