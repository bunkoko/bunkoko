# 通知のボタンで記録する Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 朝の体感と夜のふり返りのお知らせに 1〜5 のボタンを付け、iPhone・Apple Watch に届いた通知からその場で記録できるようにする。

**Architecture:** iPhone アプリのローカル通知に通知カテゴリ（ボタンの組）を付ける。ボタンの答えは iPhone のアプリが裏で受け取り（`UNUserNotificationCenterDelegate`）、アプリの画面と同じ `Recorder` で記録する。夜は「調子」に答えたら、すぐに「ストレス」の通知を出す（2段）。

**Tech Stack:** Swift 5（SwiftUI アプリ、iOS 26）、UserNotifications、XcodeGen。

**Spec:** `docs/superpowers/specs/2026-11-27-notification-buttons-design.md`

## Global Constraints
- iOS アプリには単体テストのターゲットがなく、この作業環境には Swift がない。各タスクの確認は、push で動く GitHub の Mac でのビルド（`.github/workflows/ios-build.yml`、Paid と Free の両方）と、最後の実機での確認で行う。
- 無料の Apple ID（`KENKO_EDITION = Free`）のままで動くこと。サーバーからの通知（APNs）には手を入れない。
- Watch のアプリは作らない。
- ボタンの文字（そのまま使う）:
  - いちばん上（全種類共通、何も記録しない）: `あとで入れる`
  - 朝: `1（ぐったり）` `2` `3` `4` `5（すっきり）`
  - 夜の1段目（調子）: `1（悪い）` `2` `3` `4` `5（よい）`
  - 夜の2段目（ストレス）: `1（なかった）` `2` `3` `4` `5（とても強かった）`

## Review Focus
1. **前の日の朝の通知を、次の日に押す** → 記録しない（体感はその朝のものだけ）。Task 2 の判定で守る。
2. **夜の通知に、0時を過ぎてから答える** → 通知に入れた日付（その日）のふり返りになる。今日の日付で入れない。Task 2。
3. **Watch のダブルタップ** → いちばん上の［あとで入れる］が押され、何も記録されない。Task 1 の並び順で守る。
4. **アプリが閉じた状態でボタンを押す**（裏で起動される）→ 通知の種類が登録され、記録と送信が終わってから処理を返す。Task 1（起動時の登録）と Task 2（`async` の受け取り）。
5. **アプリで調子だけ入れたあとに、通知でストレスに答える（またはその逆）** → もう片方の値は消えない。ストレスがもう入っていれば、2段目は出さない。Task 2。

自動のテストで押さえられないので、Task 3 の実機での確認で、1〜5 を順に試す。

---

### Task 1: ボタン付きの通知を出す

**Files:**
- Modify: `ios/App/Sync/Reminders.swift`
- Modify: `ios/App/KenkoKanriApp.swift`（`AppDelegate.application(_:didFinishLaunchingWithOptions:)`）

**Interfaces:**
- Produces:
  - `enum ReminderStep: String, CaseIterable { case morning, condition, stress }`（`Reminders.swift`）。`categoryIdentifier: String` は `"reminder.\(rawValue)"`。
  - `static let laterAction = "later"`、`static func scoreAction(_ score: Int) -> String`（`"score.\(score)"`）、`static func score(fromAction id: String) -> Int?`（1〜5 以外は nil）。
  - `static var categories: Set<UNNotificationCategory>`：3種類。ボタンは［あとで入れる］、1〜5 の順。オプションはどれも空（裏で処理。`.foreground`・`.destructive` にしない）。
  - `static func content(for step: ReminderStep, day: String) -> UNMutableNotificationContent`：`categoryIdentifier` と `userInfo["day"] = day` を入れる。
  - 文言: 朝 title `おはようございます` / body `起きたときの感じを 1（ぐったり）〜5（すっきり）で選んでください。iPhone では通知を長押しすると選べます`。調子 title `今日のふり返り（1/2）` / body `今日の調子を 1（悪い）〜5（よい）で選んでください`。ストレス title `今日のふり返り（2/2）` / body `今日のストレスを 1（なかった）〜5（とても強かった）で選んでください`。

