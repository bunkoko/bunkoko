-- 初期スキーマ。時刻はすべて UTC のエポックミリ秒で保存する。
-- user_id は将来の複数ユーザー対応のために最初から持たせる。

-- SwitchBot アカウントに登録されている機器
CREATE TABLE devices (
  user_id              TEXT    NOT NULL,
  device_id            TEXT    NOT NULL,
  device_name          TEXT,
  device_type          TEXT,
  hub_device_id        TEXT,
  enable_cloud_service INTEGER NOT NULL DEFAULT 1,
  -- 最新の機器一覧に含まれていれば 1。アカウントから外した機器は 0 になり、取得しない
  active               INTEGER NOT NULL DEFAULT 1,
  first_seen_at        INTEGER NOT NULL,
  last_seen_at         INTEGER NOT NULL,
  raw_json             TEXT    NOT NULL,
  PRIMARY KEY (user_id, device_id)
);

-- センサーの値（縦持ち）。1行 = 1台の機器の、ある時刻の、1つの項目
--   source = 'poll'    : 5分ごとの取得
--   source = 'webhook' : 機器の状態が変わったときに届いた通知
CREATE TABLE readings (
  user_id     TEXT    NOT NULL,
  device_id   TEXT    NOT NULL,
  measured_at INTEGER NOT NULL,
  metric      TEXT    NOT NULL,
  source      TEXT    NOT NULL CHECK (source IN ('poll', 'webhook')),
  value_num   REAL,
  value_text  TEXT,
  PRIMARY KEY (user_id, device_id, measured_at, metric, source)
) WITHOUT ROWID;

CREATE INDEX idx_readings_time ON readings (user_id, measured_at);

-- 届いた Webhook の生データ（形が変わっても後から読み直せるように保存する）
CREATE TABLE webhook_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     TEXT    NOT NULL,
  received_at INTEGER NOT NULL,
  event_type  TEXT,
  device_id   TEXT,
  device_type TEXT,
  sampled_at  INTEGER,
  raw_json    TEXT    NOT NULL
);

CREATE INDEX idx_webhook_events_time ON webhook_events (user_id, received_at);

-- 5分ごとの取得の実行記録（データの欠けや API の利用回数の確認に使う）
CREATE TABLE collector_runs (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id                TEXT    NOT NULL,
  started_at             INTEGER NOT NULL,
  finished_at            INTEGER NOT NULL,
  device_list_refreshed  INTEGER NOT NULL,
  devices_total          INTEGER NOT NULL,
  devices_ok             INTEGER NOT NULL,
  readings_written       INTEGER NOT NULL,
  errors_json            TEXT    NOT NULL
);

CREATE INDEX idx_collector_runs_time ON collector_runs (user_id, started_at);
