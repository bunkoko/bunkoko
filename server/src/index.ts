// Cloudflare Workers の入り口
//   - 5分ごと（cron）: SwitchBot の全機器の状態を取得して保存する
//   - HTTP: Webhook の受け取り、iPhone アプリ向けの API（/api/*）、Claude Code のタスク向けの API（/ai/*）、
//           動作確認用の管理 API（/admin/*）

import { buildSummary, exportReadingsCsv } from "./admin";
import { handleAi } from "./ai";
import { handleApi } from "./api";
import { collect } from "./collector";
import { processDayQueue, queueDateRange } from "./days/queue";
import { hasBearer, jsonError, notFound, secretsMatch, unauthorized } from "./http";
import { apnsConfigFrom, runNotifications, sendToAll } from "./notify";
import { latestRoom } from "./read";
import { getSettings } from "./settings";
import { SwitchBotClient } from "./switchbot";
import { addDays, isValidDate } from "./time";
import { handleSwitchBotWebhook } from "./webhook";

export interface Env {
  DB: D1Database;
  SWITCHBOT_TOKEN: string;
  SWITCHBOT_SECRET: string;
  /** Webhook の URL に含める合言葉。SwitchBot の Webhook には署名がないため、URL で見分ける */
  WEBHOOK_SECRET: string;
  /** 管理 API を使うための合言葉（Authorization: Bearer <ADMIN_TOKEN>） */
  ADMIN_TOKEN: string;
  /** iPhone アプリが /api/* を使うための合言葉 */
  APP_TOKEN: string;
  /** Claude Code のスケジュールされたタスクが /ai/* を使うための合言葉 */
  AI_TASK_TOKEN: string;
  USER_ID?: string;
  TIME_ZONE?: string;
  /** 通知（APNs）。Apple Developer Program で作る通知用の鍵（.p8 の中身）、鍵の ID、チーム ID、アプリの ID */
  APNS_KEY_P8?: string;
  APNS_KEY_ID?: string;
  APNS_TEAM_ID?: string;
  APNS_BUNDLE_ID?: string;
  /** テスト用。通常は設定しない */
  SWITCHBOT_API_BASE?: string;
}

const userIdOf = (env: Env) => env.USER_ID || "me";

function createClient(env: Env): SwitchBotClient {
  return new SwitchBotClient({
    token: env.SWITCHBOT_TOKEN,
    secret: env.SWITCHBOT_SECRET,
    baseUrl: env.SWITCHBOT_API_BASE,
  });
}

