-- Phase 1: iPhone アプリから受け取るデータと、1日 = 1行の表
-- 時刻はすべて UTC のエポックミリ秒。日付（date）は利用者のタイムゾーンでの YYYY-MM-DD。

-- HealthKit の測った値（uuid ごと）
CREATE TABLE health_samples (
  user_id        TEXT    NOT NULL,
  uuid           TEXT    NOT NULL,
  type           TEXT    NOT NULL,
  start_at       INTEGER NOT NULL,
  end_at         INTEGER NOT NULL,
  value_num      REAL,
  value_text     TEXT,
  unit           TEXT,
  source_name    TEXT,
  source_product TEXT,
  meta_json      TEXT,
  deleted        INTEGER NOT NULL DEFAULT 0,
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY (user_id, uuid)
) WITHOUT ROWID;

CREATE INDEX idx_health_samples_type_time ON health_samples (user_id, type, start_at);

-- 5分・1時間ごとの集計（心拍数・歩数など）。同じ時間を送り直したら上書きする
CREATE TABLE health_buckets (
  user_id        TEXT    NOT NULL,
  type           TEXT    NOT NULL,
  bucket_minutes INTEGER NOT NULL,
  bucket_start   INTEGER NOT NULL,
  sum            REAL,
  avg            REAL,
  min            REAL,
  max            REAL,
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY (user_id, type, bucket_minutes, bucket_start)
) WITHOUT ROWID;

-- ワークアウトの概要
CREATE TABLE workouts (
  user_id        TEXT    NOT NULL,
  uuid           TEXT    NOT NULL,
  activity_type  TEXT    NOT NULL,
  start_at       INTEGER NOT NULL,
  end_at         INTEGER NOT NULL,
  energy_kcal    REAL,
  distance_m     REAL,
  avg_heart_rate REAL,
  effort         REAL,
  source_name    TEXT,
  deleted        INTEGER NOT NULL DEFAULT 0,
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY (user_id, uuid)
) WITHOUT ROWID;

CREATE INDEX idx_workouts_time ON workouts (user_id, start_at);

-- 手入力・ショートカット・スクリーンタイムの記録
CREATE TABLE user_logs (
  user_id     TEXT    NOT NULL,
  id          TEXT    NOT NULL,
  kind        TEXT    NOT NULL,
  occurred_at INTEGER NOT NULL,
  data_json   TEXT    NOT NULL,
  deleted     INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (user_id, id)
) WITHOUT ROWID;

CREATE INDEX idx_user_logs_kind_time ON user_logs (user_id, kind, occurred_at);

-- 食事の目安。source = 'ai'（生成AI）か 'user'（利用者が直したもの）。'user' があればそちらを使う
CREATE TABLE meal_estimates (
  user_id       TEXT    NOT NULL,
  log_id        TEXT    NOT NULL,
  source        TEXT    NOT NULL CHECK (source IN ('ai', 'user')),
  model         TEXT,
  estimate_json TEXT    NOT NULL,
  estimated_at  INTEGER NOT NULL,
  PRIMARY KEY (user_id, log_id, source)
) WITHOUT ROWID;

-- 設定（JSON）
CREATE TABLE user_settings (
  user_id       TEXT    NOT NULL PRIMARY KEY,
  settings_json TEXT    NOT NULL,
  updated_at    INTEGER NOT NULL
);

-- 通知の送り先（iPhone のデバイストークン）
CREATE TABLE apns_devices (
  user_id     TEXT    NOT NULL,
  token       TEXT    NOT NULL,
  environment TEXT    NOT NULL CHECK (environment IN ('sandbox', 'production')),
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (user_id, token)
) WITHOUT ROWID;

-- 計算し直す必要がある日
CREATE TABLE day_queue (
  user_id   TEXT    NOT NULL,
  date      TEXT    NOT NULL,
  queued_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, date)
) WITHOUT ROWID;

-- 1日 = 1行の表。日付は起きた日。値は JSON で持つ
CREATE TABLE days (
  user_id      TEXT    NOT NULL,
  date         TEXT    NOT NULL,
  algo_version INTEGER NOT NULL,
  computed_at  INTEGER NOT NULL,
  sleep_start  INTEGER,
  sleep_end    INTEGER,
  metrics_json TEXT    NOT NULL,
  PRIMARY KEY (user_id, date)
) WITHOUT ROWID;

-- 送った通知（同じ通知を1日に2回送らないため）
CREATE TABLE notifications_sent (
  user_id TEXT    NOT NULL,
  kind    TEXT    NOT NULL,
  date    TEXT    NOT NULL,
  sent_at INTEGER NOT NULL,
  body    TEXT,
  PRIMARY KEY (user_id, kind, date)
) WITHOUT ROWID;
