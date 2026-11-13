import { beforeEach, describe, expect, it } from "vitest";
import { processDayQueue } from "../src/days/queue";
import { handleRequest, type Env } from "../src/index";
import { buildHome, buildTimeline, loadDays } from "../src/read";
import { defaultSettings } from "../src/settings";
import { addDays } from "../src/time";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";
import { D, PREV, TZ, at, insertReading, resetSampleIds, seedNight, useDb } from "./helpers/night";

const settings = defaultSettings(TZ);
let db: FakeD1;

beforeEach(() => {
  db = createFakeD1();
  useDb(db);
  resetSampleIds();
});

/** 体の値だけを持つ、計算済みの行を入れる */
function insertDayRow(date: string, body: { restingHeartRate?: number; wristTemperature?: number; respiratoryRateAvg?: number; hrvAvg?: number }, usable = true) {
  const metrics = {
    date,
    sleep: { onset: 1, wake: 2, asleepMin: 420, segments: [{ start: 1, end: 2, stage: "asleepCore" }] },
    body: { hrAvg: 55, hrMin: 50, spo2Avg: null, spo2Min: null, breathingDisturbances: null, restingHeartRate: 50, wristTemperature: 35, respiratoryRateAvg: 14, hrvAvg: 50, ...body },
    quality: { usable },
  };
  db.sqlite
    .prepare(`INSERT INTO days (user_id, date, algo_version, computed_at, sleep_start, sleep_end, metrics_json) VALUES ('me', ?, 1, 0, 1, 2, ?)`)
    .run(date, JSON.stringify(metrics));
}

describe("loadDays（いつもとの差）", () => {
  it("直近28日の使える夜と比べて、いつもとの差を出し、3つのうち2つ以上が大きく違う日に印を付ける", async () => {
    const start = "2026-10-20";
    for (let i = 0; i < 20; i += 1) {
      const wobble = [0, 1, -1][i % 3];
      insertDayRow(addDays(start, i), { restingHeartRate: 50 + wobble, wristTemperature: 35 + wobble * 0.1, respiratoryRateAvg: 14 + wobble * 0.2, hrvAvg: 50 + wobble * 2 });
    }
    // 使えない夜（比べる対象に入れない）
    insertDayRow(addDays(start, 20), { restingHeartRate: 80 }, false);
    // いつもと違う日: 安静時心拍と手首皮膚温が大きく上がった
    insertDayRow(addDays(start, 21), { restingHeartRate: 58, wristTemperature: 35.8 });

    const days = await loadDays(db.d1, "me", addDays(start, 19), addDays(start, 21));
    expect(days.map((day) => day.date)).toEqual([addDays(start, 21), addDays(start, 20), addDays(start, 19)]);

    const unusualDay = days[0];
    expect(unusualDay.baseline.restingHeartRate?.median).toBe(50);
    expect(unusualDay.baseline.restingHeartRate?.z).toBeGreaterThan(2);
    expect(unusualDay.baseline.wristTemperature?.z).toBeGreaterThan(2);
    expect(unusualDay.unusual).toBe(true);
    expect(days[2].unusual).toBe(false);
    // 画面に要らない睡眠ステージの細かい区間は返さない
    expect(unusualDay.metrics.sleep).not.toHaveProperty("segments");
    expect(unusualDay.hrv7dAvg).not.toBeNull();
  });

  it("14日分に満たないうちは、いつもとの差を出さない", async () => {
    for (let i = 0; i < 5; i += 1) insertDayRow(addDays("2026-11-01", i), {});
    const [latest] = await loadDays(db.d1, "me", "2026-11-05", "2026-11-05");
    expect(latest.baseline.restingHeartRate).toBeNull();
    expect(latest.unusual).toBe(false);
  });
});

