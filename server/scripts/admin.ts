// 公開したサーバーの管理用 API を、手元の Mac から呼ぶ。合言葉は .dev.vars から読み、画面には出さない
//
//   npm run admin -- summary               機器ごとの最新の値・役割、直近の取得結果、Webhook の数
//   npm run admin -- collect               すぐに1回センサーの値を取る（機器の一覧も取り直す）
//   npm run admin -- days [from] [to]      1日 = 1行の表の要点（省略すると直近7日）
//   npm run admin -- requeue <from> <to>   期間の日を計算し直す（計算のしかたを変えたとき）
//   npm run admin -- process [max]         計算待ちの日を今すぐ計算する
//
// .dev.vars の SERVER_URL（npm run secrets -- --set SERVER_URL https://…）と ADMIN_TOKEN を使う。

import { pathToFileURL } from "node:url";
import { inferRole } from "../src/roles";
import { addDays, isValidDate } from "../src/time";
import { loadDevVars } from "./dev-vars";

const TIME_ZONE = "Asia/Tokyo";

/** 出力に合言葉がまぎれていたら、名前に置き換える */
export function maskSecrets(text: string, secrets: Record<string, string | undefined>): string {
  let out = text;
  for (const [name, value] of Object.entries(secrets)) {
    if (value && value.length >= 8) out = out.split(value).join(`<${name}>`);
  }
  return out;
}

/** エポックミリ秒 → "2:07"（日本時間） */
export function clock(ms: number | null | undefined): string {
  if (typeof ms !== "number") return "-";
  return new Date(ms).toLocaleTimeString("ja-JP", { timeZone: TIME_ZONE, hour: "numeric", minute: "2-digit" });
}