export async function handleRequest(request: Request, env: Env, now = Date.now()): Promise<Response> {
  const url = new URL(request.url);
  const userId = userIdOf(env);

  if (url.pathname === "/" && request.method === "GET") {
    return new Response("bunkoko-server is running\n");
  }

  const webhookMatch = /^\/webhooks\/switchbot\/([^/]+)$/.exec(url.pathname);
  if (webhookMatch) {
    if (request.method !== "POST") return notFound();
    // 合言葉が違う場合は、URL が存在しないのと同じ応答にする
    if (!(await secretsMatch(decodeURIComponent(webhookMatch[1]), env.WEBHOOK_SECRET))) return notFound();
    return handleSwitchBotWebhook(request, env.DB, userId);
  }

  if (url.pathname.startsWith("/api/")) {
    return handleApi(request, url, env, userId, now);
  }

  if (url.pathname.startsWith("/ai/")) {
    return handleAi(request, url, env, userId, now);
  }

  if (url.pathname.startsWith("/admin/")) {
    if (!(await hasBearer(request, env.ADMIN_TOKEN))) return unauthorized();
    if (url.pathname === "/admin/summary" && request.method === "GET") {
      return Response.json(await buildSummary(env.DB, userId, now));
    }
    if (url.pathname === "/admin/readings.csv" && request.method === "GET") {
      return exportReadingsCsv(env.DB, userId, url, env.TIME_ZONE || "Asia/Tokyo", now);
    }
    if (url.pathname === "/admin/days" && request.method === "GET") {
      // 計算した 1日 = 1行の表を見る（動作確認用）
      const from = url.searchParams.get("from") ?? "0000-00-00";
      const to = url.searchParams.get("to") ?? "9999-99-99";
      const { results } = await env.DB
        .prepare(`SELECT date, computed_at, metrics_json FROM days WHERE user_id = ?1 AND date >= ?2 AND date <= ?3 ORDER BY date DESC LIMIT 60`)
        .bind(userId, from, to)
        .all<{ date: string; computed_at: number; metrics_json: string }>();
      return Response.json(results.map((row) => ({ date: row.date, computedAt: row.computed_at, metrics: JSON.parse(row.metrics_json) })));
    }
    if (url.pathname === "/admin/days/requeue" && request.method === "POST") {
      // 期間の日をすべて計算し直す（計算方法を変えたときなど）
      const from = url.searchParams.get("from") ?? "";
      const to = url.searchParams.get("to") ?? "";
      if (!isValidDate(from) || !isValidDate(to) || from > to) return jsonError(400, "from と to を YYYY-MM-DD で指定してください");
      const dates: string[] = [];
      for (let date = from; date <= to && dates.length < 400; date = addDays(date, 1)) dates.push(date);
      await queueDateRange(env.DB, userId, dates, now);
      return Response.json({ ok: true, queued: dates.length });
    }
    if (url.pathname === "/admin/days/process" && request.method === "POST") {
      // 計算待ちの日を今すぐ計算する
      const max = Math.min(Math.max(Number(url.searchParams.get("max") ?? "10") || 10, 1), 50);
      const settings = await getSettings(env.DB, userId, env.TIME_ZONE || "Asia/Tokyo");
      return Response.json(await processDayQueue(env.DB, userId, settings, now, max));
    }
    if (url.pathname === "/admin/notify/test" && request.method === "POST") {
      // 通知が届くかを試す
      const config = apnsConfigFrom(env);
      if (!config) return jsonError(400, "APNS_KEY_P8 / APNS_KEY_ID / APNS_TEAM_ID / APNS_BUNDLE_ID が設定されていません");
      const result = await sendToAll(env.DB, userId, config, { title: "通知のテスト", body: "bunkoko から通知が届きました" }, now);
      return Response.json({ ok: result.delivered > 0, ...result });
    }
    if (url.pathname === "/admin/collect" && request.method === "POST") {
      const result = await collect(
        { db: env.DB, client: createClient(env), userId },
        { forceDeviceListRefresh: url.searchParams.get("refresh") === "1" },
      );
      return Response.json(result);
    }
  }

  return notFound();
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },

  async scheduled(_controller, env) {
    const userId = userIdOf(env);
    const result = await collect({ db: env.DB, client: createClient(env), userId });
    if (result.errors.length > 0) {
      console.warn("collector errors", JSON.stringify(result.errors));
    }
    console.log(
      `collected ${result.devicesOk}/${result.devicesTotal} devices, ${result.readingsWritten} readings`,
    );

    // ついでに、計算待ちの日を数日分ずつ計算する
    const settings = await getSettings(env.DB, userId, env.TIME_ZONE || "Asia/Tokyo");
    const now = Date.now();
    const days = await processDayQueue(env.DB, userId, settings, now);
    if (days.computed.length > 0) console.log(`computed days ${days.computed.join(", ")} (${days.remaining} left)`);

    // 寝る前の確認・朝のレポート（送る時刻になっていれば）
    const notified = await runNotifications(env.DB, userId, settings, apnsConfigFrom(env), now, (roles) => latestRoom(env.DB, userId, roles, now));
    if (notified.bedtime === "sent" || notified.morning === "sent") console.log("notifications", JSON.stringify(notified));
  },
} satisfies ExportedHandler<Env>;
