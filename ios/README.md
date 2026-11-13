# 健康管理（iPhone アプリ）

睡眠と部屋の環境を見える化する iPhone アプリ。サーバー（[../server](../server/)）とやり取りする。

| 画面 | できること |
|---|---|
| ホーム | 昨夜の要点、朝の体感の入力、今の部屋の状態と助言、就寝まであと何分か |
| 夜 | 夜ごとの一覧と、睡眠ステージ・心拍・CO2・室温・明るさを同じ時間軸で見るグラフ、出来事（食事・入浴・窓・消灯・スマホ） |
| 推移 | 睡眠時間・効率・寝つき・CO2・室温・安静時心拍・HRV・朝の体感の日ごとのグラフ |
| 記録 | 食事（料理名を書くだけ）、入浴・カフェイン・お酒のワンタップ記録、食事の目安の確認と修正 |
| 設定 | 目標の就寝・起床時刻、通知、ヘルスケア・スクリーンタイムの許可、サーバーの接続 |

裏側で動くもの:
- HealthKit のデータを、新しく記録されるたびにサーバーへ送る（初回は過去1年分）
- スクリーンタイムの節目（夜にスマホを使った時刻）を記録する拡張機能
- ショートカットから呼べる「充電を記録」

---

## 1. 準備（1回だけ）

1. **Xcode** と **XcodeGen** を入れる（済み）。
2. **Apple Developer Program**: 登録していなくても、無料の Apple ID で iPhone に入れられる（`KENKO_EDITION = Free`）。ただし無料のあいだは:
   - 通知（寝る前の確認・朝のレポート）とスクリーンタイムは使えない（Apple の決まりで、無料のチームでは使えないため）
   - 7日ごとに、Xcode からもう一度入れ直す必要がある（入れ直してもデータは消えない）

   登録したら `KENKO_EDITION = Paid` に変えて、ビルドし直す。
3. **自分用の設定ファイルを作る**（`ios` フォルダで）:
   ```sh
   cp Config/Local.xcconfig.example Config/Local.xcconfig
   ```
   `Config/Local.xcconfig` をテキストエディタで開き、2つを書き換える:
   - `DEVELOPMENT_TEAM`: Apple Developer のチーム ID（ https://developer.apple.com/account の「Membership」に出る10文字の英数字）
   - `APP_BUNDLE_ID`: アプリの ID。世界で1つになるように、自分の名前などを入れる（例: `com.taro.kenkokanri`）。**サーバーの `APNS_BUNDLE_ID` と同じにする**（server/README.md の9章）
   - `KENKO_EDITION`: 無料の Apple ID なら `Free`、Apple Developer Program に登録済みなら `Paid`

## 2. ビルドして iPhone に入れる

1. Xcode のプロジェクトを作って開く:
   ```sh
   cd ios
   xcodegen
   open KenkoKanri.xcodeproj
   ```
   `project.yml` を変えたら、毎回 `xcodegen` を実行し直す（Xcode の画面で設定を変えても、`xcodegen` で上書きされるため）。
2. iPhone を Mac にケーブルでつなぐ。初めてのときは、iPhone の「設定」→「プライバシーとセキュリティ」→「デベロッパモード」をオンにする（iPhone が再起動する）。
3. Xcode の上の方で、実行先にあなたの iPhone を選び、▶（Run）を押す。
4. iPhone にアプリが入って開いたら、「3. はじめの設定」へ。

**ビルドのエラーが出たら**: Xcode の左の「Issue navigator」（⚠ のアイコン）に出た赤いエラーを、まとめてコピーして Claude に貼ってほしい（エラーを選んで右クリック →「Copy」）。コンパイルのエラーは、GitHub の Mac（`.github/workflows/ios-build.yml`）でも、push するたびに確かめている。

## 3. はじめの設定（アプリの中で）

1. **サーバーの URL と APP_TOKEN** を入れて「保存して確かめる」（server/README.md の2章で作ったもの）。
2. 「設定」タブで:
   1. **ヘルスケアへのアクセスを許可** → すべての項目をオンにする。過去1年分の送信が始まる（数分〜数十分。アプリを開いたままにすると早い）
   2. **通知を許可する**
   3. **スクリーンタイムを許可** →「見張るアプリを選ぶ」で、**すべてのアプリとカテゴリ**を選ぶ
   4. 目標の就寝・起床時刻を合わせて「サーバーに保存」
3. **充電の記録**: ショートカットアプリ →「オートメーション」→「＋」→「充電器」で、2つ作る。
   - 「接続されたとき」→ アクション「充電を記録」（状態: つないだ）→「すぐに実行」
   - 「接続が解除されたとき」→ アクション「充電を記録」（状態: 外した）→「すぐに実行」

## 4. 構成

| 場所 | 中身 |
|---|---|
| `project.yml` | XcodeGen の設定（アプリと拡張機能、権限、Info.plist の中身） |
| `Config/` | アプリの ID・チーム ID（`Local.xcconfig` は Git に含めない） |
| `App/KenkoKanriApp.swift` | 入り口。起動時のバックグラウンド受け取りの登録、通知の登録 |
| `App/Health/` | HealthKit の読み取りと送信（`HealthTypes.swift` が読むデータの一覧、`HealthSync.swift` が送る仕組み） |
| `App/Sync/` | 記録の保存と送信（通信できないときは端末に残して送り直す） |
| `App/Networking/` | サーバーの API とデータの形 |
| `App/ScreenTime/` | スクリーンタイムの節目の登録 |
| `App/Intents/` | ショートカットから呼ぶ「充電を記録」 |
| `App/Views/` | 画面 |
| `ActivityMonitor/` | スクリーンタイムの節目を受け取る拡張機能 |
| `Shared/` | アプリと拡張機能の両方で使う（App Group でのデータの受け渡し） |

### 送るデータの細かさ
docs/phase1-design.md の 3.1 のとおり。測った値（睡眠ステージ・HRV など）はそのまま、心拍数は5分ごと、歩数などは1時間ごとに集計して送る。このアプリが HealthKit に書き込んだカフェイン・お酒は、記録の方で送っているので、HealthKit からは送らない。
