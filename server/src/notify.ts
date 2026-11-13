// 通知（docs/phase1-design.md の 4.4、要件定義書の FR-601〜606）
//   寝る前の確認: 目標の就寝時刻の60分前に、部屋が目安を外れていれば、何をすればよいかを伝える
//   朝のレポート: 起きたことがわかり、その夜の計算が終わったあとに、昨夜の要点を伝える
// - 睡眠中には送らない（寝る前の確認は就寝前の15分間だけ、朝のレポートは起きた20分後以降）
// - 同じ通知は1日1回まで（notifications_sent）

import { sendApns, type ApnsConfig, type ApnsMessage } from "./apns";
import { targetBedtimeFor, type DayMetrics } from "./days/compute";
import { loadRoles } from "./roles";
import type { UserSettings } from "./settings";
import { HOUR_MS, MINUTE_MS, addDays, localDate } from "./time";

export interface RoomNow {
  measuredAt: number | null;
  co2: number | null;
  temperature: number | null;
  humidity: number | null;
  lightLevel: number | null;
  inBed: boolean | null;
  ceilingLight: { on: boolean; brightness: number | null; colorTemperature: number | null } | null;
}

export interface Advice {
  kind: "co2" | "humidityLow" | "humidityHigh" | "temperatureLow" | "temperatureHigh" | "lightColor" | "lightBright";
  message: string;
}

/** 照明が「白っぽい」とみなす色温度（K） [TBD] */
export const WHITE_LIGHT_K = 4500;
/** 照明が「明るめ」とみなす明るさ（%） [TBD] */
export const BRIGHT_LIGHT_PERCENT = 80;
/** 寝る前の確認を送る時間の幅（この間の5分ごとの実行のどれかで送る） */
const BEDTIME_WINDOW_MS = 15 * MINUTE_MS;
const MORNING_DELAY_MS = 20 * MINUTE_MS;
const MORNING_LATEST_MS = 6 * HOUR_MS;

const n = (value: number, digits = 0) => value.toLocaleString("ja-JP", { maximumFractionDigits: digits });

/** 今の部屋の状態が目安を外れていれば、何をすればよいかを返す。照明は寝る前だけ見る */
export function evaluateRoom(room: RoomNow, settings: UserSettings, options: { includeLight?: boolean } = {}): Advice[] {
  const advice: Advice[] = [];
  if (room.measuredAt === null) return advice;
  const { co2Max, humidityMin, humidityMax, temperatureMin, temperatureMax } = settings.comfort;

  if (room.co2 !== null && room.co2 > co2Max) {
    advice.push({ kind: "co2", message: `CO2 が ${n(room.co2)}ppm です。寝る前に5分ほど換気しましょう` });
  }
  if (room.humidity !== null && room.humidity < humidityMin) {
    advice.push({ kind: "humidityLow", message: `湿度が ${n(room.humidity)}% と低めです。加湿をつけましょう` });
  } else if (room.humidity !== null && room.humidity > humidityMax) {
    advice.push({ kind: "humidityHigh", message: `湿度が ${n(room.humidity)}% と高めです。換気か除湿をしましょう` });
  }
  if (room.temperature !== null && temperatureMin !== null && room.temperature < temperatureMin) {
    advice.push({ kind: "temperatureLow", message: `室温が ${n(room.temperature, 1)}℃ と低めです。エアコンで少し温めましょう` });
  } else if (room.temperature !== null && temperatureMax !== null && room.temperature > temperatureMax) {
    advice.push({ kind: "temperatureHigh", message: `室温が ${n(room.temperature, 1)}℃ と高めです。エアコンで少し下げましょう` });
  }
  const light = room.ceilingLight;
  if (options.includeLight && light?.on) {
    if (light.colorTemperature !== null && light.colorTemperature >= WHITE_LIGHT_K) {
      advice.push({ kind: "lightColor", message: `照明が白っぽい色（${n(light.colorTemperature)}K）のままです。暖かい色に切り替えましょう` });
    }
    if (light.brightness !== null && light.brightness >= BRIGHT_LIGHT_PERCENT) {
      advice.push({ kind: "lightBright", message: `照明が明るめ（${n(light.brightness)}%）です。少し暗くしましょう` });
    }
  }
  return advice;
}

