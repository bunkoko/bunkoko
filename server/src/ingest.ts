// iPhone アプリから届くデータ（HealthKit・記録）を確かめて保存する。
// 1回に届く件数が多いので、まとめて1つのクエリで書き込む（json_each）。

import { addDays, localDate } from "./time";
import {
  Problems,
  arrayOf,
  isEpochMs,
  isRecord,
  oneOf,
  optionalNumber,
  optionalString,
  requiredString,
  type Json,
} from "./validate";

export const MAX_BATCH_BYTES = 1_000_000;
/** 1回に受け取る件数の上限。無料プランの処理時間に収めるため、アプリが小分けにして送る */
export const MAX_BATCH_ITEMS = 1_000;
const MAX_QUEUED_DATES = 400;

const ID_PATTERN = /^[A-Za-z0-9-]{1,64}$/;
const TYPE_PATTERN = /^[A-Za-z0-9_]{1,64}$/;
export const BUCKET_MINUTES = [5, 60] as const;

// ---------------------------------------------------------------------------
// HealthKit

interface SampleRow {
  u: string;
  ty: string;
  s: number;
  e: number;
  v: number | null;
  vt: string | null;
  un: string | null;
  sn: string | null;
  sp: string | null;
  m: string | null;
}

interface BucketRow {
  ty: string;
  mi: number;
  s: number;
  su: number | null;
  av: number | null;
  mn: number | null;
  mx: number | null;
}

interface WorkoutRow {
  u: string;
  at: string;
  s: number;
  e: number;
  kc: number | null;
  dm: number | null;
  hr: number | null;
  ef: number | null;
  sn: string | null;
}

export interface HealthBatch {
  samples: SampleRow[];
  buckets: BucketRow[];
  workouts: WorkoutRow[];
  deleted: string[];
}

function parseTimeRange(problems: Problems, path: string, item: Json): [number, number] | null {
  if (!isEpochMs(item.start) || !isEpochMs(item.end)) {
    problems.add(path, "start と end はエポックミリ秒（整数）にしてください");
    return null;
  }
  if (item.end < item.start) {
    problems.add(path, "end は start 以降にしてください");
    return null;
  }
  return [item.start, item.end];
}

function parseSource(problems: Problems, path: string, value: unknown): { name: string | null; product: string | null } {
  if (value === undefined || value === null) return { name: null, product: null };
  if (!isRecord(value)) {
    problems.add(path, "オブジェクトにしてください");
    return { name: null, product: null };
  }
  return {
    name: optionalString(problems, `${path}.name`, value.name, 100),
    product: optionalString(problems, `${path}.productType`, value.productType, 64),
  };
}

