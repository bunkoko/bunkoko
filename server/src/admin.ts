// 動作確認用の管理 API（ADMIN_TOKEN が必要）
//   GET /admin/summary       … 機器ごとの最新の値、直近の取得結果、API の利用回数
//   GET /admin/readings.csv  … センサーの値を CSV で書き出す（最大7日分）

import { DAY_MS, formatLocal, timeZoneOffsetMs } from "./time";

export const MAX_EXPORT_DAYS = 7;
export const MAX_EXPORT_ROWS = 100_000;

interface RunRow {
  started_at: number;
  finished_at: number;
  device_list_refreshed: number;
  devices_total: number;
  devices_ok: number;
  readings_written: number;
  errors_json: string;
}

interface LatestRow {
  device_id: string;
  device_name: string | null;
  device_type: string | null;
  active: number;
  metric: string | null;
  value_num: number | null;
  value_text: string | null;
  measured_at: number | null;
}

const iso = (ms: number | null | undefined) => (typeof ms === "number" ? new Date(ms).toISOString() : null);

export async function buildSummary(db: D1Database, userId: string, now: number) {
  const [runs, latest, webhook, calls] = await db.batch([
    db
      .prepare(
        `SELECT started_at, finished_at, device_list_refreshed, devices_total, devices_ok,
                readings_written, errors_json
         FROM collector_runs WHERE user_id = ?1 ORDER BY started_at DESC LIMIT 5`,
      )
      .bind(userId),
    db
      .prepare(
        `SELECT d.device_id, d.device_name, d.device_type, d.active,
                r.metric, r.value_num, r.value_text, r.measured_at
         FROM devices d
         LEFT JOIN readings r
           ON r.user_id = d.user_id AND r.device_id = d.device_id AND r.source = 'poll'
          AND r.measured_at = (SELECT MAX(r2.measured_at) FROM readings r2
                               WHERE r2.user_id = d.user_id AND r2.device_id = d.device_id
                                 AND r2.source = 'poll')
         WHERE d.user_id = ?1
         ORDER BY d.device_name, d.device_id, r.metric`,
      )
      .bind(userId),
    db
      .prepare(`SELECT COUNT(*) AS count, MAX(received_at) AS last FROM webhook_events WHERE user_id = ?1`)
      .bind(userId),
    db
      .prepare(
        `SELECT COALESCE(SUM(devices_total + device_list_refreshed), 0) AS calls, COUNT(*) AS runs
         FROM collector_runs WHERE user_id = ?1 AND started_at >= ?2`,
      )
      .bind(userId, now - DAY_MS),
  ]);

  const devices = new Map<
    string,
    { deviceId: string; name: string | null; type: string | null; active: boolean; lastPolledAt: string | null; values: Record<string, number | string | null> }
  >();
  for (const row of latest.results as unknown as LatestRow[]) {
    let device = devices.get(row.device_id);
    if (!device) {
      device = {
        deviceId: row.device_id,
        name: row.device_name,
        type: row.device_type,
        active: row.active === 1,
        lastPolledAt: iso(row.measured_at),
        values: {},
      };
      devices.set(row.device_id, device);
    }
    if (row.metric) device.values[row.metric] = row.value_num ?? row.value_text;
  }

  const webhookRow = (webhook.results[0] ?? {}) as { count?: number; last?: number | null };
  const callsRow = (calls.results[0] ?? {}) as { calls?: number; runs?: number };

  return {
    now: iso(now),
    devices: [...devices.values()],
    recentRuns: (runs.results as unknown as RunRow[]).map((run) => ({
      startedAt: iso(run.started_at),
      durationMs: run.finished_at - run.started_at,
      deviceListRefreshed: run.device_list_refreshed === 1,
      devicesOk: run.devices_ok,
      devicesTotal: run.devices_total,
      readingsWritten: run.readings_written,
      errors: JSON.parse(run.errors_json),
    })),
    webhook: { eventsReceived: webhookRow.count ?? 0, lastReceivedAt: iso(webhookRow.last) },
    // 1日の上限は 10,000 回
    switchBotApiCallsLast24h: callsRow.calls ?? 0,
    collectorRunsLast24h: callsRow.runs ?? 0,
  };
}

/** "2026-11-20" や ISO 形式の日時を受け取り、エポックミリ秒にする。日付だけの場合はそのタイムゾーンの0時 */
export function parseTimeParam(value: string | null, timeZone: string): number | null {
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const utcMidnight = Date.parse(`${value}T00:00:00Z`);
    return utcMidnight - timeZoneOffsetMs(utcMidnight, timeZone);
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function csvCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const text = String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export async function exportReadingsCsv(
  db: D1Database,
  userId: string,
  url: URL,
  timeZone: string,
  now: number,
): Promise<Response> {
  const to = parseTimeParam(url.searchParams.get("to"), timeZone) ?? now;
  const from = parseTimeParam(url.searchParams.get("from"), timeZone) ?? to - DAY_MS;
  if (from >= to) return new Response("from は to より前にしてください", { status: 400 });
  if (to - from > MAX_EXPORT_DAYS * DAY_MS) {
    return new Response(`一度に書き出せるのは ${MAX_EXPORT_DAYS} 日分までです`, { status: 400 });
  }

  const device = url.searchParams.get("device");
  const metric = url.searchParams.get("metric");
  const { results } = await db
    .prepare(
      `SELECT r.measured_at, r.device_id, d.device_name, d.device_type, r.metric,
              r.value_num, r.value_text, r.source
       FROM readings r
       LEFT JOIN devices d ON d.user_id = r.user_id AND d.device_id = r.device_id
       WHERE r.user_id = ?1 AND r.measured_at >= ?2 AND r.measured_at < ?3
         AND (?4 IS NULL OR r.device_id = ?4)
         AND (?5 IS NULL OR r.metric = ?5)
       ORDER BY r.measured_at, r.device_id, r.metric
       LIMIT ?6`,
    )
    .bind(userId, from, to, device, metric, MAX_EXPORT_ROWS + 1)
    .all<{
      measured_at: number;
      device_id: string;
      device_name: string | null;
      device_type: string | null;
      metric: string;
      value_num: number | null;
      value_text: string | null;
      source: string;
    }>();

  const truncated = results.length > MAX_EXPORT_ROWS;
  const lines = ["time_utc,time_local,device_id,device_name,device_type,metric,value,source"];
  for (const row of results.slice(0, MAX_EXPORT_ROWS)) {
    lines.push(
      [
        new Date(row.measured_at).toISOString(),
        formatLocal(row.measured_at, timeZone),
        row.device_id,
        row.device_name,
        row.device_type,
        row.metric,
        row.value_num ?? row.value_text,
        row.source,
      ]
        .map(csvCell)
        .join(","),
    );
  }

  return new Response(`﻿${lines.join("\r\n")}\r\n`, {
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="readings.csv"`,
      "X-Truncated": truncated ? "true" : "false",
    },
  });
}
