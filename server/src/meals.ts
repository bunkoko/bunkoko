// 食事の目安（docs/analysis.md の 2.5、docs/phase1-design.md の 5章）
//   /ai/*  … Claude Code のスケジュールされたタスクが、目安がまだの食事を取り、目安を書き込む（AI_TASK_TOKEN）
//   /api/* … アプリが食事の一覧を見る・目安を直す（APP_TOKEN）

import { addDays, formatLocal, localDate } from "./time";
import { Problems, isRecord, oneOf, optionalNumber, optionalString, requiredString, type Json } from "./validate";

export const MAX_PENDING = 100;
/** これより古い食事は、目安を出さない（分析に使う期間を過ぎているため） */
export const PENDING_WINDOW_DAYS = 30;

export interface MealEstimate {
  kcal: number;
  proteinG: number | null;
  fatG: number | null;
  carbsG: number | null;
  caffeineMg: number;
  alcoholDrinks: number;
  greasiness: 1 | 2 | 3;
  spiciness: 1 | 2 | 3;
  volume: 1 | 2 | 3;
  confidence: "low" | "medium" | "high";
  items: { name: string; kcal: number | null }[];
}

const LEVEL = (problems: Problems, path: string, value: unknown): 1 | 2 | 3 | null => {
  if (value === 1 || value === 2 || value === 3) return value;
  problems.add(path, "1・2・3 のどれかにしてください");
  return null;
};

/** 目安の形と値の範囲を確かめる */
export function parseEstimate(problems: Problems, path: string, value: unknown): MealEstimate | null {
  if (!isRecord(value)) {
    problems.add(path, "オブジェクトにしてください");
    return null;
  }
  const before = problems.list.length;
  const kcal = optionalNumber(problems, `${path}.kcal`, value.kcal, 0, 5_000);
  if (kcal === null && value.kcal === undefined) problems.add(`${path}.kcal`, "必須です");
  const estimate: MealEstimate = {
    kcal: kcal ?? 0,
    proteinG: optionalNumber(problems, `${path}.proteinG`, value.proteinG, 0, 500),
    fatG: optionalNumber(problems, `${path}.fatG`, value.fatG, 0, 500),
    carbsG: optionalNumber(problems, `${path}.carbsG`, value.carbsG, 0, 1_000),
    caffeineMg: optionalNumber(problems, `${path}.caffeineMg`, value.caffeineMg, 0, 1_000) ?? 0,
    alcoholDrinks: optionalNumber(problems, `${path}.alcoholDrinks`, value.alcoholDrinks, 0, 20) ?? 0,
    greasiness: LEVEL(problems, `${path}.greasiness`, value.greasiness) ?? 1,
    spiciness: LEVEL(problems, `${path}.spiciness`, value.spiciness) ?? 1,
    volume: LEVEL(problems, `${path}.volume`, value.volume) ?? 1,
    confidence: oneOf(problems, `${path}.confidence`, value.confidence, ["low", "medium", "high"] as const) ?? "low",
    items: [],
  };
  if (value.items !== undefined) {
    if (!Array.isArray(value.items) || value.items.length > 20) problems.add(`${path}.items`, "20件以内の配列にしてください");
    else {
      value.items.forEach((item, index) => {
        if (!isRecord(item)) return problems.add(`${path}.items[${index}]`, "オブジェクトにしてください");
        const name = requiredString(problems, `${path}.items[${index}].name`, item.name, 100);
        const itemKcal = optionalNumber(problems, `${path}.items[${index}].kcal`, item.kcal, 0, 5_000);
        if (name) estimate.items.push({ name, kcal: itemKcal });
      });
    }
  }
  return problems.list.length === before ? estimate : null;
}

export interface PendingMeal {
  id: string;
  text: string;
  amount: string | null;
  meal: string | null;
  /** 利用者のタイムゾーンでの食べた時刻（"YYYY-MM-DD HH:MM"） */
  eatenAt: string;
}

/** まだ目安がない食事（利用者が直したものも、生成AI のものもない） */
export async function listPendingMeals(
  db: D1Database,
  userId: string,
  timeZone: string,
  now: number,
  limit: number,
): Promise<PendingMeal[]> {
  const { results } = await db
    .prepare(
      `SELECT l.id, l.occurred_at, l.data_json FROM user_logs AS l
       WHERE l.user_id = ?1 AND l.kind = 'meal' AND l.deleted = 0 AND l.occurred_at >= ?2
         AND NOT EXISTS (SELECT 1 FROM meal_estimates AS e WHERE e.user_id = l.user_id AND e.log_id = l.id)
       ORDER BY l.occurred_at
       LIMIT ?3`,
    )
    .bind(userId, now - PENDING_WINDOW_DAYS * 24 * 60 * 60 * 1000, limit)
    .all<{ id: string; occurred_at: number; data_json: string }>();
  return results.map((row) => {
    const data = JSON.parse(row.data_json) as Json;
    return {
      id: row.id,
      text: String(data.text ?? ""),
      amount: (data.amount as string | null) ?? null,
      meal: (data.meal as string | null) ?? null,
      eatenAt: formatLocal(row.occurred_at, timeZone).slice(0, 16),
    };
  });
}

export interface EstimateInput {
  id: string;
  model: string | null;
  estimate: MealEstimate;
}