export function parseHealthBatch(body: unknown, problems: Problems): HealthBatch {
  const batch: HealthBatch = { samples: [], buckets: [], workouts: [], deleted: [] };
  if (!isRecord(body)) {
    problems.add("body", "オブジェクトにしてください");
    return batch;
  }
  const samples = arrayOf(problems, "samples", body.samples);
  const buckets = arrayOf(problems, "buckets", body.buckets);
  const workouts = arrayOf(problems, "workouts", body.workouts);
  const deleted = arrayOf(problems, "deleted", body.deleted);
  const total = samples.length + buckets.length + workouts.length + deleted.length;
  if (total > MAX_BATCH_ITEMS) {
    problems.add("body", `1回に送れるのは ${MAX_BATCH_ITEMS} 件までです（${total} 件）`);
    return batch;
  }

  samples.forEach((item, index) => {
    const path = `samples[${index}]`;
    if (!isRecord(item)) return problems.add(path, "オブジェクトにしてください");
    const uuid = requiredString(problems, `${path}.uuid`, item.uuid, 64, ID_PATTERN);
    const type = requiredString(problems, `${path}.type`, item.type, 64, TYPE_PATTERN);
    const range = parseTimeRange(problems, path, item);
    const value = optionalNumber(problems, `${path}.value`, item.value);
    const valueText = optionalString(problems, `${path}.valueText`, item.valueText, 64);
    const unit = optionalString(problems, `${path}.unit`, item.unit, 32);
    const source = parseSource(problems, `${path}.source`, item.source);
    let meta: string | null = null;
    if (item.meta !== undefined && item.meta !== null) {
      meta = isRecord(item.meta) ? JSON.stringify(item.meta) : null;
      if (meta === null || meta.length > 2_000) problems.add(`${path}.meta`, "2,000 文字以内のオブジェクトにしてください");
    }
    if (uuid && type && range) {
      batch.samples.push({ u: uuid, ty: type, s: range[0], e: range[1], v: value, vt: valueText, un: unit, sn: source.name, sp: source.product, m: meta });
    }
  });

  buckets.forEach((item, index) => {
    const path = `buckets[${index}]`;
    if (!isRecord(item)) return problems.add(path, "オブジェクトにしてください");
    const type = requiredString(problems, `${path}.type`, item.type, 64, TYPE_PATTERN);
    if (!isEpochMs(item.start)) problems.add(`${path}.start`, "エポックミリ秒（整数）にしてください");
    if (!(BUCKET_MINUTES as readonly unknown[]).includes(item.minutes)) {
      problems.add(`${path}.minutes`, `${BUCKET_MINUTES.join(" / ")} のどれかにしてください`);
    }
    const sum = optionalNumber(problems, `${path}.sum`, item.sum);
    const avg = optionalNumber(problems, `${path}.avg`, item.avg);
    const min = optionalNumber(problems, `${path}.min`, item.min);
    const max = optionalNumber(problems, `${path}.max`, item.max);
    if (type && isEpochMs(item.start) && (BUCKET_MINUTES as readonly unknown[]).includes(item.minutes)) {
      batch.buckets.push({ ty: type, mi: item.minutes as number, s: item.start, su: sum, av: avg, mn: min, mx: max });
    }
  });

  workouts.forEach((item, index) => {
    const path = `workouts[${index}]`;
    if (!isRecord(item)) return problems.add(path, "オブジェクトにしてください");
    const uuid = requiredString(problems, `${path}.uuid`, item.uuid, 64, ID_PATTERN);
    const activityType = requiredString(problems, `${path}.activityType`, item.activityType, 64, TYPE_PATTERN);
    const range = parseTimeRange(problems, path, item);
    const energy = optionalNumber(problems, `${path}.energyKcal`, item.energyKcal, 0, 20_000);
    const distance = optionalNumber(problems, `${path}.distanceM`, item.distanceM, 0, 1_000_000);
    const heartRate = optionalNumber(problems, `${path}.avgHeartRate`, item.avgHeartRate, 0, 300);
    const effort = optionalNumber(problems, `${path}.effort`, item.effort, 0, 10);
    const source = parseSource(problems, `${path}.source`, item.source);
    if (uuid && activityType && range) {
      batch.workouts.push({ u: uuid, at: activityType, s: range[0], e: range[1], kc: energy, dm: distance, hr: heartRate, ef: effort, sn: source.name });
    }
  });

  deleted.forEach((item, index) => {
    const uuid = requiredString(problems, `deleted[${index}]`, item, 64, ID_PATTERN);
    if (uuid) batch.deleted.push(uuid);
  });
  return batch;
}

/** 影響する日（その日と翌日）を集める。日中のデータは「翌朝に起きた日」の行に入るため、翌日も入れる */
function collectDates(dates: Set<string>, epochMs: number, timeZone: string): void {
  const date = localDate(epochMs, timeZone);
  dates.add(date);
  dates.add(addDays(date, 1));
}

function queueStatement(db: D1Database, userId: string, dates: Set<string>, now: number): D1PreparedStatement | null {
  if (dates.size === 0) return null;
  const list = [...dates].sort().slice(-MAX_QUEUED_DATES);
  return db
    .prepare(
      `INSERT INTO day_queue (user_id, date, queued_at)
       SELECT ?1, j.value, ?2 FROM json_each(?3) AS j WHERE true
       ON CONFLICT (user_id, date) DO UPDATE SET queued_at = excluded.queued_at`,
    )
    .bind(userId, now, JSON.stringify(list));
}

export interface IngestResult {
  accepted: Record<string, number>;
  daysQueued: string[];
}

