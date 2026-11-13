// 計算し直す日（day_queue）を、少しずつ処理する。
// 無料プランは1回の処理時間が短いので、5分ごとの実行のついでに数日分ずつ計算する。
// 新しい日から計算する（過去1年分を取り込んだあとも、最近の日がすぐに見られるように）。

import { loadRoles } from "../roles";
import type { UserSettings } from "../settings";
import { localDate } from "../time";
import { ALGO_VERSION, computeDay } from "./compute";

/** 5分ごとの実行で計算する日数 */
export const DAYS_PER_RUN = 4;

export interface QueueResult {
  computed: string[];
  dropped: string[];
  remaining: number;
}

export async function processDayQueue(
  db: D1Database,
  userId: string,
  settings: UserSettings,
  now: number,
  maxDays = DAYS_PER_RUN,
): Promise<QueueResult> {
  const today = localDate(now, settings.timeZone);
  const { results } = await db
    .prepare(`SELECT date, queued_at FROM day_queue WHERE user_id = ?1 ORDER BY date DESC LIMIT ?2`)
    .bind(userId, maxDays * 2)
    .all<{ date: string; queued_at: number }>();

  const computed: string[] = [];
  const dropped: string[] = [];
  const roles = results.length > 0 ? await loadRoles(db, userId, settings.deviceRoles) : null;

  for (const { date, queued_at: queuedAt } of results) {
    if (date > today) {
      // まだ来ていない日（その夜のデータが届いたら、また計算待ちに入る）
      dropped.push(date);
      await db.prepare(`DELETE FROM day_queue WHERE user_id = ?1 AND date = ?2 AND queued_at <= ?3`).bind(userId, date, queuedAt).run();
      continue;
    }
    if (computed.length >= maxDays || !roles) break;

    const metrics = await computeDay(db, userId, date, settings, roles);
    await db.batch([
      db
        .prepare(
          `INSERT INTO days (user_id, date, algo_version, computed_at, sleep_start, sleep_end, metrics_json)
           VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
           ON CONFLICT (user_id, date) DO UPDATE SET
             algo_version = excluded.algo_version, computed_at = excluded.computed_at,
             sleep_start = excluded.sleep_start, sleep_end = excluded.sleep_end, metrics_json = excluded.metrics_json`,
        )
        .bind(userId, date, ALGO_VERSION, now, metrics.sleep?.onset ?? null, metrics.sleep?.wake ?? null, JSON.stringify(metrics)),
      // 計算している間に新しいデータが届いて入れ直された場合は、消さずに残す
      db.prepare(`DELETE FROM day_queue WHERE user_id = ?1 AND date = ?2 AND queued_at <= ?3`).bind(userId, date, queuedAt),
    ]);
    computed.push(date);
  }

  const remaining = await db
    .prepare(`SELECT COUNT(*) AS n FROM day_queue WHERE user_id = ?1`)
    .bind(userId)
    .first<number>("n");
  return { computed, dropped, remaining: remaining ?? 0 };
}

/** 指定した期間の日を、すべて計算待ちに入れる（計算方法を変えたときなど） */
export async function queueDateRange(db: D1Database, userId: string, dates: string[], now: number): Promise<void> {
  if (dates.length === 0) return;
  await db
    .prepare(
      `INSERT INTO day_queue (user_id, date, queued_at)
       SELECT ?1, j.value, ?2 FROM json_each(?3) AS j WHERE true
       ON CONFLICT (user_id, date) DO UPDATE SET queued_at = excluded.queued_at`,
    )
    .bind(userId, now, JSON.stringify(dates))
    .run();
}