export function parseEstimateBatch(body: unknown, problems: Problems): EstimateInput[] {
  if (!isRecord(body) || !Array.isArray(body.estimates)) {
    problems.add("estimates", "配列にしてください");
    return [];
  }
  if (body.estimates.length > MAX_PENDING) {
    problems.add("estimates", `1回に ${MAX_PENDING} 件までです`);
    return [];
  }
  const inputs: EstimateInput[] = [];
  body.estimates.forEach((item, index) => {
    const path = `estimates[${index}]`;
    if (!isRecord(item)) return problems.add(path, "オブジェクトにしてください");
    const id = requiredString(problems, `${path}.id`, item.id, 64, /^[A-Za-z0-9-]{1,64}$/);
    const model = optionalString(problems, `${path}.model`, item.model, 100);
    const estimate = parseEstimate(problems, `${path}.estimate`, item.estimate);
    if (id && estimate) inputs.push({ id, model, estimate });
  });
  return inputs;
}

/**
 * 目安を保存する。存在しない・削除された・食事でない記録の id は保存せずに返す。
 * 保存した食事の日は、1日 = 1行の表を計算し直すために計算待ちに入れる。
 */
export async function saveEstimates(
  db: D1Database,
  userId: string,
  timeZone: string,
  source: "ai" | "user",
  inputs: EstimateInput[],
  now: number,
): Promise<{ saved: string[]; unknown: string[] }> {
  if (inputs.length === 0) return { saved: [], unknown: [] };
  const { results } = await db
    .prepare(
      `SELECT id, occurred_at FROM user_logs
       WHERE user_id = ?1 AND kind = 'meal' AND deleted = 0 AND id IN (SELECT value FROM json_each(?2))`,
    )
    .bind(userId, JSON.stringify(inputs.map((input) => input.id)))
    .all<{ id: string; occurred_at: number }>();
  const known = new Map(results.map((row) => [row.id, row.occurred_at]));
  const accepted = inputs.filter((input) => known.has(input.id));
  const unknown = inputs.filter((input) => !known.has(input.id)).map((input) => input.id);
  if (accepted.length === 0) return { saved: [], unknown };

  const dates = new Set<string>();
  for (const input of accepted) {
    const date = localDate(known.get(input.id) as number, timeZone);
    dates.add(date);
    dates.add(addDays(date, 1));
  }
  const rows = accepted.map((input) => ({ id: input.id, model: input.model, estimate: JSON.stringify(input.estimate) }));
  await db.batch([
    db
      .prepare(
        `INSERT INTO meal_estimates (user_id, log_id, source, model, estimate_json, estimated_at)
         SELECT ?1, j.value ->> 'id', ?2, j.value ->> 'model', j.value ->> 'estimate', ?3 FROM json_each(?4) AS j WHERE true
         ON CONFLICT (user_id, log_id, source) DO UPDATE SET
           model = excluded.model, estimate_json = excluded.estimate_json, estimated_at = excluded.estimated_at`,
      )
      .bind(userId, source, now, JSON.stringify(rows)),
    db
      .prepare(
        `INSERT INTO day_queue (user_id, date, queued_at)
         SELECT ?1, j.value, ?2 FROM json_each(?3) AS j WHERE true
         ON CONFLICT (user_id, date) DO UPDATE SET queued_at = excluded.queued_at`,
      )
      .bind(userId, now, JSON.stringify([...dates])),
  ]);
  return { saved: accepted.map((input) => input.id), unknown };
}

export interface MealWithEstimate {
  id: string;
  occurredAt: number;
  text: string;
  amount: string | null;
  meal: string | null;
  estimate: MealEstimate | null;
  estimateSource: "user" | "ai" | null;
}

/** アプリ向け: 期間の食事と目安（利用者が直した目安を優先） */
export async function listMeals(db: D1Database, userId: string, from: number, to: number): Promise<MealWithEstimate[]> {
  const { results } = await db
    .prepare(
      `SELECT l.id, l.occurred_at, l.data_json, u.estimate_json AS user_est, a.estimate_json AS ai_est
       FROM user_logs AS l
       LEFT JOIN meal_estimates AS u ON u.user_id = l.user_id AND u.log_id = l.id AND u.source = 'user'
       LEFT JOIN meal_estimates AS a ON a.user_id = l.user_id AND a.log_id = l.id AND a.source = 'ai'
       WHERE l.user_id = ?1 AND l.kind = 'meal' AND l.deleted = 0 AND l.occurred_at >= ?2 AND l.occurred_at < ?3
       ORDER BY l.occurred_at DESC
       LIMIT 500`,
    )
    .bind(userId, from, to)
    .all<{ id: string; occurred_at: number; data_json: string; user_est: string | null; ai_est: string | null }>();
  return results.map((row) => {
    const data = JSON.parse(row.data_json) as Json;
    const raw = row.user_est ?? row.ai_est;
    return {
      id: row.id,
      occurredAt: row.occurred_at,
      text: String(data.text ?? ""),
      amount: (data.amount as string | null) ?? null,
      meal: (data.meal as string | null) ?? null,
      estimate: raw ? (JSON.parse(raw) as MealEstimate) : null,
      estimateSource: row.user_est ? "user" : row.ai_est ? "ai" : null,
    };
  });
}