export async function ingestHealthBatch(
  db: D1Database,
  userId: string,
  timeZone: string,
  batch: HealthBatch,
  now: number,
): Promise<IngestResult> {
  const dates = new Set<string>();
  for (const sample of batch.samples) collectDates(dates, sample.e, timeZone);
  for (const bucket of batch.buckets) collectDates(dates, bucket.s, timeZone);
  for (const workout of batch.workouts) collectDates(dates, workout.e, timeZone);

  if (batch.deleted.length > 0) {
    // 削除されたデータの日付を調べて、その日も計算し直す
    const { results } = await db
      .prepare(
        `SELECT end_at FROM health_samples WHERE user_id = ?1 AND uuid IN (SELECT value FROM json_each(?2))
         UNION ALL
         SELECT end_at FROM workouts WHERE user_id = ?1 AND uuid IN (SELECT value FROM json_each(?2))`,
      )
      .bind(userId, JSON.stringify(batch.deleted))
      .all<{ end_at: number }>();
    for (const row of results) collectDates(dates, row.end_at, timeZone);
  }

  const statements: D1PreparedStatement[] = [];
  if (batch.samples.length > 0) {
    statements.push(
      db
        .prepare(
          `INSERT INTO health_samples (user_id, uuid, type, start_at, end_at, value_num, value_text, unit,
                                       source_name, source_product, meta_json, deleted, updated_at)
           SELECT ?1, j.value ->> 'u', j.value ->> 'ty', j.value ->> 's', j.value ->> 'e', j.value ->> 'v',
                  j.value ->> 'vt', j.value ->> 'un', j.value ->> 'sn', j.value ->> 'sp', j.value ->> 'm', 0, ?2
           FROM json_each(?3) AS j WHERE true
           ON CONFLICT (user_id, uuid) DO UPDATE SET
             type = excluded.type, start_at = excluded.start_at, end_at = excluded.end_at,
             value_num = excluded.value_num, value_text = excluded.value_text, unit = excluded.unit,
             source_name = excluded.source_name, source_product = excluded.source_product,
             meta_json = excluded.meta_json, deleted = 0, updated_at = excluded.updated_at`,
        )
        .bind(userId, now, JSON.stringify(batch.samples)),
    );
  }
  if (batch.buckets.length > 0) {
    statements.push(
      db
        .prepare(
          `INSERT INTO health_buckets (user_id, type, bucket_minutes, bucket_start, sum, avg, min, max, updated_at)
           SELECT ?1, j.value ->> 'ty', j.value ->> 'mi', j.value ->> 's', j.value ->> 'su', j.value ->> 'av',
                  j.value ->> 'mn', j.value ->> 'mx', ?2
           FROM json_each(?3) AS j WHERE true
           ON CONFLICT (user_id, type, bucket_minutes, bucket_start) DO UPDATE SET
             sum = excluded.sum, avg = excluded.avg, min = excluded.min, max = excluded.max,
             updated_at = excluded.updated_at`,
        )
        .bind(userId, now, JSON.stringify(batch.buckets)),
    );
  }
  if (batch.workouts.length > 0) {
    statements.push(
      db
        .prepare(
          `INSERT INTO workouts (user_id, uuid, activity_type, start_at, end_at, energy_kcal, distance_m,
                                 avg_heart_rate, effort, source_name, deleted, updated_at)
           SELECT ?1, j.value ->> 'u', j.value ->> 'at', j.value ->> 's', j.value ->> 'e', j.value ->> 'kc',
                  j.value ->> 'dm', j.value ->> 'hr', j.value ->> 'ef', j.value ->> 'sn', 0, ?2
           FROM json_each(?3) AS j WHERE true
           ON CONFLICT (user_id, uuid) DO UPDATE SET
             activity_type = excluded.activity_type, start_at = excluded.start_at, end_at = excluded.end_at,
             energy_kcal = excluded.energy_kcal, distance_m = excluded.distance_m,
             avg_heart_rate = excluded.avg_heart_rate, effort = excluded.effort,
             source_name = excluded.source_name, deleted = 0, updated_at = excluded.updated_at`,
        )
        .bind(userId, now, JSON.stringify(batch.workouts)),
    );
  }
  if (batch.deleted.length > 0) {
    const deletedJson = JSON.stringify(batch.deleted);
    statements.push(
      db
        .prepare(
          `UPDATE health_samples SET deleted = 1, updated_at = ?2
           WHERE user_id = ?1 AND uuid IN (SELECT value FROM json_each(?3))`,
        )
        .bind(userId, now, deletedJson),
      db
        .prepare(
          `UPDATE workouts SET deleted = 1, updated_at = ?2
           WHERE user_id = ?1 AND uuid IN (SELECT value FROM json_each(?3))`,
        )
        .bind(userId, now, deletedJson),
    );
  }
  const queue = queueStatement(db, userId, dates, now);
  if (queue) statements.push(queue);
  if (statements.length > 0) await db.batch(statements);

  return {
    accepted: {
      samples: batch.samples.length,
      buckets: batch.buckets.length,
      workouts: batch.workouts.length,
      deleted: batch.deleted.length,
    },
    daysQueued: [...dates].sort(),
  };
}

