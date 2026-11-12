// SwitchBot から届く Webhook（機器の状態が変わったときの通知）を保存する。
// 人感センサー Pro の在床や開閉センサーの開け閉めは、5分ごとの取得だと時刻がずれたり
// 短い変化を見逃したりするため、こちらで正確な時刻を記録する。

import { insertReadings, insertWebhookEvent } from "./db";
import { fahrenheitToCelsius, normalizeDeviceId, toReadings, type Reading } from "./normalize";

export const MAX_WEBHOOK_BYTES = 64 * 1024;

export interface ParsedWebhookEvent {
  eventType: string | null;
  deviceId: string | null;
  deviceType: string | null;
  sampledAt: number;
  readings: Reading[];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** timeOfSample はミリ秒。秒で届いた場合にも備えて、桁数で判定する */
function toEpochMillis(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return null;
  return value < 1e12 ? Math.round(value * 1000) : Math.round(value);
}

export function parseWebhookEvent(payload: unknown, receivedAt: number): ParsedWebhookEvent {
  const root = asRecord(payload);
  const context = asRecord(root?.context);
  const eventType = typeof root?.eventType === "string" ? root.eventType : null;
  if (!context) {
    return { eventType, deviceId: null, deviceType: null, sampledAt: receivedAt, readings: [] };
  }

  const deviceId = typeof context.deviceMac === "string" ? normalizeDeviceId(context.deviceMac) : null;
  const deviceType = typeof context.deviceType === "string" ? context.deviceType : null;
  const sampledAt = toEpochMillis(context.timeOfSample) ?? receivedAt;

  // 温度の単位がアプリで華氏に設定されている場合は、摂氏にそろえる
  const values = { ...context };
  if (context.scale === "FAHRENHEIT" && typeof values.temperature === "number") {
    values.temperature = fahrenheitToCelsius(values.temperature);
  }

  return { eventType, deviceId, deviceType, sampledAt, readings: deviceId ? toReadings(values) : [] };
}

export async function handleSwitchBotWebhook(
  request: Request,
  db: D1Database,
  userId: string,
  now: () => number = () => Date.now(),
): Promise<Response> {
  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  if (declaredLength > MAX_WEBHOOK_BYTES) return new Response("payload too large", { status: 413 });

  const text = await request.text();
  if (text.length > MAX_WEBHOOK_BYTES) return new Response("payload too large", { status: 413 });

  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    return new Response("invalid json", { status: 400 });
  }

  const receivedAt = now();
  const event = parseWebhookEvent(payload, receivedAt);
  await insertWebhookEvent(db, {
    userId,
    receivedAt,
    eventType: event.eventType,
    deviceId: event.deviceId,
    deviceType: event.deviceType,
    sampledAt: event.sampledAt,
    rawJson: text,
  });
  if (event.deviceId) {
    await insertReadings(db, userId, "webhook", [
      { deviceId: event.deviceId, measuredAt: event.sampledAt, readings: event.readings },
    ]);
  }
  return Response.json({ ok: true });
}
