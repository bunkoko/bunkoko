// iPhone アプリ向けの API（/api/*）。APP_TOKEN が必要。

import { hasBearer, jsonError, notFound, readJsonBody, unauthorized } from "./http";
import {
  MAX_BATCH_BYTES,
  ingestHealthBatch,
  ingestLogsBatch,
  parseHealthBatch,
  parseLogsBatch,
} from "./ingest";
import { listMeals, parseEstimate, saveEstimates } from "./meals";
import { buildHome, buildTimeline, loadDays } from "./read";
import { applySettingsPatch, getSettings, saveSettings } from "./settings";
import { DAY_MS, addDays, isValidDate, localDate, localMidnight } from "./time";
import { Problems, isRecord, oneOf } from "./validate";

export interface ApiEnv {
  DB: D1Database;
  APP_TOKEN: string;
  TIME_ZONE?: string;
}

const APNS_ENVIRONMENTS = ["sandbox", "production"] as const;
const APNS_TOKEN = /^[0-9a-fA-F]{64,200}$/;

const invalid = (problems: Problems) => jsonError(400, "送られたデータに問題があります", problems.list);

export async function handleApi(
  request: Request,
  url: URL,
  env: ApiEnv,
  userId: string,
  now: number,
): Promise<Response> {
  if (!(await hasBearer(request, env.APP_TOKEN))) return unauthorized();
  const defaultTimeZone = env.TIME_ZONE || "Asia/Tokyo";
  const route = `${request.method} ${url.pathname}`;

  switch (route) {
    case "POST /api/health/batch": {
      const body = await readJsonBody(request, MAX_BATCH_BYTES);
      if (!body.ok) return body.response;
      const problems = new Problems();
      const batch = parseHealthBatch(body.value, problems);
      if (!problems.ok) return invalid(problems);
      const settings = await getSettings(env.DB, userId, defaultTimeZone);
      const result = await ingestHealthBatch(env.DB, userId, settings.timeZone, batch, now);
      return Response.json({ ok: true, ...result });
    }

    case "POST /api/logs/batch": {
      const body = await readJsonBody(request, MAX_BATCH_BYTES);
      if (!body.ok) return body.response;
      const problems = new Problems();
      const batch = parseLogsBatch(body.value, problems);
      if (!problems.ok) return invalid(problems);
      const settings = await getSettings(env.DB, userId, defaultTimeZone);
      const result = await ingestLogsBatch(env.DB, userId, settings.timeZone, batch, now);
      return Response.json({ ok: true, ...result });
    }

    case "GET /api/settings":
      return Response.json({ ok: true, settings: await getSettings(env.DB, userId, defaultTimeZone) });

    case "PUT /api/settings": {
      const body = await readJsonBody(request, 64 * 1024);
      if (!body.ok) return body.response;
      const problems = new Problems();
      const current = await getSettings(env.DB, userId, defaultTimeZone);
      const next = applySettingsPatch(current, body.value, problems);
      if (!problems.ok) return invalid(problems);
      await saveSettings(env.DB, userId, next, now);
      return Response.json({ ok: true, settings: next });
    }

    case "POST /api/devices/apns": {
      const body = await readJsonBody(request, 4 * 1024);
      if (!body.ok) return body.response;
      const problems = new Problems();
      const value = isRecord(body.value) ? body.value : {};
      if (typeof value.token !== "string" || !APNS_TOKEN.test(value.token)) {
        problems.add("token", "16進数のデバイストークンにしてください");
      }
      const environment = oneOf(problems, "environment", value.environment, APNS_ENVIRONMENTS);
      if (!problems.ok || !environment) return invalid(problems);
      await env.DB
        .prepare(
          `INSERT INTO apns_devices (user_id, token, environment, created_at, updated_at)
           VALUES (?1, ?2, ?3, ?4, ?4)
           ON CONFLICT (user_id, token) DO UPDATE SET environment = excluded.environment, updated_at = excluded.updated_at`,
        )
        .bind(userId, (value.token as string).toLowerCase(), environment, now)
        .run();
      return Response.json({ ok: true });
    }

    case "GET /api/home": {
      const settings = await getSettings(env.DB, userId, defaultTimeZone);
      return Response.json({ ok: true, ...(await buildHome(env.DB, userId, settings, now)) });
    }

    case "GET /api/days": {
      // 1日 = 1行の表（from / to は日付。省略すると直近14日）
      const settings = await getSettings(env.DB, userId, defaultTimeZone);
      const today = localDate(now, settings.timeZone);
      const to = url.searchParams.get("to") ?? today;
      const from = url.searchParams.get("from") ?? addDays(to, -13);
      if (!isValidDate(from) || !isValidDate(to) || from > to) return jsonError(400, "from と to を YYYY-MM-DD で指定してください");
      if (addDays(from, 92) < to) return jsonError(400, "一度に見られるのは92日分までです");
      return Response.json({ ok: true, days: await loadDays(env.DB, userId, from, to) });
    }

    case "GET /api/meals": {
      // 期間の食事と目安（from / to は日付。省略すると直近7日）
      const settings = await getSettings(env.DB, userId, defaultTimeZone);
      const fromDate = url.searchParams.get("from");
      const toDate = url.searchParams.get("to");
      if ((fromDate && !isValidDate(fromDate)) || (toDate && !isValidDate(toDate))) {
        return jsonError(400, "from と to は YYYY-MM-DD で指定してください");
      }
      const to = toDate ? localMidnight(addDays(toDate, 1), settings.timeZone) : now;
      const from = fromDate ? localMidnight(fromDate, settings.timeZone) : to - 7 * DAY_MS;
      if (to - from > 62 * DAY_MS) return jsonError(400, "一度に見られるのは62日分までです");
      return Response.json({ ok: true, meals: await listMeals(env.DB, userId, from, to) });
    }
  }

  // 夜の詳細
  const timeline = /^\/api\/nights\/(\d{4}-\d{2}-\d{2})\/timeline$/.exec(url.pathname);
  if (request.method === "GET" && timeline && isValidDate(timeline[1])) {
    const settings = await getSettings(env.DB, userId, defaultTimeZone);
    const view = await buildTimeline(env.DB, userId, timeline[1], settings);
    if (!view) return jsonError(404, "その日のデータはまだ計算されていません");
    return Response.json({ ok: true, ...view });
  }

  // 食事の目安を利用者が直す（生成AI の目安より優先される）
  const mealEstimate = /^\/api\/meals\/([A-Za-z0-9-]{1,64})\/estimate$/.exec(url.pathname);
  if (request.method === "PUT" && mealEstimate) {
    const body = await readJsonBody(request, 32 * 1024);
    if (!body.ok) return body.response;
    const problems = new Problems();
    const estimate = parseEstimate(problems, "estimate", isRecord(body.value) ? body.value.estimate : undefined);
    if (!problems.ok || !estimate) return invalid(problems);
    const settings = await getSettings(env.DB, userId, defaultTimeZone);
    const result = await saveEstimates(env.DB, userId, settings.timeZone, "user", [{ id: mealEstimate[1], model: null, estimate }], now);
    if (result.saved.length === 0) return jsonError(404, "その食事の記録はありません");
    return Response.json({ ok: true });
  }
  return notFound();
}