- [ ] **Step 1:** `Reminders.swift` に上の型・定数・`categories`・`content(for:day:)` を足し、`refresh()` の朝と夜の予約を `content(for: .morning, day: day)`・`content(for: .condition, day: day)` で作るように変える（予約の時刻・条件・id は今のまま）。
- [ ] **Step 2:** `AppDelegate` の起動時に `UNUserNotificationCenter.current().setNotificationCategories(Reminders.categories)` を呼ぶ（delegate を設定する行の直後）。
- [ ] **Step 3:** commit して push し、`iOS build` が Paid・Free とも成功することを確かめる（`mcp__github__actions_list` で `ios-build.yml` の最新の実行が `success`）。

```bash
git add ios/App/Sync/Reminders.swift ios/App/KenkoKanriApp.swift
git commit -m "iOS: put score buttons on the morning and evening reminders"
```

### Task 2: ボタンの答えを記録する

**Files:**
- Modify: `ios/App/Sync/Reminders.swift`
- Modify: `ios/App/KenkoKanriApp.swift`（`AppDelegate`）
- Modify: `docs/analysis.md`（2.7 の「入れ忘れを減らす」）、`docs/requirements.md`（FR-117）、`ios/README.md`（記録のリマインダーの説明）、`docs/phase1-design.md`（版の表）

**Interfaces:**
- Consumes: Task 1 の `ReminderStep`・`score(fromAction:)`・`content(for:day:)`。`Recorder.morningFeeling(_:at:)`、`Recorder.dayCheckin(day:condition:stress:)`、`LogStore.shared.log(id:)`、`Recorder.checkinID(day:)`、`Fmt.dateString(_:)`。
- Produces:
  - `static func handle(_ response: UNNotificationResponse) async`（`Reminders.swift`）。
  - `AppDelegate.userNotificationCenter(_:didReceive:) async`（中で `await Reminders.handle(response)`）。

- [ ] **Step 1:** `handle(_:)` を作る。決まり:
  - 点数のボタン以外（［あとで入れる］、通知そのものを押した、閉じた）は何もしない。
  - 日付は `response.notification.request.content.userInfo["day"]`（なければ何もしない）。
  - 朝: その日付が `Fmt.dateString(Date())` と同じときだけ `Recorder.morningFeeling(score)`。
  - 調子: その日のふり返り（`LogStore.shared.log(id: Recorder.checkinID(day:))?.data`）のストレスを残して `Recorder.dayCheckin(day:condition: score, stress: 今の値)`。そのあと、ストレスがまだ入っていなければ、`content(for: .stress, day:)` の通知を、すぐに出す（`trigger: nil`、id は `"reminder.stress.\(day)"`）。
  - ストレス: 調子を残して `Recorder.dayCheckin(day:condition: 今の値, stress: score)`。
- [ ] **Step 2:** `AppDelegate` に `userNotificationCenter(_:didReceive:) async` を足す。
- [ ] **Step 3:** 文書を直す。2.7 と FR-117 に「通知のボタンから、その場で答えられる（iPhone がロック中なら Apple Watch でも）。夜は調子とストレスの2段」。`ios/README.md` の記録のリマインダーの説明にも同じこと。`phase1-design.md` の版の表に v0.9。
- [ ] **Step 4:** commit して push し、`iOS build` が成功することを確かめる。

```bash
git add ios/App/Sync/Reminders.swift ios/App/KenkoKanriApp.swift docs/analysis.md docs/requirements.md ios/README.md docs/phase1-design.md
git commit -m "iOS: record the reminder answers from their buttons"
```

### Task 3: 実機で確かめる（利用者と一緒に）

- [ ] **Step 1:** 利用者に、`git pull` と Xcode の ▶ でアプリを入れ直してもらう（`xcodegen` は不要）。アプリを一度開くと、予約が新しい通知に置きかわる。
- [ ] **Step 2:** 今夜の夜のお知らせ（目標の就寝時刻の1時間前）を、iPhone をロックした状態で Watch で受けてもらう。確かめること: ボタンが6つとも出る／調子を押すと、すぐストレスの通知が来る／押した値が、翌朝の `npm run admin -- days`（Claude が読む）の調子・ストレスに入る。
- [ ] **Step 3:** 朝のお知らせは、体感を入れ忘れた朝に確かめる（Review Focus の 1 は、前の日の通知が残っていれば試す）。
- [ ] **Step 4:** Watch でボタンが6つ出なければ、出た数を聞いて、並べ方を見直す。
