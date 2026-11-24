// 無料プランの「1日に読める行数」（D1 は 1日 500万行）に収めるための確認。
// 主な処理を一通り動かし、実行した SQL がすべて「索引で範囲を絞って読む」計画になっているかを調べる。
// 利用者ごとに全件を読む計画（データが増えるほど読む行数が増える）があれば失敗する。

import { beforeEach, describe, expect, it } from "vitest";
import { collect } from "../src/collector";
import { computeDay } from "../src/days/compute";
import { runScheduled, handleRequest, type Env } from "../src/index";
import { loadRoles } from "../src/roles";
import { defaultSettings } from "../src/settings";
import type { DeviceListBody, DeviceStatus, SwitchBotClient } from "../src/switchbot";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";
import { DEVICES, STATUSES } from "./helpers/fixtures";
import { D, TZ, at, resetSampleIds, seedNight, useDb } from "./helpers/night";

/** データが増え続ける表 */
const GROWING = new Set(["readings", "health_samples", "health_buckets", "user_logs", "webhook_events", "workouts", "days"]);
const KEYWORDS = new Set(["where", "left", "join", "inner", "on", "indexed", "group", "order", "limit", "union", "as", "select", "set"]);

/** SQL の別名（FROM readings AS r の r など）から、表の名前を引けるようにする */
function aliases(sql: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const match of sql.matchAll(/\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?/gi)) {
    const table = match[1];
    map.set(table, table);
    if (match[2] && !KEYWORDS.has(match[2].toLowerCase())) map.set(match[2], table);
  }
  return map;
}

