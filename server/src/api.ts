// iPhone アプリ向けの API（/api/*）。APP_TOKEN が必要。

import { hasBearer, jsonError, notFound, readJsonBody, unauthorized } from "./http";
import {
  MAX_BATCH_BYTES,
  ingestHealthBatch,
  ingestLogsBatch,
  parseHealthBatch,
  parseLogsBatch,
} from "./ingest";
import { applySettingsPatch, getSettings, saveSettings } from "./settings";
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
  }
  return notFound();
}