// ---------------------------------------------------------------------------
// 記録（手入力・ショートカット・スクリーンタイム）

export const LOG_KINDS = ["meal", "morningFeeling", "bath", "caffeine", "alcohol", "charger", "screenUsage"] as const;
export type LogKind = (typeof LOG_KINDS)[number];

const MEAL_AMOUNTS = ["small", "normal", "large"] as const;
const MEAL_TYPES = ["breakfast", "lunch", "dinner", "snack"] as const;
const CHARGER_STATES = ["connected", "disconnected"] as const;

interface LogRow {
  id: string;
  kind: LogKind;
  t: number;
  /** 種類ごとに決まった順番の項目で JSON にした文字列（内容が変わったかを文字列で比べられるように） */
  data: string;
}

export interface LogsBatch {
  logs: LogRow[];
  deleted: string[];
}

/** 種類ごとの中身を確かめ、決まった項目だけを持つオブジェクトにする */
function parseLogData(problems: Problems, path: string, kind: LogKind, value: unknown): Json | null {
  const data: Json = isRecord(value) ? value : {};
  if (value !== undefined && !isRecord(value)) problems.add(path, "オブジェクトにしてください");
  switch (kind) {
    case "meal": {
      const text = requiredString(problems, `${path}.text`, typeof data.text === "string" ? data.text.trim() : data.text, 500);
      const amount = oneOf(problems, `${path}.amount`, data.amount, MEAL_AMOUNTS, true);
      const meal = oneOf(problems, `${path}.meal`, data.meal, MEAL_TYPES, true);
      return text ? { text, amount, meal } : null;
    }
    case "morningFeeling": {
      const score = data.score;
      if (!Number.isInteger(score) || (score as number) < 1 || (score as number) > 5) {
        problems.add(`${path}.score`, "1〜5 の整数にしてください");
        return null;
      }
      return { score, note: optionalString(problems, `${path}.note`, data.note, 500) };
    }
    case "bath":
      return {};
    case "caffeine":
      return {
        item: optionalString(problems, `${path}.item`, data.item, 100),
        mg: optionalNumber(problems, `${path}.mg`, data.mg, 0, 1_000),
      };
    case "alcohol":
      return {
        item: optionalString(problems, `${path}.item`, data.item, 100),
        drinks: optionalNumber(problems, `${path}.drinks`, data.drinks, 0, 20),
      };
    case "charger": {
      const state = oneOf(problems, `${path}.state`, data.state, CHARGER_STATES);
      return state ? { state } : null;
    }
    case "screenUsage": {
      const minutes = optionalNumber(problems, `${path}.cumulativeMinutes`, data.cumulativeMinutes, 0, 1_440);
      if (minutes === null) {
        problems.add(`${path}.cumulativeMinutes`, "0〜1440 の数値にしてください");
        return null;
      }
      return { cumulativeMinutes: minutes };
    }
  }
}

