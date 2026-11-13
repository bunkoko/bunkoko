import { beforeEach, describe, expect, it } from "vitest";
import { handleRequest, type Env } from "../src/index";
import { MAX_BATCH_ITEMS } from "../src/ingest";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";

const BASE = "https://bunkoko.example.workers.dev";
const NOW = Date.parse("2026-11-21T00:00:00Z");
const APP = "app-token-789";

let db: FakeD1;
let env: Env;

beforeEach(() => {
  db = createFakeD1();
  env = {
    DB: db.d1,
    SWITCHBOT_TOKEN: "token",
    SWITCHBOT_SECRET: "secret",
    WEBHOOK_SECRET: "hook",
    ADMIN_TOKEN: "admin",
    APP_TOKEN: APP,
    TIME_ZONE: "Asia/Tokyo",
  };
});

function call(method: string, path: string, body?: unknown, token = APP): Promise<Response> {
  return handleRequest(
    new Request(`${BASE}${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    env,
    NOW,
  );
}

const rows = (sql: string, ...params: (string | number)[]) => db.sqlite.prepare(sql).all(...params);

const watch = { name: "Apple Watch", productType: "Watch7,1" };

/** 2026-11-20 23:40 JST 〜 2026-11-21 06:50 JST の睡眠をまねたデータ */
function sleepBatch() {
  return {
    samples: [
      { uuid: "S-1", type: "sleepAnalysis", start: Date.parse("2026-11-20T14:40:00Z"), end: Date.parse("2026-11-20T16:00:00Z"), valueText: "asleepCore", source: watch },
      { uuid: "S-2", type: "sleepAnalysis", start: Date.parse("2026-11-20T16:00:00Z"), end: Date.parse("2026-11-20T17:00:00Z"), valueText: "asleepDeep", source: watch },
      { uuid: "H-1", type: "heartRateVariabilitySDNN", start: Date.parse("2026-11-20T17:00:00Z"), end: Date.parse("2026-11-20T17:01:00Z"), value: 48.5, unit: "ms", source: watch },
    ],
    buckets: [
      { type: "heartRate", start: Date.parse("2026-11-20T15:00:00Z"), minutes: 5, avg: 56, min: 54, max: 59 },
      { type: "stepCount", start: Date.parse("2026-11-20T03:00:00Z"), minutes: 60, sum: 1200 },
    ],
    workouts: [
      { uuid: "W-1", activityType: "running", start: Date.parse("2026-11-20T09:00:00Z"), end: Date.parse("2026-11-20T09:30:00Z"), energyKcal: 320, avgHeartRate: 150 },
    ],
  };
}

describe("認証", () => {
  it("APP_TOKEN がないと 401", async () => {
    expect((await call("GET", "/api/settings", undefined, "wrong")).status).toBe(401);
  });

  it("管理用の合言葉では /api を使えない", async () => {
    expect((await call("GET", "/api/settings", undefined, "admin")).status).toBe(401);
  });

  it("知らない API は 404", async () => {
    expect((await call("GET", "/api/nothing")).status).toBe(404);
  });
});

describe("POST /api/health/batch", () => {
  it("HealthKit のデータを保存し、影響する日を計算待ちに入れる", async () => {
    const response = await call("POST", "/api/health/batch", sleepBatch());
    expect(response.status).toBe(200);
    const result = (await response.json()) as { accepted: Record<string, number>; daysQueued: string[] };
    expect(result.accepted).toEqual({ samples: 3, buckets: 2, workouts: 1, deleted: 0 });
    // 睡眠は日本時間 11/21 に終わるので 11/21（と翌日）、日中の歩数・ワークアウトは 11/20（と翌日 11/21）
    expect(result.daysQueued).toEqual(["2026-11-20", "2026-11-21", "2026-11-22"]);

    expect(rows(`SELECT uuid, type, value_num, value_text, source_product FROM health_samples ORDER BY uuid`)).toEqual([
      { uuid: "H-1", type: "heartRateVariabilitySDNN", value_num: 48.5, value_text: null, source_product: "Watch7,1" },
      { uuid: "S-1", type: "sleepAnalysis", value_num: null, value_text: "asleepCore", source_product: "Watch7,1" },
      { uuid: "S-2", type: "sleepAnalysis", value_num: null, value_text: "asleepDeep", source_product: "Watch7,1" },
    ]);
    expect(rows(`SELECT type, bucket_minutes, avg, sum FROM health_buckets ORDER BY type`)).toEqual([
      { type: "heartRate", bucket_minutes: 5, avg: 56, sum: null },
      { type: "stepCount", bucket_minutes: 60, avg: null, sum: 1200 },
    ]);
    expect(rows(`SELECT uuid, activity_type, energy_kcal FROM workouts`)).toEqual([
      { uuid: "W-1", activity_type: "running", energy_kcal: 320 },
    ]);
    expect(rows(`SELECT date FROM day_queue ORDER BY date`)).toHaveLength(3);
  });

  it("同じデータを送り直しても重複せず、集計は新しい値で上書きする", async () => {
    await call("POST", "/api/health/batch", sleepBatch());
    const again = sleepBatch();
    again.buckets[1].sum = 1500;
    await call("POST", "/api/health/batch", again);

    expect(rows(`SELECT COUNT(*) AS n FROM health_samples`)).toEqual([{ n: 3 }]);
    expect(rows(`SELECT sum FROM health_buckets WHERE type = 'stepCount'`)).toEqual([{ sum: 1500 }]);
  });

  it("HealthKit で削除されたデータは、削除済みにする", async () => {
    await call("POST", "/api/health/batch", sleepBatch());
    const response = await call("POST", "/api/health/batch", { deleted: ["S-2", "W-1"] });
    expect(response.status).toBe(200);

    expect(rows(`SELECT uuid, deleted FROM health_samples ORDER BY uuid`)).toEqual([
      { uuid: "H-1", deleted: 0 },
      { uuid: "S-1", deleted: 0 },
      { uuid: "S-2", deleted: 1 },
    ]);
    expect(rows(`SELECT deleted FROM workouts`)).toEqual([{ deleted: 1 }]);
    const result = (await response.json()) as { daysQueued: string[] };
    expect(result.daysQueued).toContain("2026-11-21");
  });

  it("形が正しくなければ 400 を返し、どこが悪いかを伝える", async () => {
    const response = await call("POST", "/api/health/batch", {
      samples: [{ uuid: "bad uuid!", type: "heartRate", start: 1, end: 2 }],
      buckets: [{ type: "stepCount", start: NOW, minutes: 15, sum: 10 }],
    });
    expect(response.status).toBe(400);
    const body = (await response.json()) as { details: string[] };
    expect(body.details.some((d) => d.startsWith("samples[0].uuid"))).toBe(true);
    expect(body.details.some((d) => d.startsWith("samples[0]:"))).toBe(true);
    expect(body.details.some((d) => d.startsWith("buckets[0].minutes"))).toBe(true);
    expect(rows(`SELECT COUNT(*) AS n FROM health_samples`)).toEqual([{ n: 0 }]);
  });

  it(`1回に ${MAX_BATCH_ITEMS} 件を超えたら 400`, async () => {
    const samples = Array.from({ length: MAX_BATCH_ITEMS + 1 }, (_, i) => ({
      uuid: `X-${i}`, type: "heartRateVariabilitySDNN", start: NOW, end: NOW, value: 40,
    }));
    expect((await call("POST", "/api/health/batch", { samples })).status).toBe(400);
  });

  it("上限ちょうどの件数は、すぐに処理できる", async () => {
    const buckets = Array.from({ length: MAX_BATCH_ITEMS }, (_, i) => ({
      type: "heartRate", start: NOW - (i + 1) * 5 * 60 * 1000, minutes: 5, avg: 60, min: 55, max: 66,
    }));
    const started = performance.now();
    const response = await call("POST", "/api/health/batch", { buckets });
    const elapsed = performance.now() - started;
    expect(response.status).toBe(200);
    expect(rows(`SELECT COUNT(*) AS n FROM health_buckets`)).toEqual([{ n: MAX_BATCH_ITEMS }]);
    // Workers の無料プランの CPU 時間（10ms）の目安。テスト環境は遅いので、大きく外れていないかだけを見る
    expect(elapsed).toBeLessThan(500);
  });
});

describe("POST /api/logs/batch", () => {
  const dinner = { id: "L-1", kind: "meal", occurredAt: Date.parse("2026-11-20T11:00:00Z"), data: { text: " 豚骨ラーメン、餃子 ", amount: "large", meal: "dinner" } };

  it("記録を保存し、中身を決まった形にそろえる", async () => {
    const response = await call("POST", "/api/logs/batch", {
      logs: [
        dinner,
        { id: "L-2", kind: "charger", occurredAt: Date.parse("2026-11-20T15:10:00Z"), data: { state: "connected" } },
        { id: "L-3", kind: "screenUsage", occurredAt: Date.parse("2026-11-20T15:05:00Z"), data: { cumulativeMinutes: 35, extra: "x" } },
        { id: "L-4", kind: "morningFeeling", occurredAt: Date.parse("2026-11-20T22:00:00Z"), data: { score: 4 } },
        { id: "L-5", kind: "bath", occurredAt: Date.parse("2026-11-20T12:30:00Z") },
      ],
    });
    expect(response.status).toBe(200);
    expect(rows(`SELECT id, kind, data_json FROM user_logs ORDER BY id`)).toEqual([
      { id: "L-1", kind: "meal", data_json: '{"text":"豚骨ラーメン、餃子","amount":"large","meal":"dinner"}' },
      { id: "L-2", kind: "charger", data_json: '{"state":"connected"}' },
      { id: "L-3", kind: "screenUsage", data_json: '{"cumulativeMinutes":35}' },
      { id: "L-4", kind: "morningFeeling", data_json: '{"score":4,"note":null}' },
      { id: "L-5", kind: "bath", data_json: "{}" },
    ]);
  });

  it("食事の内容を直したら、古い目安を消す（推定し直すため）。内容が同じなら残す", async () => {
    await call("POST", "/api/logs/batch", { logs: [dinner] });
    db.sqlite
      .prepare(`INSERT INTO meal_estimates (user_id, log_id, source, model, estimate_json, estimated_at) VALUES ('me', 'L-1', 'ai', 'x', '{}', 1)`)
      .run();

    await call("POST", "/api/logs/batch", { logs: [dinner] });
    expect(rows(`SELECT COUNT(*) AS n FROM meal_estimates`)).toEqual([{ n: 1 }]);

    await call("POST", "/api/logs/batch", { logs: [{ ...dinner, data: { text: "醤油ラーメン", amount: "normal" } }] });
    expect(rows(`SELECT COUNT(*) AS n FROM meal_estimates`)).toEqual([{ n: 0 }]);
  });

  it("取り消した記録は削除済みにする", async () => {
    await call("POST", "/api/logs/batch", { logs: [dinner] });
    const response = await call("POST", "/api/logs/batch", { deleted: ["L-1"] });
    expect(response.status).toBe(200);
    expect(rows(`SELECT deleted FROM user_logs`)).toEqual([{ deleted: 1 }]);
  });

  it("種類ごとの中身が正しくなければ 400", async () => {
    const response = await call("POST", "/api/logs/batch", {
      logs: [
        { id: "A", kind: "meal", occurredAt: NOW, data: { text: "" } },
        { id: "B", kind: "morningFeeling", occurredAt: NOW, data: { score: 7 } },
        { id: "C", kind: "charger", occurredAt: NOW, data: { state: "half" } },
        { id: "D", kind: "dance", occurredAt: NOW, data: {} },
      ],
    });
    expect(response.status).toBe(400);
    const body = (await response.json()) as { details: string[] };
    expect(body.details).toHaveLength(4);
  });
});

describe("設定", () => {
  it("まだ保存していなければ既定値を返す", async () => {
    const response = await call("GET", "/api/settings");
    const body = (await response.json()) as { settings: { timeZone: string; targetBedtime: string; comfort: { co2Max: number } } };
    expect(body.settings.timeZone).toBe("Asia/Tokyo");
    expect(body.settings.targetBedtime).toBe("23:30");
    expect(body.settings.comfort.co2Max).toBe(1000);
  });

  it("一部だけ変えられ、ほかの値は残る", async () => {
    await call("PUT", "/api/settings", { targetBedtime: "00:15", notifications: { bedtimeLeadMinutes: 45 }, deviceRoles: { W1: "window" } });
    await call("PUT", "/api/settings", { comfort: { temperatureMin: 18, temperatureMax: 22 } });
    const body = (await (await call("GET", "/api/settings")).json()) as {
      settings: { targetBedtime: string; notifications: { bedtime: boolean; bedtimeLeadMinutes: number }; comfort: { temperatureMin: number; co2Max: number }; deviceRoles: Record<string, string> };
    };
    expect(body.settings.targetBedtime).toBe("00:15");
    expect(body.settings.notifications).toEqual({ bedtime: true, bedtimeLeadMinutes: 45, morningReport: true });
    expect(body.settings.comfort.temperatureMin).toBe(18);
    expect(body.settings.comfort.co2Max).toBe(1000);
    expect(body.settings.deviceRoles).toEqual({ W1: "window" });
  });

  it("おかしな値は 400 で、保存しない", async () => {
    const response = await call("PUT", "/api/settings", { timeZone: "Mars/Base", targetWake: "7時", deviceRoles: { X: "toaster" } });
    expect(response.status).toBe(400);
    const body = (await response.json()) as { details: string[] };
    expect(body.details).toHaveLength(3);
    expect(rows(`SELECT COUNT(*) AS n FROM user_settings`)).toEqual([{ n: 0 }]);
  });

  it("タイムゾーンを変えると、日付の区切りもそれに合わせる", async () => {
    await call("PUT", "/api/settings", { timeZone: "UTC" });
    const response = await call("POST", "/api/logs/batch", {
      logs: [{ id: "T-1", kind: "bath", occurredAt: Date.parse("2026-11-20T15:30:00Z") }],
    });
    const body = (await response.json()) as { daysQueued: string[] };
    expect(body.daysQueued).toEqual(["2026-11-20", "2026-11-21"]);
  });
});

describe("POST /api/devices/apns", () => {
  it("通知の送り先を登録する（同じものは上書き）", async () => {
    const token = "A".repeat(64);
    expect((await call("POST", "/api/devices/apns", { token, environment: "sandbox" })).status).toBe(200);
    expect((await call("POST", "/api/devices/apns", { token, environment: "production" })).status).toBe(200);
    expect(rows(`SELECT token, environment FROM apns_devices`)).toEqual([{ token: "a".repeat(64), environment: "production" }]);
  });

  it("形が正しくなければ 400", async () => {
    expect((await call("POST", "/api/devices/apns", { token: "xyz", environment: "beta" })).status).toBe(400);
  });
});
