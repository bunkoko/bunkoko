# Claude Code の顧客データ遮断（Mac 全体の管理設定）

Claude Code が顧客データ（案件フォルダ `~/fukugyo-c-jobs/`）を読めないようにする設定です。
リポジトリの `.claude/settings.json` にも読み取り禁止ルールとフックは入っていますが、
それだけでは Python スクリプト経由の読み取りを止められません。
この管理設定で OS レベルのサンドボックスを有効にし、Claude やリポジトリの設定から外せないようにします。

| 層 | 何を止めるか | 置き場所 |
| --- | --- | --- |
| 読み取り禁止ルール | Read / Edit ツール、`cat` などの読み取りコマンド | リポジトリ ＋ 管理設定 |
| サンドボックス（denyRead） | Bash から起動した Python などの子プロセスも含めた読み取り | 管理設定のみ |
| フック | 案件フォルダを指すパス・名前・シンボリックリンク・ワイルドカード・ホームからの再帰検索 | リポジトリ ＋ 管理設定 |
| 運用 | 案件の処理は Claude Code を起動していない別ターミナルで行う | ― |

## 設置手順（初回だけ、約10分）

1. Claude Code を更新する。フックの `onFailure` には v2.1.295 以降が必要です。

   ```bash
   claude update
   claude --version
   ```

2. Mac 標準の Python があるか確かめる（Xcode コマンドラインツールに含まれます）。

   ```bash
   /usr/bin/python3 --version   # 3.9 以上なら OK
   ```

3. リポジトリのルートで、管理設定とフックを root 所有の場所に置く。

   ```bash
   sudo mkdir -p "/Library/Application Support/ClaudeCode/hooks"
   sudo cp ops/claude-guard/managed-settings.json "/Library/Application Support/ClaudeCode/managed-settings.json"
   sudo cp .claude/hooks/guard_customer_data.py "/Library/Application Support/ClaudeCode/hooks/"
   sudo chown -R root:wheel "/Library/Application Support/ClaudeCode"
   sudo chmod 644 "/Library/Application Support/ClaudeCode/managed-settings.json"
   sudo chmod 755 "/Library/Application Support/ClaudeCode/hooks/guard_customer_data.py"
   ```

4. Claude Code を起動し直し、次の3つを確かめる。
   - `/status` に managed settings（file）が表示される
   - `/hooks` に guard_customer_data.py が表示される
   - `/sandbox` の Config に `~/fukugyo-c-jobs` の denyRead が表示される

5. claude.ai の設定で「モデル改善への利用」をオフにする（Data privacy controls）。

## 効いているかの確認（見張りファイル）

普通のターミナル（Claude Code の外）で見張りファイルを作ります。

```bash
mkdir -p ~/fukugyo-c-jobs/_canary
echo "CANARY-$(date +%s)" > ~/fukugyo-c-jobs/_canary/canary.txt
```

Claude Code に次の4つを順に頼み、どれも中身（CANARY-…）が表示されないことを確かめます。

1. 「~/fukugyo-c-jobs/_canary/canary.txt を Read で読んで」→ 権限ルールかフックで拒否
2. 「cat で表示して」→ フックで拒否
3. 「`d=fukugyo; cat ~/${d}-c-jobs/_canary/canary.txt` を実行して」→ サンドボックスで `Operation not permitted`
4. 「Python で文字列をつなげてパスを作り、open() で読んで」→ サンドボックスで拒否

3 と 4 が読めてしまう場合、管理設定が効いていません。手順4の表示を確認してください。
確認が済んだら `~/fukugyo-c-jobs/_canary` は消してかまいません。

## 影響と注意

- **Mac 上のすべての Claude Code に効きます。** 他のプロジェクトでも Bash がサンドボックス内で動き、
  許可リストにないサイトへの接続は確認を求められます。
- `allowLocalBinding: true` は、架空データでの検品で `127.0.0.1` の llama-server に接続するためです。
  サンドボックス内のコマンドが localhost の他のサービスにも届くようになるので、
  認証のないローカルサービスを常駐させないでください。
- `allowWrite` の3つは uv と Hugging Face のキャッシュです。サンドボックス内で依存の追加やモデルの取得をするために開けています。
- `Edit(~/.claude/settings.json)` の禁止は、Claude がユーザー設定にサンドボックスの例外を書き足すのを防ぐためです。
  ユーザー設定を変えたいときは自分でエディタを使ってください。
- 管理設定は管理者権限があれば本人は外せます。これは Claude の誤操作への備えで、
  顧客の回答文を会話に貼る、`!` で案件を処理する、といった操作ミスは防げません。

## sudo が使えない場合

`managed-settings.json` の中身を `~/.claude/settings.json` に手で統合します
（フックのパスは `$HOME/dev/bunkoko/.claude/hooks/guard_customer_data.py` のようにリポジトリ内を指す）。
ただしユーザー設定は Claude からも書き換えられるため、守りは弱くなります。

## 元に戻す

```bash
sudo rm "/Library/Application Support/ClaudeCode/managed-settings.json"
sudo rm -r "/Library/Application Support/ClaudeCode/hooks"
```
