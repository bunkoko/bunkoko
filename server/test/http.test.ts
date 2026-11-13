import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseTimeParam } from "../src/admin";
import { collect } from "../src/collector";
import { handleRequest, type Env } from "../src/index";
import type { DeviceStatus } from "../src/switchbot";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";
import { DEVICES, STATUSES } from "./helpers/fixtures";

const NOW = Date.parse("2026-11-20T14:00:00Z");
const BASE = "https://bunkoko.example.workers.dev";

let db: FakeD1;
let env: Env;

beforeEach(() => {
  db = createFakeD1();
  env = {
    DB: db.d1,
    SWITCHBOT_TOKEN: "token",
    SWITCHBOT_SECRET: "secret",
    WEBHOOK_SECRET: "hook-secret-123",
    ADMIN_TOKEN: "admin-token-456",
    APP_TOKEN: "app-token-789",
    AI_TASK_TOKEN: "ai-token",
    USER_ID: "me",
    TIME_ZONE: "Asia/Tokyo",
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const admin = (path: string, init: RequestInit = {}) =>
  handleRequest(
    new Request(`${BASE}${path}`, { ...init, headers: { Authorization: "Bearer admin-token-456" } }),
    env,
    NOW,
  );

const postWebhook = (secret: string, body: string) =>
  handleRequest(new Request(`${BASE}/webhooks/switchbot/${secret}`, { method: "POST", body }), env, NOW);

/** 公式資料の例に合わせた、人感センサー Pro の Webhook */
const presenceEvent = (detectionState: string, timeOfSample: number) =>
  JSON.stringify({
    eventType: "changeReport",
    eventVersion: "1",
    context: { deviceType: "WoPresence", deviceMac: "P1:P1:P1:P1:P1:P1", detectionState, battery: 100, lightLevel: 19, timeOfSample },
  });

describe("GET /", () => {
  it("動いていることを返す", async () => {
    const response = await handleRequest(new Request(`${BASE}/`), env, NOW);
    expect(response.status).toBe(200);
  });
});

describe("Webhook", () => {
  it("合言葉が違うときは、URL が存在しないのと同じ応答にする", async () => {
    const response = await postWebhook("wrong", presenceEvent("DETECTED", NOW));
    expect(response.status).toBe(404);
    expect(db.sqlite.prepare(`SELECT COUNT(*) AS n FROM webhook_events`).get()).toEqual({ n: 0 });
  });

  it("正しい合言葉なら、生データと値を機器の時刻で保存する", async () => {
    const sampledAt = NOW - 3_000;
    const response = await postWebhook("hook-secret-123", presenceEvent("NOT_DETECTED", sampledAt));
    expect(response.status).toBe(200);

    const event = db.sqlite.prepare(`SELECT device_id, device_type, sampled_at, event_type FROM webhook_events`).get();
    expect(event).toEqual({ device_id: "P1P1P1P1P1P1", device_type: "WoPresence", sampled_at: sampledAt, event_type: "changeReport" });

    const readings = db.sqlite
      .prepare(`SELECT metric, value_num, value_text, measured_at, source FROM readings ORDER BY metric`)
      .all();
    expect(readings).toEqual([
      { metric: "battery", value_num: 100, value_text: null, measured_at: sampledAt, source: "webhook" },
      { metric: "detectionState", value_num: null, value_text: "NOT_DETECTED", measured_at: sampledAt, source: "webhook" },
      { metric: "lightLevel", value_num: 19, value_text: null, measured_at: sampledAt, source: "webhook" },
    ]);
  });

  it("温度が華氏で届いたら摂氏にそろえる", async () => {
    const body = JSON.stringify({
      eventType: "changeReport",
      context: { deviceType: "MeterPro(CO2)", deviceMac: "C1C1C1C1C1C1", temperature: 72.5, scale: "FAHRENHEIT", humidity: 40, CO2: 900, timeOfSample: NOW },
    });
    await postWebhook("hook-secret-123", body);
    const temperature = db.sqlite.prepare(`SELECT value_num FROM readings WHERE metric = 'temperature'`).get();
    expect(temperature).toEqual({ value_num: 22.5 });
  });

  it("JSON でないときは 400 を返す", async () => {
    const response = await postWebhook("hook-secret-123", "not json");
    expect(response.status).toBe(400);
  });

  it("機器の情報がないイベントも、生データだけは保存する", async () => {
    const response = await postWebhook("hook-secret-123", JSON.stringify({ eventType: "scene" }));
    expect(response.status).toBe(200);
    expect(db.sqlite.prepare(`SELECT COUNT(*) AS n FROM webhook_events`).get()).toEqual({ n: 1 });
    expect(db.sqlite.prepare(`SELECT COUNT(*) AS n FROM readings`).get()).toEqual({ n: 0 });
  });
});

describe("管理 API", () => {
  const fakeClient = {
    listDevices: async () => ({ deviceList: DEVICES, infraredRemoteList: [] }),
    getStatus: async (deviceId: string): Promise<DeviceStatus> => STATUSES[deviceId],
  };

  it("合言葉がないと 401 を返す", async () => {
    const response = await handleRequest(new Request(`${BASE}/admin/summary`), env, NOW);
    expect(response.status).toBe(401);
  });

  it("合言葉が違うと 401 を返す", async () => {
    const response = await handleRequest(
      new Request(`${BASE}/admin/summary`, { headers: { Authorization: "Bearer nope" } }),
      env,
      NOW,
    );
    expect(response.status).toBe(401);
  });

  it("summary で機器ごとの最新の値と API の利用回数を返す", async () => {
    await collect({ db: db.d1, client: fakeClient, userId: "me", now: () => NOW - 60_000 });
    await postWebhook("hook-secret-123", presenceEvent("DETECTED", NOW - 1_000));

    const response = await admin("/admin/summary");
    expect(response.status).toBe(200);
    const summary = (await response.json()) as {
      devices: { deviceId: string; values: Record<string, unknown>; lastPolledAt: string }[];
      switchBotApiCallsLast24h: number;
      webhook: { eventsReceived: number };
      recentRuns: unknown[];
    };

    expect(summary.devices).toHaveLength(9);
    const co2 = summary.devices.find((device) => device.deviceId === "C1C1C1C1C1C1");
    expect(co2?.values).toEqual({ CO2: 1203, battery: 90, humidity: 50, temperature: 22.1 });
    expect(co2?.lastPolledAt).toBe(new Date(NOW - 60_000).toISOString());
    // 機器一覧1回 + 状態9回
    expect(summary.switchBotApiCallsLast24h).toBe(10);
    expect(summary.webhook.eventsReceived).toBe(1);
    expect(summary.recentRuns).toHaveLength(1);
  });

  it("CSV で値を書き出し、日本時間の列も付ける", async () => {
    await collect({ db: db.d1, client: fakeClient, userId: "me", now: () => Date.parse("2026-11-20T13:00:00Z") });

    const response = await admin("/admin/readings.csv?from=2026-11-20&to=2026-11-21&metric=CO2");
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/csv");
    const lines = (await response.text()).replace(/^﻿/, "").trim().split("\r\n");
    expect(lines).toEqual([
      "time_utc,time_local,device_id,device_name,device_type,metric,value,source",
      "2026-11-20T13:00:00.000Z,2026-11-20 22:00:00,C1C1C1C1C1C1,CO2センサー,MeterPro(CO2),CO2,1203,poll",
    ]);
  });

  it("CSV は一度に7日分まで", async () => {
    const response = await admin("/admin/readings.csv?from=2026-11-01&to=2026-11-20");
    expect(response.status).toBe(400);
  });

  it("collect で今すぐ取得できる（SwitchBot へは署名付きで問い合わせる）", async () => {
    const requests: Request[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      requests.push(request);
      const url = new URL(request.url);
      if (url.pathname === "/v1.1/devices") {
        return Response.json({ statusCode: 100, message: "success", body: { deviceList: DEVICES, infraredRemoteList: [] } });
      }
      const deviceId = url.pathname.split("/")[3];
      return Response.json({ statusCode: 100, message: "success", body: STATUSES[deviceId] });
    });

    const response = await admin("/admin/collect", { method: "POST" });
    expect(response.status).toBe(200);
    const result = (await response.json()) as { devicesOk: number };
    expect(result.devicesOk).toBe(9);
    expect(requests).toHaveLength(10);
    expect(requests.every((request) => request.headers.get("sign") && request.headers.get("t"))).toBe(true);
    expect(new URL(requests[0].url).origin).toBe("https://api.switch-bot.com");
  });
});

describe("parseTimeParam", () => {
  it("日付だけなら、そのタイムゾーンの0時にする", () => {
    expect(parseTimeParam("2026-11-20", "Asia/Tokyo")).toBe(Date.parse("2026-11-19T15:00:00Z"));
  });

  it("ISO 形式の日時はそのまま使う", () => {
    expect(parseTimeParam("2026-11-20T01:02:03Z", "Asia/Tokyo")).toBe(Date.parse("2026-11-20T01:02:03Z"));
  });

  it("読めない値は null", () => {
    expect(parseTimeParam("tomorrow", "Asia/Tokyo")).toBeNull();
  });
});