/** 分 → "7:57" */
export function hm(minutes: number | null | undefined): string {
  if (typeof minutes !== "number") return "-";
  const total = Math.round(minutes);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

const value = (v: unknown, unit = "") => (v === null || v === undefined ? "-" : `${v}${unit}`);

interface DayRow {
  date: string;
  metrics: Record<string, Record<string, unknown> | null>;
}

/** 1日 = 1行の表の1行を、1行の文字にする */
export function formatDay(row: DayRow): string {
  const m = row.metrics;
  const sleep = m.sleep as { asleepMin?: number; onset?: number; wake?: number; efficiency?: number; deepMin?: number; remMin?: number; awakenings?: number; latencyMin?: number | null } | null;
  const quality = (m.quality ?? {}) as { heartRateCoverage?: number | null; usable?: boolean };
  const morning = (m.morning ?? {}) as { feeling?: number | null };
  const nextDay = (m.nextDay ?? {}) as { condition?: number | null; stress?: number | null };
  const room = (m.room ?? {}) as { co2Max?: number | null; temperatureAvg?: number | null };
  const parts = [
    row.date,
    sleep ? `睡眠 ${hm(sleep.asleepMin)}（${clock(sleep.onset)}〜${clock(sleep.wake)}）` : "睡眠 -",
    sleep ? `効率 ${Math.round((sleep.efficiency ?? 0) * 100)}%` : "",
    sleep ? `深い ${value(sleep.deepMin, "分")} レム ${value(sleep.remMin, "分")}` : "",
    sleep ? `中途覚醒 ${value(sleep.awakenings, "回")} 寝つき ${value(sleep.latencyMin, "分")}` : "",
    `心拍の記録 ${quality.heartRateCoverage === null || quality.heartRateCoverage === undefined ? "-" : `${Math.round(quality.heartRateCoverage * 100)}%`}`,
    quality.usable ? "使える夜" : "使えない夜",
    `体感 ${value(morning.feeling)} 調子 ${value(nextDay.condition)} ストレス ${value(nextDay.stress)}`,
    `CO2最高 ${value(room.co2Max, "ppm")} 室温 ${value(room.temperatureAvg, "℃")}`,
  ];
  return parts.filter(Boolean).join("  ");
}

interface SummaryDevice {
  deviceId: string;
  name: string | null;
  type: string | null;
  lastPolledAt: string | null;
  values: Record<string, unknown>;
}

export const ROLE_LABELS: Record<string, string> = {
  co2: "CO2・室温",
  bedPresence: "在床（ベッド）",
  window: "窓",
  door: "ドア",
  ceilingLight: "照明",
  curtain: "カーテン",
  purifier: "加湿空気清浄機",
  tv: "テレビ",
  hub: "ハブ",
};

/** 役割の名前。決まらないときは、名前の付け方を伝える */
export function roleLabel(type: string | null, name: string | null): string {
  const role = inferRole(type, name);
  if (role) return ROLE_LABELS[role] ?? role;
  if (type === "Contact Sensor") return "役割なし（名前を見直す: 名前に「窓」か「ドア」を入れる）";
  if (/^Plug Mini/.test(type ?? "")) return "役割なし（名前を見直す: 名前に「加湿」か「テレビ」を入れる）";
  return "このツールでは使わない";
}

/** 機器ごとの役割と最新の値 */
export function formatDevice(device: SummaryDevice): string {
  const label = roleLabel(device.type, device.name);
  const values = Object.entries(device.values)
    .map(([key, v]) => `${key}=${v}`)
    .join(" ");
  const polled = device.lastPolledAt ? clock(Date.parse(device.lastPolledAt)) : "-";
  return `${device.name ?? "(名前なし)"}（${device.type ?? "?"}）→ ${label}  最後に取得 ${polled}  ${values}`;
}

function today(): string {
  return new Date().toLocaleDateString("sv-SE", { timeZone: TIME_ZONE });
}

async function main(): Promise<void> {
  const [command, arg1, arg2] = process.argv.slice(2);
  const vars = loadDevVars();
  const base = (process.env.SERVER_URL ?? vars.SERVER_URL ?? "").replace(/\/+$/, "");
  const token = process.env.ADMIN_TOKEN ?? vars.ADMIN_TOKEN ?? "";
  if (!base || !token) {
    console.error("SERVER_URL と ADMIN_TOKEN を .dev.vars に入れてください（npm run secrets -- --set SERVER_URL https://…）");
    process.exit(1);
  }
  const secrets = { ADMIN_TOKEN: token, WEBHOOK_SECRET: vars.WEBHOOK_SECRET, APP_TOKEN: vars.APP_TOKEN, AI_TASK_TOKEN: vars.AI_TASK_TOKEN };

  const call = async (method: string, path: string): Promise<unknown> => {
    const response = await fetch(`${base}${path}`, { method, headers: { Authorization: `Bearer ${token}` } });
    const text = await response.text();
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${maskSecrets(text, secrets)}`);
    return JSON.parse(text);
  };
  const print = (data: unknown) => console.log(maskSecrets(JSON.stringify(data, null, 2), secrets));

  switch (command) {
    case "summary": {
      const summary = (await call("GET", "/admin/summary")) as {
        devices: SummaryDevice[];
        recentRuns: { startedAt: string; devicesOk: number; devicesTotal: number; errors: unknown[] }[];
        webhook: { eventsReceived: number; lastReceivedAt: string | null };
        switchBotApiCallsLast24h: number;
      };
      console.log(`機器（${summary.devices.length}台）`);
      for (const device of summary.devices) console.log(`  ${maskSecrets(formatDevice(device), secrets)}`);
      console.log("直近の取得");
      for (const run of summary.recentRuns) {
        const errors = run.errors.length > 0 ? `  エラー: ${maskSecrets(JSON.stringify(run.errors), secrets)}` : "";
        console.log(`  ${clock(Date.parse(run.startedAt))}  ${run.devicesOk}/${run.devicesTotal}台${errors}`);
      }
      console.log(`Webhook: ${summary.webhook.eventsReceived}件（最後 ${summary.webhook.lastReceivedAt ? clock(Date.parse(summary.webhook.lastReceivedAt)) : "-"}）`);
      console.log(`SwitchBot API の利用（24時間）: ${summary.switchBotApiCallsLast24h}回 / 上限 10,000回`);
      return;
    }
    case "collect":
      print(await call("POST", "/admin/collect?refresh=1"));
      return;
    case "days": {
      const to = arg2 ?? arg1 ?? today();
      const from = arg2 ? arg1 : addDays(to, -6);
      if (!isValidDate(from) || !isValidDate(to)) throw new Error("日付は YYYY-MM-DD で書いてください");
      const rows = (await call("GET", `/admin/days?from=${from}&to=${to}`)) as DayRow[];
      if (rows.length === 0) console.log("まだ計算された日がありません");
      for (const row of rows) console.log(formatDay(row));
      return;
    }
    case "requeue":
      if (!arg1 || !arg2 || !isValidDate(arg1) || !isValidDate(arg2)) throw new Error("npm run admin -- requeue <from> <to>（YYYY-MM-DD）");
      print(await call("POST", `/admin/days/requeue?from=${arg1}&to=${arg2}`));
      return;
    case "process":
      print(await call("POST", `/admin/days/process?max=${Number(arg1 ?? 10) || 10}`));
      return;
    default:
      console.log(`使い方:
  npm run admin -- summary
  npm run admin -- collect
  npm run admin -- days [from] [to]
  npm run admin -- requeue <from> <to>
  npm run admin -- process [max]`);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  });
}