/** 問題のある計画の行を返す */
function badPlanLines(db: FakeD1, sql: string, params: unknown[]): string[] {
  const plan = db.sqlite.prepare(`EXPLAIN QUERY PLAN ${sql}`).all(...(params as never[])) as { detail: string }[];
  const names = aliases(sql);
  const bad: string[] = [];
  for (const { detail } of plan) {
    // 例: "SEARCH l USING PRIMARY KEY (user_id=? AND id=?)"、"SEARCH l EXISTS USING ..."、"SCAN readings"
    const match = /^(SCAN|SEARCH) (\w+)[^(]*(?:\((.*)\))?$/.exec(detail);
    if (!match) continue;
    const table = names.get(match[2]) ?? match[2];
    if (!GROWING.has(table)) continue;
    const constraints = (match[3] ?? "").split(" AND ").filter(Boolean);
    // 最新の時刻だけを主キーの索引で探す（MAX の最適化。1回の探索で済む）
    const maxLookup = /SELECT MAX\(r\.measured_at\) FROM readings AS r WHERE r\.user_id = d\.user_id AND r\.device_id = d\.device_id/.test(sql);
    if (match[1] === "SCAN") bad.push(detail);
    else if (constraints.length <= 1) bad.push(detail);
    else if (table === "readings" && !constraints.some((c) => c.startsWith("measured_at")) && !maxLookup) bad.push(detail);
  }
  return bad;
}

const settings = defaultSettings(TZ);
const NOW = at(D, "09:00");
let db: FakeD1;
let env: Env;

beforeEach(() => {
  db = createFakeD1();
  useDb(db);
  resetSampleIds();
  seedNight();
  env = { DB: db.d1, WEBHOOK_SECRET: "hook-secret", ADMIN_TOKEN: "admin", APP_TOKEN: "app", AI_TASK_TOKEN: "ai", TIME_ZONE: TZ } as Env;
});

const api = (method: string, path: string, token: string, body?: unknown) =>
  handleRequest(
    new Request(`https://x.example${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    env,
    NOW,
  );

class StubClient {
  async listDevices(): Promise<DeviceListBody> {
    return { deviceList: DEVICES, infraredRemoteList: [] };
  }
  async getStatus(deviceId: string): Promise<DeviceStatus> {
    return STATUSES[deviceId] as DeviceStatus;
  }
}

describe("問い合わせの計画", () => {
  it("主な処理の SQL は、どれも索引で範囲を絞って読む", async () => {
    db.executed.length = 0;

    // センサーの取得・Webhook
    await collect({ db: db.d1, client: new StubClient() as unknown as SwitchBotClient, userId: "me", now: () => NOW }, { forceDeviceListRefresh: true });
    await handleRequest(
      new Request("https://x.example/webhooks/switchbot/hook-secret", {
        method: "POST",
        body: JSON.stringify({ eventType: "changeReport", eventVersion: "1", context: { deviceType: "WoContact", deviceMac: "W1:W1:W1:W1:W1:W1", openState: "open", timeOfSample: NOW } }),
      }),
      env,
      NOW,
    );

    // アプリからの受け取り
    await api("POST", "/api/health/batch", "app", {
      samples: [{ uuid: "S-new", type: "heartRateVariabilitySDNN", start: NOW - 3_600_000, end: NOW - 3_600_000, value: 40 }],
      buckets: [{ type: "heartRate", start: NOW - 300_000, minutes: 5, avg: 60, min: 55, max: 65 }],
    });
    await api("POST", "/api/logs/batch", "app", {
      logs: [
        { id: "L-meal", kind: "meal", occurredAt: NOW - 3_600_000, data: { text: "カレー" } },
        { id: "checkin-2026-11-21", kind: "dayCheckin", occurredAt: NOW, data: { day: D, condition: 4, stress: 2 } },
      ],
      deleted: ["L-meal"],
    });

    // 1日 = 1行の計算と、5分ごとの処理
    await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    await runScheduled(env, NOW);

    // 食事の目安（生成AIのタスク・利用者の修正）
    const estimate = { kcal: 850, proteinG: 30, fatG: 35, carbsG: 100, caffeineMg: 0, alcoholDrinks: 0, greasiness: 3, spiciness: 1, volume: 3, confidence: "medium", items: [] };
    await api("POST", "/api/logs/batch", "app", { logs: [{ id: "M-1", kind: "meal", occurredAt: NOW - 7_200_000, data: { text: "親子丼" } }] });
    expect((await api("POST", "/ai/meals/estimates", "ai", { estimates: [{ id: "M-1", model: "m", estimate }] })).status).toBe(200);
    expect((await api("PUT", "/api/meals/M-1/estimate", "app", { estimate: { ...estimate, kcal: 700 } })).status).toBe(200);

    // 管理用の操作
    expect((await api("POST", "/admin/days/requeue?from=2026-11-15&to=2026-11-21", "admin")).status).toBe(200);
    expect((await api("POST", "/admin/days/process?max=3", "admin")).status).toBe(200);
    expect((await api("GET", "/admin/readings.csv?from=2026-11-20&to=2026-11-21", "admin")).status).toBe(200);

    // アプリ・生成AIのタスク・管理用に見せる API
    for (const path of ["/api/home", "/api/days?from=2026-11-01&to=2026-11-21", `/api/nights/${D}/timeline`, "/api/meals?from=2026-11-15&to=2026-11-21", "/api/settings"]) {
      expect((await api("GET", path, "app")).status).toBe(200);
    }
    expect((await api("GET", "/ai/meals/pending", "ai")).status).toBe(200);
    for (const path of ["/admin/summary", "/admin/days?from=2026-11-15&to=2026-11-21"]) {
      expect((await api("GET", path, "admin")).status).toBe(200);
    }

    const problems: string[] = [];
    const seen = new Set<string>();
    for (const { sql, params } of db.executed) {
      if (seen.has(sql)) continue;
      seen.add(sql);
      for (const line of badPlanLines(db, sql, params)) problems.push(`${line}\n    ${sql.replace(/\s+/g, " ").slice(0, 200)}`);
    }
    expect(problems).toEqual([]);
    expect(seen.size).toBeGreaterThan(30);
  });
});
