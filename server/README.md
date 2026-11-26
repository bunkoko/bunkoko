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
9. [通知（寝る前の確認・朝のレポート）](#9-通知寝る前の確認朝のレポート)

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
6. 合言葉を4つ作って登録する:
   ```sh
   npm run secrets
   ```
   4つの合言葉（`WEBHOOK_SECRET`・`ADMIN_TOKEN`・`APP_TOKEN`・`AI_TASK_TOKEN`）をランダムに作って `server/.dev.vars` に保存し、Cloudflare に登録する。何度実行しても、一度作った合言葉は変わらない。
   - `WEBHOOK_SECRET`: SwitchBot の Webhook の URL に入れる（3章）
   - `ADMIN_TOKEN`: あなたが動作確認に使う（4章）
   - `APP_TOKEN`: iPhone アプリに設定する。`npm run secrets -- --copy APP_TOKEN` でクリップボードにコピーし、iPhone に貼り付ける（Mac と iPhone が同じ Apple ID なら、Mac でコピーしたものを iPhone で貼り付けられる）
   - `AI_TASK_TOKEN`: Claude Code のタスクに設定する（8章）

   **どれもパスワードと同じ扱い**。人に見せない。`.dev.vars` は Git に含まれない。消すと分からなくなるので、パスワード管理アプリなどにも控えておくとよい。

この時点では SwitchBot のトークンが未登録なので、5分ごとの取得はエラーになる（問題ない）。

## 3. SwitchBot とつなぐ（機器が届いたら）

1. SwitchBot アプリで、ハブ3 → 各センサーの順に登録する。各機器の設定で「クラウドサービス」をオンにする。
   **名前の付け方**（このツールは、機器の種類と名前から役割を決める）:
   - 開閉センサー: 窓に付けたものは名前に「**窓**」、ドアに付けたものは「**ドア**」を入れる
   - プラグミニ: 加湿空気清浄機につないだものは名前に「**加湿**」、テレビにつないだものは「**テレビ**」を入れる
   - ほかの機器（ハブ3・CO2センサー・人感センサー Pro・シーリングライト・カーテン）は、種類で決まるので名前は自由
2. サーバーの URL を、手元のコマンド用に保存する（秘密ではない値なので、Cloudflare には登録しない）:
   ```sh
   npm run secrets -- --set SERVER_URL https://bunkoko-server.<あなたの名前>.workers.dev
   ```
3. トークンとシークレットを登録する（アプリのバージョン 9.0 以降）:
   「プロフィール」→「設定」→「アプリについて」→「アプリバージョン」を **10回タップ** →「開発者向けオプション」で、トークンとシークレットが出る。
   1つずつコピーして、Mac で実行する（クリップボードから読み取って登録する。値は画面に出ない）:
   ```sh
   npm run secrets -- --paste SWITCHBOT_TOKEN    # トークンをコピーしてから
   npm run secrets -- --paste SWITCHBOT_SECRET   # シークレットをコピーしてから
   ```
4. トークンが正しいか、名前から役割が決まったかを確かめる:
   ```sh
   npm run switchbot -- devices
   ```
   「役割なし（名前を見直す）」と出た機器は、SwitchBot アプリで名前を直してから、もう一度実行する。
5. **Webhook を登録する**（在床・開閉の変化を正確な時刻で受け取るため）。送り先は `.dev.vars` の SERVER_URL と WEBHOOK_SECRET から作る（合言葉は画面に出ない）:
   ```sh
   npm run switchbot -- webhook-setup
   npm run switchbot -- webhook-query    # 登録した送り先が出れば OK（合言葉の部分は <WEBHOOK_SECRET> と出る）
   ```
6. すぐに1回取得して、結果を見る:
   ```sh
   npm run admin -- collect
   npm run admin -- summary
   ```
   `collect` で `"devicesOk": 9, "devicesTotal": 9` のように出れば完了。以降は5分ごとに自動で記録される。`summary` で、機器ごとの役割と最新の値が見られる。

## 4. 動いているか確かめる

```sh
npm run admin -- summary     # 機器ごとの役割と最新の値、直近の取得結果、Webhook の数、API の利用回数
npm run admin -- days        # 1日 = 1行の表の要点（直近7日。期間は npm run admin -- days 2026-12-01 2026-12-07）
npm run admin -- night       # 昨夜の様子を15分ごとの表で（別の夜は npm run admin -- night 2026-12-01。日付は起きた日）
```

`summary`・`collect`・`requeue`・`process` は `ADMIN_TOKEN` で管理用の API を使う。`days`・`night`・`analyze` は、Claude と同じ読むだけの API（`/ai/*`）を `AI_TASK_TOKEN` で読む（どちらも `npm run secrets` で `.dev.vars` に入っている）。

`night` の表は、寝る2時間前から起きた1時間後まで。明るさは人感センサーの値（1〜20）の幅、布団と心拍は15分の中で記録があった数（5分ごとなので最大3）。CO2・室温は平均。布団に入った・寝ついた・起きた時刻と、部屋が暗くなった・窓を開けたなどの出来事を、その15分の下に出す。心拍の記録が少ない夜（使えない夜）に、どこが抜けたかを見るのにも使う。

`curl` で直接見るときは（`<ADMIN_TOKEN>` は `.dev.vars` の値）:
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
# 計算方法を変えたときなどに、期間の日をすべて計算し直す（最大400日）
npm run admin -- requeue 2026-12-01 2026-12-07
npm run admin -- process 10
```
計算待ちの日は、5分ごとの実行のついでに4日分ずつ計算される。

夜を条件で2つに分けて、睡眠や翌日の様子を比べる（[分析の設計](../docs/analysis.md) の5章②。いまは Apple Watch のデータで調べられる H5・H6・H8・H9）:
```sh
npm run admin -- analyze                         # 直近1年
npm run admin -- analyze 2026-06-01 2026-11-30   # 期間を決める
```
どの比較も、グループごとに7夜以上そろわなければ「データ不足」。差の範囲（ブートストラップ法）が0をまたげば「差ははっきりしない」、データの前半と後半で同じ向きの差なら「気づきの候補」と出す（7章の決まり）。体調がいつもと違う日と、使えない夜は外す。朝の体感・日中の調子は、記録が7夜以上たまった比較から出る。

ログを見る: `npx wrangler tail`（またはCloudflare のダッシュボードの Workers → bunkoko-server → ログ）

よくあるエラー:
| エラー | 原因と対処 |
|---|---|
| `HTTP 401` | トークン・シークレットの誤り、または1日の上限超え。`npm run switchbot -- devices` で確かめる |
| `statusCode 161` / `171` | 機器 / ハブがオフライン。モバイル Wi-Fi の電源・スリープ設定を確認する |
| `Exceeded CPU` | 無料プランの処理時間の上限を超えた。続くようなら Workers の有料プラン（月 5ドル）にする |
| `HTTP 500`・`Worker threw exception`（1101）がすべての API で出る | データベース（D1）の無料プランの1日の上限（読み取り 500万行・書き込み 10万行）を超えた可能性が高い。日本時間の朝9時（UTC の0時）に戻る。`npx wrangler d1 info bunkoko` で直近24時間の利用量を見られる。2026-11-24 に、1日 = 1行の計算が毎回すべての心拍を読んでいた不具合を直した（`test/query-plans.test.ts` で、全件を読む SQL がないかを確かめている） |

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
- 合言葉は使う相手ごとに分け、使える API を絞っている（管理: `ADMIN_TOKEN` で `/admin/*`、アプリ: `APP_TOKEN` で `/api/*`、Claude Code: `AI_TASK_TOKEN` で `/ai/*`。Claude ができるのは、食事の目安を読み書きすることと、1日 = 1行の表・夜の詳細を読むことだけ）。

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

同じ環境（ネットワークシークレット）があれば、Claude Code の会話からも、1日 = 1行の表と夜の詳細を読める（`GET /ai/days`・`GET /ai/nights/:date/timeline`。読むだけ）。`npm run admin -- days` などを、あなたが貼らなくても Claude が自分で実行できる:
```sh
# URL は環境変数 BUNKOKO_URL から。合言葉はネットワークシークレットで付くので、AI_TASK_TOKEN はダミーでよい。
# クラウドの通信は中継（プロキシ）を通るので、Node の fetch に使わせる（NODE_USE_ENV_PROXY=1）
NODE_USE_ENV_PROXY=1 AI_TASK_TOKEN=proxy-injected npm run --silent admin -- days
```

---

## 9. 通知（寝る前の確認・朝のレポート）

Apple Developer Program に登録したあとに設定する。iPhone アプリができて、通知を許可してから試す。

1. 通知用の鍵を作る: Apple Developer のサイト（ https://developer.apple.com/account ）→「Certificates, Identifiers & Profiles」→「Keys」→「+」→「Apple Push Notifications service (APNs)」にチェックして作成する。
   - `.p8` ファイルをダウンロードする（**1回しかダウンロードできない**。パスワードと同じ扱いで保管する）
   - 表示される **Key ID** と、「Membership」に出ている **Team ID** を控える
2. サーバーに登録する（`AuthKey_XXXXXXXXXX.p8` はダウンロードしたファイル名）:
   ```sh
   npx wrangler secret put APNS_KEY_P8 < AuthKey_XXXXXXXXXX.p8
   npx wrangler secret put APNS_KEY_ID      # Key ID
   npx wrangler secret put APNS_TEAM_ID     # Team ID
   npx wrangler secret put APNS_BUNDLE_ID   # アプリの ID（例: com.<あなた>.bunkoko）
   ```
3. 届くか試す:
   ```sh
   curl -X POST "https://<URL>/admin/notify/test" -H "Authorization: Bearer <ADMIN_TOKEN>"
   ```
   `"delivered": 1` のように表示され、iPhone に「通知のテスト」が届けば完了。

- Cloudflare Workers から Apple の通知の仕組みに送れるかは、この手順で初めて確かめられる（この作業環境からは Apple のサーバーに通信できず、確かめられていない）。届かない場合は、表示される `failed` の `reason` を Claude に伝える。
- 通知は5分ごとの実行の中で送る。寝る前の確認は目標の就寝時刻の60分前（設定で変えられる）、朝のレポートは起きた20分後以降に1回だけ。部屋に問題がなければ、寝る前の確認は送らない。