describe("buildHome / buildTimeline（1晩のデータで）", () => {
  beforeEach(async () => {
    seedNight();
    db.sqlite.prepare(`INSERT INTO day_queue (user_id, date, queued_at) VALUES ('me', ?, 0)`).run(D);
    await processDayQueue(db.d1, "me", settings, at(D, "09:00"));
  });

  it("ホーム: 今の部屋（30分以内の値）・助言・昨夜の要点・就寝まであと何分か", async () => {
    // 朝 07:55 の値が最新（CO2 1,200）
    const home = await buildHome(db.d1, "me", settings, at(D, "08:00"));
    expect(home.room.co2).toBe(1200);
    expect(home.room.temperature).toBe(21);
    expect(home.room.inBed).toBe(true);
    expect(home.roomAdvice.map((advice) => advice.kind)).toEqual(["co2"]);
    expect(home.lastNight?.date).toBe(D);
    expect(home.lastNight?.metrics.sleep?.asleepMin).toBe(425);
    // 今夜の目標 23:30 まで 15時間30分
    expect(home.bedtime).toEqual({ target: at(D, "23:30"), minutesUntil: 930 });
    expect(home.mealsWithoutEstimate).toBe(0);
  });

  it("ホーム: 部屋の値が古ければ（30分より前）、今の状態としては使わない", async () => {
    const home = await buildHome(db.d1, "me", settings, at(D, "09:00"));
    expect(home.room.measuredAt).toBeNull();
    expect(home.roomAdvice).toEqual([]);
  });

  it("夜の詳細: 寝る前2時間〜起きて1時間後の、心拍・部屋の値・出来事", async () => {
    const view = await buildTimeline(db.d1, "me", D, settings);
    expect(view?.window).toEqual({ start: at(PREV, "21:10"), end: at(D, "07:50") });
    expect(view?.sleep?.asleepMin).toBe(425);
    expect(view?.heartRate).toHaveLength(24 + 86); // 寝る前の24個 + 睡眠中の86個
    expect(view?.series.co2).toHaveLength(128); // 21:10〜07:45 の5分ごと
    expect(view?.series.co2[0]).toEqual([at(PREV, "21:10"), 700]);
    expect(view?.series.ceilingLight.find(([t]) => t === at(PREV, "23:30"))).toEqual([at(PREV, "23:30"), 0, 3000]);
    // カフェイン（15:00）は範囲（21:10〜）の6時間より前なので入らない。窓は、5分ごとの値と Webhook で同じ「開いた」を1回と数える
    expect(view?.events.map((event) => event.kind)).toEqual([
      "meal",
      "alcohol",
      "bath",
      "windowOpen",
      "windowClose",
      "lightsOff",
      "charger",
      "phoneLastUse",
    ]);
    expect(view?.events[0]).toEqual({ t: at(PREV, "20:30"), kind: "meal", label: "豚骨ラーメン、餃子" });
  });

  it("計算していない日は null", async () => {
    expect(await buildTimeline(db.d1, "me", "2026-11-01", settings)).toBeNull();
  });
});

describe("HTTP", () => {
  const env = (): Env => ({
    DB: db.d1,
    SWITCHBOT_TOKEN: "t",
    SWITCHBOT_SECRET: "s",
    WEBHOOK_SECRET: "w",
    ADMIN_TOKEN: "admin",
    APP_TOKEN: "app",
    AI_TASK_TOKEN: "ai",
    TIME_ZONE: TZ,
  });
  const get = (path: string, now: number) =>
    handleRequest(new Request(`https://x.example${path}`, { headers: { Authorization: "Bearer app" } }), env(), now);

  it("/api/home・/api/days・/api/nights/:date/timeline が使える", async () => {
    seedNight();
    insertReading("C1", at(D, "08:05"), "CO2", 650);
    db.sqlite.prepare(`INSERT INTO day_queue (user_id, date, queued_at) VALUES ('me', ?, 0)`).run(D);
    await processDayQueue(db.d1, "me", settings, at(D, "09:00"));

    const home = (await (await get("/api/home", at(D, "08:10"))).json()) as { ok: boolean; room: { co2: number } };
    expect(home.ok).toBe(true);
    expect(home.room.co2).toBe(650);

    const days = (await (await get(`/api/days?from=${PREV}&to=${D}`, at(D, "08:10"))).json()) as { days: { date: string }[] };
    expect(days.days.map((day) => day.date)).toEqual([D]);

    expect((await get(`/api/nights/${D}/timeline`, at(D, "08:10"))).status).toBe(200);
    expect((await get("/api/nights/2026-11-01/timeline", at(D, "08:10"))).status).toBe(404);
    expect((await get("/api/days?from=2026-01-01&to=2026-11-21", at(D, "08:10"))).status).toBe(400);
  });
});