export function parseLogsBatch(body: unknown, problems: Problems): LogsBatch {
  const batch: LogsBatch = { logs: [], deleted: [] };
  if (!isRecord(body)) {
    problems.add("body", "オブジェクトにしてください");
    return batch;
  }
  const logs = arrayOf(problems, "logs", body.logs);
  const deleted = arrayOf(problems, "deleted", body.deleted);
  if (logs.length + deleted.length > MAX_BATCH_ITEMS) {
    problems.add("body", `1回に送れるのは ${MAX_BATCH_ITEMS} 件までです`);
    return batch;
  }

  logs.forEach((item, index) => {
    const path = `logs[${index}]`;
    if (!isRecord(item)) return problems.add(path, "オブジェクトにしてください");
    const id = requiredString(problems, `${path}.id`, item.id, 64, ID_PATTERN);
    const kind = oneOf(problems, `${path}.kind`, item.kind, LOG_KINDS);
    if (!isEpochMs(item.occurredAt)) problems.add(`${path}.occurredAt`, "エポックミリ秒（整数）にしてください");
    const data = kind ? parseLogData(problems, `${path}.data`, kind, item.data) : null;
    if (id && kind && isEpochMs(item.occurredAt) && data) {
      batch.logs.push({ id, kind, t: item.occurredAt, data: JSON.stringify(data) });
    }
  });
  deleted.forEach((item, index) => {
    const id = requiredString(problems, `deleted[${index}]`, item, 64, ID_PATTERN);
    if (id) batch.deleted.push(id);
  });
  return batch;
}

export async function ingestLogsBatch(
  db: D1Database,
  userId: string,
  timeZone: string,
  batch: LogsBatch,
  now: number,
): Promise<IngestResult> {
  const dates = new Set<string>();
  for (const log of batch.logs) collectDates(dates, log.t, timeZone);
  if (batch.deleted.length > 0) {
    const { results } = await db
      .prepare(`SELECT occurred_at FROM user_logs WHERE user_id = ?1 AND id IN (SELECT value FROM json_each(?2))`)
      .bind(userId, JSON.stringify(batch.deleted))
      .all<{ occurred_at: number }>();
    for (const row of results) collectDates(dates, row.occurred_at, timeZone);
  }

  const statements: D1PreparedStatement[] = [];
  if (batch.logs.length > 0) {
    const logsJson = JSON.stringify(batch.logs);
    statements.push(
      // 食事の内容が変わったら、古い目安を消す（生成AIで推定し直すため）
      db
        .prepare(
          `DELETE FROM meal_estimates WHERE user_id = ?1 AND log_id IN (
             SELECT l.id FROM user_logs AS l JOIN json_each(?2) AS j ON l.id = j.value ->> 'id'
             WHERE l.user_id = ?1 AND l.data_json <> j.value ->> 'data')`,
        )
        .bind(userId, logsJson),
      db
        .prepare(
          `INSERT INTO user_logs (user_id, id, kind, occurred_at, data_json, deleted, created_at, updated_at)
           SELECT ?1, j.value ->> 'id', j.value ->> 'kind', j.value ->> 't', j.value ->> 'data', 0, ?2, ?2
           FROM json_each(?3) AS j WHERE true
           ON CONFLICT (user_id, id) DO UPDATE SET
             kind = excluded.kind, occurred_at = excluded.occurred_at, data_json = excluded.data_json,
             deleted = 0, updated_at = excluded.updated_at`,
        )
        .bind(userId, now, logsJson),
    );
  }
  if (batch.deleted.length > 0) {
    const deletedJson = JSON.stringify(batch.deleted);
    statements.push(
      db
        .prepare(
          `UPDATE user_logs SET deleted = 1, updated_at = ?2
           WHERE user_id = ?1 AND id IN (SELECT value FROM json_each(?3))`,
        )
        .bind(userId, now, deletedJson),
      db
        .prepare(`DELETE FROM meal_estimates WHERE user_id = ?1 AND log_id IN (SELECT value FROM json_each(?2))`)
        .bind(userId, deletedJson),
    );
  }
  const queue = queueStatement(db, userId, dates, now);
  if (queue) statements.push(queue);
  if (statements.length > 0) await db.batch(statements);

  return {
    accepted: { logs: batch.logs.length, deleted: batch.deleted.length },
    daysQueued: [...dates].sort(),
  };
}