const duration = (minutes: number) => {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h > 0 ? `${h}時間${m > 0 ? `${m}分` : ""}` : `${m}分`;
};

/** 朝のレポートの文章 */
export function morningReport(metrics: DayMetrics, settings: UserSettings): ApnsMessage | null {
  const sleep = metrics.sleep;
  if (!sleep) return null;
  const parts: string[] = [];
  const details = [
    sleep.latencyMin !== null ? `寝つき${duration(sleep.latencyMin)}` : null,
    `中途覚醒${sleep.awakenings}回`,
  ].filter(Boolean);
  parts.push(`${duration(sleep.asleepMin)}眠りました（${details.join("・")}）。`);

  const { co2Max, temperatureMin, temperatureMax } = settings.comfort;
  if (metrics.room.co2Max !== null && metrics.room.co2Max > co2Max) {
    parts.push(`CO2 が最高 ${n(metrics.room.co2Max)}ppm まで上がりました。`);
  }
  const t = metrics.room;
  if (temperatureMin !== null && t.temperatureMin !== null && t.temperatureMin < temperatureMin) {
    parts.push(`室温が ${n(t.temperatureMin, 1)}℃ まで下がりました。`);
  } else if (temperatureMax !== null && t.temperatureMax !== null && t.temperatureMax > temperatureMax) {
    parts.push(`室温が ${n(t.temperatureMax, 1)}℃ まで上がりました。`);
  }
  if (!metrics.quality.usable) parts.push("Apple Watch のデータが少なめの夜でした。");
  return { title: "昨夜の睡眠", body: parts.join(""), data: { kind: "morning", date: metrics.date } };
}

/** 次に寝る前の確認をする夜（その夜の「起きる日」と、目標の就寝時刻） */
export function upcomingBedtime(now: number, settings: UserSettings): { nightDate: string; target: number } {
  const today = localDate(now, settings.timeZone);
  const lastNight = targetBedtimeFor(today, settings);
  return now <= lastNight + 6 * HOUR_MS
    ? { nightDate: today, target: lastNight }
    : { nightDate: addDays(today, 1), target: targetBedtimeFor(addDays(today, 1), settings) };
}

export interface NotifyEnv {
  APNS_KEY_P8?: string;
  APNS_KEY_ID?: string;
  APNS_TEAM_ID?: string;
  APNS_BUNDLE_ID?: string;
}

export function apnsConfigFrom(env: NotifyEnv): ApnsConfig | null {
  if (!env.APNS_KEY_P8 || !env.APNS_KEY_ID || !env.APNS_TEAM_ID || !env.APNS_BUNDLE_ID) return null;
  return { keyP8: env.APNS_KEY_P8, keyId: env.APNS_KEY_ID, teamId: env.APNS_TEAM_ID, bundleId: env.APNS_BUNDLE_ID };
}

type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

/** 登録されているすべての iPhone に送る。使われなくなった送り先は消す。1台でも届けば true */
export async function sendToAll(
  db: D1Database,
  userId: string,
  config: ApnsConfig,
  message: ApnsMessage,
  now: number,
  fetchFn?: FetchFn,
): Promise<{ delivered: number; failed: { status: number; reason: string }[] }> {
  const { results: devices } = await db
    .prepare(`SELECT token, environment FROM apns_devices WHERE user_id = ?1`)
    .bind(userId)
    .all<{ token: string; environment: "sandbox" | "production" }>();
  let delivered = 0;
  const failed: { status: number; reason: string }[] = [];
  for (const device of devices) {
    const result = await sendApns(config, device, message, now, fetchFn);
    if (result.ok) delivered += 1;
    else {
      failed.push({ status: result.status, reason: result.reason });
      if (result.unregistered) {
        await db.prepare(`DELETE FROM apns_devices WHERE user_id = ?1 AND token = ?2`).bind(userId, device.token).run();
      }
    }
  }
  return { delivered, failed };
}

