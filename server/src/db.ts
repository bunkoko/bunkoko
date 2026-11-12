// データベース（Cloudflare D1）への読み書き
// 無料プランでは1回の実行で発行できるクエリの数に上限があるため、
// 複数行の書き込みは JSON を1つ渡して1つのクエリで行う（json_each）。

import type { Reading } from "./normalize";
import type { SwitchBotDevice } from "./switchbot";

export type ReadingSource = "poll" | "webhook";

export interface DeviceReadings {
  deviceId: string;
  measuredAt: number;
  readings: Reading[];
}

export interface PollableDevice {
  deviceId: string;
  deviceName: string | null;
  deviceType: string | null;
}

export interface CollectorError {
  deviceId: string | null;
  message: string;
}

export interface CollectorRun {
  userId: string;
  startedAt: number;
  finishedAt: number;
  deviceListRefreshed: boolean;
  devicesTotal: number;
  devicesOk: number;
  readingsWritten: number;
  errors: CollectorError[];
}

/** 機器一覧を保存し、一覧から消えた機器は取得の対象から外す */
export async function syncDevices(
  db: D1Database,
  userId: string,
  devices: SwitchBotDevice[],
  now: number,
): Promise<void> {
  const rows = devices
    .filter((device) => typeof device.deviceId === "string" && device.deviceId !== "")
    .map((device) => ({
      id: device.deviceId,
      name: device.deviceName ?? null,
      type: device.deviceType ?? null,
      hub: device.hubDeviceId ?? null,
      cloud: device.enableCloudService === false ? 0 : 1,
      raw: JSON.stringify(device),
    }));

  const statements: D1PreparedStatement[] = [];
  if (rows.length > 0) {
    statements.push(
      db
        .prepare(
          `INSERT INTO devices (user_id, device_id, device_name, device_type, hub_device_id,
                                enable_cloud_service, active, first_seen_at, last_seen_at, raw_json)
           SELECT ?1, j.value ->> 'id', j.value ->> 'name', j.value ->> 'type', j.value ->> 'hub',
                  j.value ->> 'cloud', 1, ?2, ?2, j.value ->> 'raw'
           FROM json_each(?3) AS j
           WHERE true
           ON CONFLICT (user_id, device_id) DO UPDATE SET
             device_name = excluded.device_name,
             device_type = excluded.device_type,
             hub_device_id = excluded.hub_device_id,
             enable_cloud_service = excluded.enable_cloud_service,
             active = 1,
             last_seen_at = excluded.last_seen_at,
             raw_json = excluded.raw_json`,
        )
        .bind(userId, now, JSON.stringify(rows)),
    );
  }
  statements.push(
    db
      .prepare(`UPDATE devices SET active = 0 WHERE user_id = ?1 AND last_seen_at < ?2`)
      .bind(userId, now),
  );
  await db.batch(statements);
}

/** 最後に機器一覧を取得した時刻（まだ一度も取得していなければ null） */
export async function getLastDeviceSync(db: D1Database, userId: string): Promise<number | null> {
  const value = await db
    .prepare(`SELECT MAX(last_seen_at) AS last FROM devices WHERE user_id = ?1`)
    .bind(userId)
    .first<number | null>("last");
  return value ?? null;
}

export async function getPollableDevices(db: D1Database, userId: string): Promise<PollableDevice[]> {
  const { results } = await db
    .prepare(
      `SELECT device_id AS deviceId, device_name AS deviceName, device_type AS deviceType
       FROM devices
       WHERE user_id = ?1 AND active = 1 AND enable_cloud_service = 1
       ORDER BY device_id`,
    )
    .bind(userId)
    .all<PollableDevice>();
  return results;
}

/** センサーの値をまとめて保存する。同じ時刻・項目の値がすでにあれば無視する。保存した件数を返す */
export async function insertReadings(
  db: D1Database,
  userId: string,
  source: ReadingSource,
  items: DeviceReadings[],
): Promise<number> {
  const rows = items.flatMap((item) =>
    item.readings.map((reading) => ({
      d: item.deviceId,
      t: item.measuredAt,
      m: reading.metric,
      n: reading.valueNum,
      x: reading.valueText,
    })),
  );
  if (rows.length === 0) return 0;

  const result = await db
    .prepare(
      `INSERT OR IGNORE INTO readings (user_id, device_id, measured_at, metric, source, value_num, value_text)
       SELECT ?1, j.value ->> 'd', j.value ->> 't', j.value ->> 'm', ?2, j.value ->> 'n', j.value ->> 'x'
       FROM json_each(?3) AS j`,
    )
    .bind(userId, source, JSON.stringify(rows))
    .run();
  return result.meta.changes ?? 0;
}

export async function insertCollectorRun(db: D1Database, run: CollectorRun): Promise<void> {
  await db
    .prepare(
      `INSERT INTO collector_runs (user_id, started_at, finished_at, device_list_refreshed,
                                   devices_total, devices_ok, readings_written, errors_json)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)`,
    )
    .bind(
      run.userId,
      run.startedAt,
      run.finishedAt,
      run.deviceListRefreshed ? 1 : 0,
      run.devicesTotal,
      run.devicesOk,
      run.readingsWritten,
      JSON.stringify(run.errors),
    )
    .run();
}

export interface WebhookEventRow {
  userId: string;
  receivedAt: number;
  eventType: string | null;
  deviceId: string | null;
  deviceType: string | null;
  sampledAt: number | null;
  rawJson: string;
}

export async function insertWebhookEvent(db: D1Database, event: WebhookEventRow): Promise<void> {
  await db
    .prepare(
      `INSERT INTO webhook_events (user_id, received_at, event_type, device_id, device_type, sampled_at, raw_json)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`,
    )
    .bind(
      event.userId,
      event.receivedAt,
      event.eventType,
      event.deviceId,
      event.deviceType,
      event.sampledAt,
      event.rawJson,
    )
    .run();
}
