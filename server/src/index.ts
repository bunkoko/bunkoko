// Cloudflare Workers の入り口
//   - 5分ごと（cron）: SwitchBot の全機器の状態を取得して保存する
//   - HTTP: Webhook の受け取りと、動作確認用の管理 API

import { buildSummary, exportReadingsCsv } from "./admin";
import { collect } from "./collector";
import { SwitchBotClient } from "./switchbot";
import { handleSwitchBotWebhook } from "./webhook";

export interface Env {
  DB: D1Database;
  SWITCHBOT_TOKEN: string;
  SWITCHBOT_SECRET: string;
  /** Webhook の URL に含める合言葉。SwitchBot の Webhook には署名がないため、URL で見分ける */
  WEBHOOK_SECRET: string;
  /** 管理 API を使うための合言葉（Authorization: Bearer <ADMIN_TOKEN>） */
  ADMIN_TOKEN: string;
  USER_ID?: string;
  TIME_ZONE?: string;
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

/** 文字列を、長さや内容から推測されにくい方法で比べる */
async function secretsMatch(given: string, expected: string): Promise<boolean> {
  if (!expected) return false;
  const encoder = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(given)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const left = new Uint8Array(a);
  const right = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) diff |= left[i] ^ right[i];
  return diff === 0;
}

async function isAdmin(request: Request, env: Env): Promise<boolean> {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  return match !== null && (await secretsMatch(match[1].trim(), env.ADMIN_TOKEN));
}

const notFound = () => new Response("not found", { status: 404 });

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

  if (url.pathname.startsWith("/admin/")) {
    if (!(await isAdmin(request, env))) {
      return new Response("unauthorized", { status: 401, headers: { "WWW-Authenticate": "Bearer" } });
    }
    if (url.pathname === "/admin/summary" && request.method === "GET") {
      return Response.json(await buildSummary(env.DB, userId, now));
    }
    if (url.pathname === "/admin/readings.csv" && request.method === "GET") {
      return exportReadingsCsv(env.DB, userId, url, env.TIME_ZONE || "Asia/Tokyo", now);
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
    const result = await collect({ db: env.DB, client: createClient(env), userId: userIdOf(env) });
    if (result.errors.length > 0) {
      console.warn("collector errors", JSON.stringify(result.errors));
    }
    console.log(
      `collected ${result.devicesOk}/${result.devicesTotal} devices, ${result.readingsWritten} readings`,
    );
  },
} satisfies ExportedHandler<Env>;
