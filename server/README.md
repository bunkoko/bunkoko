# bunkoko-server

睡眠と部屋の環境のデータを集めて、1日 = 1行の表を計算するサーバー。

- SwitchBot の全機器の値を **5分ごと** に取得して保存する。人感センサー Pro・開閉センサーなどの変化は **Webhook** でも受け取り、正確な時刻で記録する（Phase 0）
- iPhone アプリから HealthKit のデータと記録（食事・朝の体感など）を受け取り、1日 = 1行の表を計算する（Phase 1）
- Claude Code のスケジュールされたタスクに、食事の目安を出してもらう（Phase 1）

- 動く場所: Cloudflare Workers（サーバーの管理が不要で、5分ごとの自動実行ができる）
- 保存先: Cloudflare D1（SQLite のデータベース）
- 言語: TypeScript
- 費用: 無料枠に収まる見込み（[費用と上限](#費用と上限)）

センサーの値は、記録していない期間の分を後から取れない。**機器が届いたらすぐ記録が始まる**ように、先にこのサーバーを動かしておく。

---

## 目次
1. [準備（Mac に入れるもの）](#1-準備mac-に入れるもの)
2. [Cloudflare に置く（機器が届く前にできる）](#2-cloudflare-に置く機器が届く前にできる)
3. [SwitchBot とつなぐ（機器が届いたら）](#3-switchbot-とつなぐ機器が届いたら)
4. [動いているか確かめる](#4-動いているか確かめる)
5. [データを取り出す](#5-データを取り出す)
6. [機器がなくても手元で試す](#6-機器がなくても手元で試す)
7. [仕組み](#7-仕組み)
8. [食事の目安（Claude Code のスケジュールされたタスク）](#8-食事の目安claude-code-のスケジュールされたタスク)

---

## 1. 準備（Mac に入れるもの）

1. **Node.js**（LTS 版）を入れる: https://nodejs.org/ja からインストーラーをダウンロードして実行する。
   ターミナルで `node --version` と打って、`v22` 以上が表示されれば OK。
2. このリポジトリを Mac に取得し、`server` フォルダで次を実行する:
   ```sh
   cd server
   npm install
   npm test        # すべてのテストが通ることを確認
   ```

## 2. Cloudflare に置く（機器が届く前にできる）

1. Cloudflare のアカウントを作る（無料）: https://dash.cloudflare.com/sign-up
2. ターミナルから Cloudflare にログインする（ブラウザが開く）:
   ```sh
   npx wrangler login
   ```
3. データベースを作る（`apac` = アジア太平洋に置く）:
   ```sh
   npx wrangler d1 create bunkoko --location apac
   ```
   表示された `database_id` を、`wrangler.jsonc` の `"database_id"` の値（`00000000-…`）と置き換える。
4. データベースに表を作る:
   ```sh
   npm run db:migrate:remote
   ```
5. サーバーを公開する:
   ```sh
   npm run deploy
   ```
   最後に表示される `https://bunkoko-server.<あなたの名前>.workers.dev` が、このサーバーの URL。
6. 合言葉を4つ作って登録する。ターミナルで `openssl rand -hex 24` を4回実行し、出てきた文字列をそれぞれ使う:
   ```sh
   npx wrangler secret put WEBHOOK_SECRET   # 1つ目: SwitchBot の Webhook の URL に入れる
   npx wrangler secret put ADMIN_TOKEN      # 2つ目: あなたが動作確認に使う
   npx wrangler secret put APP_TOKEN        # 3つ目: iPhone アプリに設定する
   npx wrangler secret put AI_TASK_TOKEN    # 4つ目: Claude Code のタスクに設定する（8章）
   ```
   **どれもパスワードと同じ扱い**。パスワード管理アプリなどに保存し、人に見せない。

この時点では SwitchBot のトークンが未登録なので、5分ごとの取得はエラーになる（問題ない）。

## 3. SwitchBot とつなぐ（機器が届いたら）

1. SwitchBot アプリで、ハブ3 → 各センサーの順に登録する。各機器の設定で「クラウドサービス」をオンにする。
2. トークンとシークレットを発行する（アプリのバージョン 9.0 以降）:
   「プロフィール」→「設定」→「アプリについて」→「アプリバージョン」を **10回タップ** →「開発者向けオプション」→ トークンとシークレットが表示される
3. サーバーに登録する:
   ```sh
   npx wrangler secret put SWITCHBOT_TOKEN
   npx wrangler secret put SWITCHBOT_SECRET
   ```
4. 手元のコマンドでも使えるように、`server/.dev.vars` を作る:
   ```sh
   cp .dev.vars.example .dev.vars
   ```
   `.dev.vars` をテキストエディタで開き、トークン・シークレット・2つの合言葉を書き込む（このファイルは Git に含まれない）。
5. トークンが正しいか確かめる（機器の一覧が出れば OK）:
   ```sh
   npm run switchbot -- devices
   ```
6. **Webhook を登録する**（在床・開閉の変化を正確な時刻で受け取るため）。`<URL>` は手順 2-5 の URL、`<WEBHOOK_SECRET>` は手順 2-6 の1つ目の文字列:
   ```sh
   npm run switchbot -- webhook-setup https://<URL>/webhooks/switchbot/<WEBHOOK_SECRET>
   npm run switchbot -- webhook-query    # 登録した URL が表示されれば OK
   ```
7. すぐに1回取得してみる（`<ADMIN_TOKEN>` は手順 2-6 の2つ目の文字列）:
   ```sh
   curl -X POST "https://<URL>/admin/collect?refresh=1" -H "Authorization: Bearer <ADMIN_TOKEN>"
   ```
   `"devicesOk": 9, "devicesTotal": 9` のように表示されれば完了。以降は5分ごとに自動で記録される。

## 4. 動いているか確かめる

```sh
curl "https://<URL>/admin/summary" -H "Authorization: Bearer <ADMIN_TOKEN>"
```

| 見る項目 | 意味 |
|---|---|
| `devices` | 機器ごとの最新の値と、最後に取得した時刻（`lastPolledAt`） |
| `recentRuns` | 直近5回の取得結果。`errors` にオフラインの機器などが出る |
| `webhook.eventsReceived` | 届いた Webhook の数 |
| `switchBotApiCallsLast24h` | 直近24時間の SwitchBot API の利用回数（上限は1日 10,000回。9台なら約2,600回） |

計算した 1日 = 1行の表を見る（iPhone アプリからデータが届くようになってから）:
```sh
curl "https://<URL>/admin/days?from=2026-12-01&to=2026-12-07" -H "Authorization: Bearer <ADMIN_TOKEN>"
# 計算方法を変えたときなどに、期間の日をすべて計算し直す
curl -X POST "https://<URL>/admin/days/requeue?from=2026-12-01&to=2026-12-07" -H "Authorization: Bearer <ADMIN_TOKEN>"
curl -X POST "https://<URL>/admin/days/process?max=10" -H "Authorization: Bearer <ADMIN_TOKEN>"
```
計算待ちの日は、5分ごとの実行のついでに4日分ずつ計算される。

ログを見る: `npx wrangler tail`（またはCloudflare のダッシュボードの Workers → bunkoko-server → ログ）

よくあるエラー:
| エラー | 原因と対処 |
|---|---|
| `HTTP 401` | トークン・シークレットの誤り、または1日の上限超え。`npm run switchbot -- devices` で確かめる |
| `statusCode 161` / `171` | 機器 / ハブがオフライン。モバイル Wi-Fi の電源・スリープ設定を確認する |
| `Exceeded CPU` | 無料プランの処理時間の上限を超えた。続くようなら Workers の有料プラン（月 5ドル）にする |

## 5. データを取り出す

CSV（最大7日分。日本時間の列付き。Numbers や Excel で開ける）:
```sh
curl -o readings.csv "https://<URL>/admin/readings.csv?from=2026-12-01&to=2026-12-08" \
  -H "Authorization: Bearer <ADMIN_TOKEN>"
```
- `from` / `to` は日付（日本時間の0時）か ISO 形式の日時。省略すると直近24時間
- `&metric=CO2` で項目を、`&device=<deviceId>` で機器を絞り込める

データベースを丸ごと保存する（バックアップ）:
```sh
npx wrangler d1 export bunkoko --remote --output backup.sql
```

## 6. 機器がなくても手元で試す

偽の SwitchBot サーバーを使って、Mac の中だけで一通り動かせる。

```sh
cp .dev.vars.example .dev.vars
# .dev.vars に、適当な値を6つと、次の1行を書く:
#   SWITCHBOT_API_BASE=http://127.0.0.1:8788
npm run db:migrate:local
npm run mock:switchbot     # ターミナル1: 偽の SwitchBot サーバー
npm run dev                # ターミナル2: このサーバー（http://localhost:8787）
```
ターミナル3で:
```sh
curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"     # 5分ごとの取得を1回動かす
curl "http://localhost:8787/admin/summary" -H "Authorization: Bearer <.dev.vars の ADMIN_TOKEN>"
```
本物につなぐときは、`.dev.vars` から `SWITCHBOT_API_BASE` の行を消す。

---

## 7. 仕組み

```
SwitchBot の機器 ─Bluetooth→ ハブ3 ─Wi-Fi→ SwitchBot のクラウド
                                               │
                    5分ごとに取りに行く ←──────┤（API: 1日 約2,600回）
                    変化したら届く      ←──────┘（Webhook）
                               │
                  Cloudflare Workers（このサーバー）→ D1 に保存
```

| ファイル | 役割 |
|---|---|
| `src/index.ts` | 入り口。5分ごとの実行と、URL ごとの振り分け |
| `src/collector.ts` | 全機器の状態を取得して保存する。機器一覧は1時間に1回だけ取り直す |
| `src/switchbot.ts` | SwitchBot API v1.1 のクライアント（署名の作成を含む） |
| `src/normalize.ts` | 応答を「項目ごとの値」に変換する。機器の種類ごとの対応表は持たず、新しい機器にもそのまま対応する |
| `src/webhook.ts` | Webhook を受け取って保存する |
| `src/admin.ts` | 動作確認用の管理 API（summary・CSV） |
| `src/db.ts` | センサーの値のデータベースへの読み書き |
| `src/api.ts` / `src/ingest.ts` / `src/settings.ts` | iPhone アプリ向けの API（HealthKit のデータ・記録の受け取り、設定） |
| `src/days/` | 1日 = 1行の表の計算（`sleep.ts` が睡眠の区切り方、`compute.ts` が各値、`queue.ts` が計算待ちの処理） |
| `src/roles.ts` | 機器の役割（どの機器の値を室温・在床などに使うか） |
| `src/ai.ts` / `src/meals.ts` | 食事の目安の受け渡し |
| `ai/meal-estimation-task.md` | Claude Code のスケジュールされたタスクへの指示書 |
| `migrations/` | データベースの表の定義 |
| `scripts/switchbot-cli.ts` | 手元から SwitchBot API を試すコマンド |
| `scripts/mock-switchbot.ts` | 手元で試すための偽の SwitchBot サーバー |

データベースの表:
| 表 | 中身 |
|---|---|
| `readings` | センサーの値。1行 = 機器・時刻・項目（例: CO2センサーの 22:00 の `CO2` = 1203）。`source` は `poll`（5分ごと）か `webhook` |
| `devices` | SwitchBot に登録されている機器 |
| `webhook_events` | 届いた Webhook の生データ |
| `collector_runs` | 5分ごとの取得の実行記録（データの欠けや API の利用回数の確認用） |
| `health_samples` / `health_buckets` / `workouts` | iPhone アプリから届いた HealthKit のデータ |
| `user_logs` / `meal_estimates` | 食事・朝の体感・入浴などの記録と、食事の目安 |
| `days` / `day_queue` | 1日 = 1行の表と、計算し直す日 |
| `user_settings` / `apns_devices` / `notifications_sent` | 設定、通知の送り先、送った通知 |

時刻はすべて UTC のエポックミリ秒で保存している（表示するときに日本時間にする）。

### 安全のための決まり
- トークン・シークレット・合言葉は `wrangler secret` と `.dev.vars` にだけ置き、Git には含めない。
- SwitchBot の Webhook には署名がないため、URL に長い合言葉を含めて、合言葉が違う送信は「存在しない URL」として扱う。
- 合言葉は使う相手ごとに分け、使える API を絞っている（管理: `ADMIN_TOKEN` で `/admin/*`、アプリ: `APP_TOKEN` で `/api/*`、Claude Code のタスク: `AI_TASK_TOKEN` で `/ai/*`）。

### 費用と上限
2026年11月時点の Cloudflare の無料枠の目安（変わることがあるので公式で確認: https://developers.cloudflare.com/workers/platform/pricing/ 、 https://developers.cloudflare.com/d1/platform/pricing/ ）。

| 項目 | 無料枠 | この構成での使用量（9台） |
|---|---|---|
| Workers の実行 | 1日 10万回 | 約300回（5分ごと＋Webhook） |
| D1 の書き込み | 1日 10万行 | 約2万行 |
| D1 の保存容量 | 5GB | 1年で約0.4GB |

---

## 8. 食事の目安（Claude Code のスケジュールされたタスク）

食事の料理名から、睡眠の分析に使う目安（カロリー・脂っこさ・辛さ・量など）を、毎朝 Claude Code に出してもらう。
Claude のサブスクリプションの使用量の範囲で動き、API の別料金はかからない。サーバーを公開したあとに1回だけ設定する。

**この会話で Claude に「食事の目安のタスクを設定して」と頼めば、下の手順を一緒に進められる**（合言葉は、表示される入力欄にあなたが入れる。チャットには貼らない）。

自分で設定する場合:
1. サーバーに `AI_TASK_TOKEN` を登録しておく（2章の手順 6）。
2. Claude Code（ https://claude.ai/code ）で、このタスク用のクラウド環境を用意する。環境の設定（セッションのタイトルバーの環境のメニュー →「編集」）で:
   - **環境変数**: `BUNKOKO_URL=https://<URL>`（2章の手順 5 の URL）
   - **ネットワークシークレット**: ホストに `<URL のホスト名>`（`bunkoko-server.<あなたの名前>.workers.dev`）、ヘッダーに `Authorization`、値に `Bearer <AI_TASK_TOKEN>`。保存すると、そのホストへの通信も許可される
3. スケジュールされたタスクを作る:
   - 時刻: 毎朝 5:30（日本時間）。朝のレポートの前に終わらせるため
   - リポジトリ: このリポジトリ
   - 環境: 手順 2 の環境
   - 指示: `server/ai/meal-estimation-task.md の手順に従って、食事の目安を出してください。`
4. タスクを今すぐ1回動かして、報告に件数が出れば完了。アプリの食事の一覧で、目安が付いていることを確かめる。

指示書（`ai/meal-estimation-task.md`）では、Claude ができることを「目安がまだの食事を読む」「目安を書き込む」の2つの API だけに絞っている。料理名の文字に何が書かれていても、指示としては扱わない。