async function alreadySent(db: D1Database, userId: string, kind: string, date: string): Promise<boolean> {
  const row = await db
    .prepare(`SELECT 1 AS x FROM notifications_sent WHERE user_id = ?1 AND kind = ?2 AND date = ?3`)
    .bind(userId, kind, date)
    .first<number>("x");
  return row !== null;
}

async function markSent(db: D1Database, userId: string, kind: string, date: string, now: number, body: string | null) {
  await db
    .prepare(`INSERT OR IGNORE INTO notifications_sent (user_id, kind, date, sent_at, body) VALUES (?1, ?2, ?3, ?4, ?5)`)
    .bind(userId, kind, date, now, body)
    .run();
}

export interface NotifyResult {
  bedtime: "sent" | "nothingToSay" | "notYet" | "alreadyDone" | "off";
  morning: "sent" | "notYet" | "alreadyDone" | "off";
}

/**
 * 5分ごとの実行で呼ぶ。送る時刻になっていて、まだ送っていなければ送る。
 * latestRoomFn は今の部屋の状態を返す関数（read.ts の latestRoom。循環 import を避けるため受け取る）
 */
export async function runNotifications(
  db: D1Database,
  userId: string,
  settings: UserSettings,
  config: ApnsConfig | null,
  now: number,
  latestRoomFn: (roles: Awaited<ReturnType<typeof loadRoles>>) => Promise<RoomNow>,
  fetchFn?: FetchFn,
): Promise<NotifyResult> {
  const result: NotifyResult = { bedtime: "off", morning: "off" };
  if (!config) return result;

  // 寝る前の確認
  if (settings.notifications.bedtime) {
    const { nightDate, target } = upcomingBedtime(now, settings);
    const checkAt = target - settings.notifications.bedtimeLeadMinutes * MINUTE_MS;
    if (now < checkAt || now >= checkAt + BEDTIME_WINDOW_MS) result.bedtime = "notYet";
    else if (await alreadySent(db, userId, "bedtime", nightDate)) result.bedtime = "alreadyDone";
    else {
      const room = await latestRoomFn(await loadRoles(db, userId, settings.deviceRoles));
      const advice = evaluateRoom(room, settings, { includeLight: true });
      if (advice.length === 0) {
        // 問題がなければ送らない（確認したことだけ記録する）
        await markSent(db, userId, "bedtime", nightDate, now, null);
        result.bedtime = "nothingToSay";
      } else {
        const body = advice.slice(0, 2).map((item) => item.message).join("。") + "。";
        const sent = await sendToAll(db, userId, config, { title: "寝る前の確認", body, data: { kind: "bedtime", date: nightDate } }, now, fetchFn);
        if (sent.delivered > 0) await markSent(db, userId, "bedtime", nightDate, now, body);
        result.bedtime = sent.delivered > 0 ? "sent" : "notYet";
      }
    }
  }

  // 朝のレポート
  if (settings.notifications.morningReport) {
    const today = localDate(now, settings.timeZone);
    const row = await db
      .prepare(`SELECT computed_at, sleep_end, metrics_json FROM days WHERE user_id = ?1 AND date = ?2`)
      .bind(userId, today)
      .first<{ computed_at: number; sleep_end: number | null; metrics_json: string }>();
    const ready =
      row !== null &&
      row.sleep_end !== null &&
      now >= row.sleep_end + MORNING_DELAY_MS &&
      now <= row.sleep_end + MORNING_LATEST_MS &&
      // 起きたあとに計算した行（夜の途中で計算した行ではない）
      row.computed_at >= row.sleep_end;
    if (!ready) result.morning = "notYet";
    else if (await alreadySent(db, userId, "morning", today)) result.morning = "alreadyDone";
    else {
      const message = morningReport(JSON.parse(row.metrics_json) as DayMetrics, settings);
      if (!message) result.morning = "notYet";
      else {
        const sent = await sendToAll(db, userId, config, message, now, fetchFn);
        if (sent.delivered > 0) await markSent(db, userId, "morning", today, now, message.body);
        result.morning = sent.delivered > 0 ? "sent" : "notYet";
      }
    }
  }
  return result;
}
