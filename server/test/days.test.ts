// 1晩を丸ごとまねたデータで、1日 = 1行の表の計算を確かめる。
// 期待する値は、下のデータから手で計算したもの。
//
// 2026-11-21（起きた日）の行。日本時間で:
//   前の夜: 11/19 23:30 〜 11/20 07:00（日中は 11/20 07:00 から）
//   昼寝: 11/20 14:00〜14:30 / ワークアウト: 18:00〜18:45 / 夕食: 20:30 / 入浴: 21:40
//   布団に入る: 23:10（人感センサー）/ 寝つく: 23:40 / 起きる: 06:50
//   CO2: 寝る前 700、寝ついてから 03:00 まで 900、03:00 から 1,200

import { beforeEach, describe, expect, it } from "vitest";
import { computeDay } from "../src/days/compute";
import { processDayQueue } from "../src/days/queue";
import { loadRoles } from "../src/roles";
import { defaultSettings } from "../src/settings";
import { MINUTE_MS, localTimeOn } from "../src/time";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";

const TZ = "Asia/Tokyo";
const D = "2026-11-21";
const PREV = "2026-11-20";
const at = (date: string, hhmm: string) => localTimeOn(date, hhmm, TZ);
const settings = defaultSettings(TZ);

let db: FakeD1;

function insertDevice(id: string, type: string, name: string) {
  db.sqlite
    .prepare(`INSERT INTO devices (user_id, device_id, device_name, device_type, enable_cloud_service, active, first_seen_at, last_seen_at, raw_json) VALUES ('me', ?, ?, ?, 1, 1, 0, 0, '{}')`)
    .run(id, name, type);
}

const insertReading = db_insertReading;
function db_insertReading(deviceId: string, t: number, metric: string, value: number | string, source = "poll") {
  db.sqlite
    .prepare(`INSERT INTO readings (user_id, device_id, measured_at, metric, source, value_num, value_text) VALUES ('me', ?, ?, ?, ?, ?, ?)`)
    .run(deviceId, t, metric, source, typeof value === "number" ? value : null, typeof value === "string" ? value : null);
}

let sampleId = 0;
function insertSample(type: string, start: number, end: number, value: number | string, product = "Watch7,1") {
  sampleId += 1;
  db.sqlite
    .prepare(`INSERT INTO health_samples (user_id, uuid, type, start_at, end_at, value_num, value_text, source_name, source_product, deleted, updated_at) VALUES ('me', ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)`)
    .run(`S-${sampleId}`, type, start, end, typeof value === "number" ? value : null, typeof value === "string" ? value : null, product.startsWith("Watch") ? "Apple Watch" : "iPhone", product);
}

function insertBucket(type: string, minutes: number, start: number, values: { sum?: number; avg?: number; min?: number }) {
  db.sqlite
    .prepare(`INSERT INTO health_buckets (user_id, type, bucket_minutes, bucket_start, sum, avg, min, max, updated_at) VALUES ('me', ?, ?, ?, ?, ?, ?, NULL, 0)`)
    .run(type, minutes, start, values.sum ?? null, values.avg ?? null, values.min ?? null);
}

function insertLog(id: string, kind: string, t: number, data: unknown) {
  db.sqlite
    .prepare(`INSERT INTO user_logs (user_id, id, kind, occurred_at, data_json, deleted, created_at, updated_at) VALUES ('me', ?, ?, ?, ?, 0, 0, 0)`)
    .run(id, kind, t, JSON.stringify(data));
}

/** from から to の手前まで、5分ごとに fn を呼ぶ */
function every5(from: number, to: number, fn: (t: number) => void) {
  for (let t = from; t < to; t += 5 * MINUTE_MS) fn(t);
}

function seedNight() {
  insertDevice("C1", "MeterPro(CO2)", "CO2センサー");
  insertDevice("P1", "Presence Sensor", "人感センサー");
  insertDevice("W1", "Contact Sensor", "窓");
  insertDevice("D1", "Contact Sensor", "ドア");
  insertDevice("L1", "Ceiling Light", "シーリングライト");
  insertDevice("G1", "Plug Mini (JP)", "加湿空気清浄機");
  insertDevice("G2", "Plug Mini (JP)", "テレビ");
  insertDevice("B1", "Hub 3", "ハブ3");

  const onset = at(PREV, "23:40");
  every5(at(PREV, "20:00"), at(D, "08:00"), (t) => {
    insertReading("C1", t, "CO2", t < onset ? 700 : t < at(D, "03:00") ? 900 : 1200);
    insertReading("C1", t, "temperature", 21);
    insertReading("C1", t, "humidity", 45);
    insertReading("B1", t, "temperature", 25); // ハブ3 の温度は、CO2センサーがあるので使われない
    insertReading("P1", t, "Detected", t >= at(PREV, "23:10") ? 1 : 0);
    insertReading("P1", t, "lightLevel", t >= onset ? 2 : 12);
    insertReading("W1", t, "openState", t >= at(PREV, "22:00") && t < at(PREV, "22:20") ? "open" : "close");
    insertReading("D1", t, "openState", "close");
    const lightOn = t <= at(PREV, "23:15");
    insertReading("L1", t, "power", lightOn ? "on" : "off");
    insertReading("L1", t, "brightness", 60);
    insertReading("L1", t, "colorTemperature", 3000);
    insertReading("G1", t, "weight", t >= onset && t < at(D, "06:50") ? 15 : 0);
    insertReading("G2", t, "weight", t >= at(PREV, "21:30") && t < at(PREV, "22:30") ? 60 : 0);
  });
  insertReading("W1", at(PREV, "22:00") + 30_000, "openState", "open", "webhook");

  // 睡眠（Apple Watch）。iPhone の記録は使われないことも確かめる
  insertSample("sleepAnalysis", at("2026-11-19", "23:30"), at(PREV, "07:00"), "asleepCore");
  insertSample("sleepAnalysis", at(PREV, "14:00"), at(PREV, "14:30"), "asleepCore");
  insertSample("sleepAnalysis", onset, at(D, "01:00"), "asleepCore");
  insertSample("sleepAnalysis", at(D, "01:00"), at(D, "02:00"), "asleepDeep");
  insertSample("sleepAnalysis", at(D, "02:00"), at(D, "02:05"), "awake");
  insertSample("sleepAnalysis", at(D, "02:05"), at(D, "03:00"), "asleepREM");
  insertSample("sleepAnalysis", at(D, "03:00"), at(D, "06:50"), "asleepCore");
  insertSample("sleepAnalysis", at(PREV, "22:00"), at(D, "07:30"), "asleepUnspecified", "iPhone16,1");

  insertSample("heartRateVariabilitySDNN", at(D, "02:30"), at(D, "02:31"), 45);
  insertSample("heartRateVariabilitySDNN", at(D, "04:30"), at(D, "04:31"), 55);
  insertSample("respiratoryRate", at(D, "01:30"), at(D, "01:31"), 14);
  insertSample("respiratoryRate", at(D, "05:30"), at(D, "05:31"), 15);
  insertSample("appleSleepingWristTemperature", onset, at(D, "06:50"), 35.2);
  insertSample("restingHeartRate", at(D, "00:00"), at(D, "12:00"), 52);
  insertSample("stateOfMind", at(PREV, "18:00"), at(PREV, "18:00"), 0.5);
  insertSample("mindfulSession", at(PREV, "22:30"), at(PREV, "22:40"), 1);

  every5(onset, at(D, "06:50"), (t) => insertBucket("heartRate", 5, t, { avg: 55, min: 50 }));
  every5(at(PREV, "21:10"), at(PREV, "23:10"), (t) => insertBucket("heartRate", 5, t, { avg: 70, min: 65 }));
  for (let hour = 7; hour <= 22; hour += 1) insertBucket("stepCount", 60, at(PREV, `${String(hour).padStart(2, "0")}:00`), { sum: 1000 });
  for (const hour of ["08", "09", "10", "11", "13"]) insertBucket("timeInDaylight", 60, at(PREV, `${hour}:00`), { sum: 10 });

  db.sqlite
    .prepare(`INSERT INTO workouts (user_id, uuid, activity_type, start_at, end_at, effort, deleted, updated_at) VALUES ('me', 'W-1', 'running', ?, ?, 7, 0, 0)`)
    .run(at(PREV, "18:00"), at(PREV, "18:45"));

  insertLog("M-1", "meal", at(PREV, "20:30"), { text: "豚骨ラーメン、餃子", amount: "large", meal: "dinner" });
  db.sqlite
    .prepare(`INSERT INTO meal_estimates (user_id, log_id, source, model, estimate_json, estimated_at) VALUES ('me', 'M-1', 'ai', 'test', ?, 0)`)
    .run(JSON.stringify({ kcal: 850, greasiness: 3, spiciness: 1, volume: 3, caffeineMg: 0, alcoholDrinks: 0 }));
  insertLog("B-1", "bath", at(PREV, "21:40"), {});
  insertLog("C-1", "caffeine", at(PREV, "15:00"), { item: "コーヒー", mg: 90 });
  insertLog("A-1", "alcohol", at(PREV, "21:00"), { item: "ビール", drinks: 1 });
  insertLog("S-1", "screenUsage", at(PREV, "21:00"), { cumulativeMinutes: 5 });
  insertLog("S-2", "screenUsage", at(PREV, "22:00"), { cumulativeMinutes: 20 });
  insertLog("S-3", "screenUsage", at(PREV, "23:30"), { cumulativeMinutes: 40 });
  insertLog("S-4", "screenUsage", at(PREV, "23:50"), { cumulativeMinutes: 45 });
  insertLog("K-1", "charger", at(PREV, "23:45"), { state: "connected" });
  insertLog("F-1", "morningFeeling", at(D, "07:10"), { score: 4 });
}

beforeEach(() => {
  db = createFakeD1();
  sampleId = 0;
});

describe("computeDay", () => {
  it("1晩のデータから、睡眠・体・部屋・寝る前・行動・日中・翌朝の値を出す", async () => {
    seedNight();
    const roles = await loadRoles(db.d1, "me", {});
    const metrics = await computeDay(db.d1, "me", D, settings, roles);

    const { segments, ...sleep } = metrics.sleep!;
    expect(sleep).toEqual({
      onset: at(PREV, "23:40"),
      wake: at(D, "06:50"),
      inBedStart: at(PREV, "23:10"),
      inBedSource: "presence",
      asleepMin: 425,
      deepMin: 60,
      remMin: 55,
      coreMin: 310,
      awakeMin: 5,
      awakenings: 1,
      timeInBedMin: 460,
      efficiency: 0.924,
      latencyMin: 30,
    });
    expect(segments).toHaveLength(5);

    expect(metrics.body).toEqual({
      hrAvg: 55,
      hrMin: 50,
      hrvAvg: 50,
      respiratoryRateAvg: 14.5,
      spo2Avg: null,
      spo2Min: null,
      wristTemperature: 35.2,
      breathingDisturbances: null,
      restingHeartRate: 52,
    });

    expect(metrics.room).toEqual({
      co2Avg: 1060, // (40回 × 900 + 46回 × 1,200) ÷ 86回
      co2Max: 1200,
      co2OverMin: 230, // 03:00〜06:50
      co2AtBed: 700,
      temperatureAvg: 21,
      temperatureMin: 21,
      temperatureMax: 21,
      humidityAvg: 45,
      lightLevelAvg: 2,
      brightMin: 0,
      windowOpenMin: 0,
      doorOpenMin: 0,
      purifierMin: 430,
    });

    expect(metrics.preBed).toEqual({
      start: at(PREV, "21:10"),
      end: at(PREV, "23:10"),
      heartRateAvg: 70,
      temperatureAvg: 21,
      lightOnMin: 120,
      lightBrightnessAvg: 60,
      lightColorTemperatureAvg: 3000,
      lightsOffAt: at(PREV, "23:20"),
      tvOnMin: 60,
      windowOpenMin: 20,
      windowOpenings: 1,
      purifierMin: 0,
    });

    expect(metrics.behavior).toEqual({
      bathToBedMin: 90,
      lastMealToBedMin: 160,
      lastMeal: { kcal: 850, greasiness: 3, spiciness: 1, volume: 3, estimated: true },
      lateSnack: false,
      eveningKcal: 850,
      caffeineMg: 90,
      lastCaffeineToBedMin: 490,
      alcoholDrinks: 1,
      lastPhoneUseAt: at(PREV, "23:50"),
      phoneInBedMin: 40,
      nightPhoneMin: 45,
      chargerConnectedAt: at(PREV, "23:45"),
      mindfulMin: 10,
    });

    expect(metrics.day).toEqual({
      start: at(PREV, "07:00"),
      end: at(PREV, "23:10"),
      steps: 16000,
      exerciseMin: null,
      standMin: null,
      activeKcal: null,
      daylightMin: 50,
      morningDaylightMin: 40,
      workoutMin: 45,
      workoutMaxEffort: 7,
      lastWorkoutToBedMin: 265,
      napMin: 30,
      valenceAvg: 0.5,
    });

    expect(metrics.morning).toEqual({ feeling: 4 });
    expect(metrics.quality).toEqual({ hasSleep: true, heartRateCoverage: 1, roomCoverage: 1, presenceAvailable: true, usable: true });
  });

  it("寝る直前の目安のない夜食があれば、最後の食事として扱い「夜食」にする", async () => {
    seedNight();
    insertLog("M-2", "meal", at(PREV, "22:30"), { text: "ポテトチップス" });
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.behavior.lastMealToBedMin).toBe(40);
    expect(metrics.behavior.lastMeal).toEqual({ kcal: null, greasiness: null, spiciness: null, volume: null, estimated: false });
    expect(metrics.behavior.lateSnack).toBe(true);
    // 目安のない食事があるので、夜の合計カロリーは出さない
    expect(metrics.behavior.eveningKcal).toBeNull();
  });

  it("人感センサーがなければ、HealthKit の「就床」を布団に入った時刻に使う", async () => {
    seedNight();
    db.sqlite.prepare(`DELETE FROM readings WHERE device_id = 'P1'`).run();
    insertSample("sleepAnalysis", at(PREV, "23:20"), at(D, "06:55"), "inBed");
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.sleep?.inBedStart).toBe(at(PREV, "23:20"));
    expect(metrics.sleep?.inBedSource).toBe("healthkit");
    expect(metrics.sleep?.latencyMin).toBe(20);
    expect(metrics.quality.presenceAvailable).toBe(false);
  });

  it("睡眠のデータがない日は、睡眠を null にし、目標の就寝時刻を区切りに使う", async () => {
    insertDevice("C1", "MeterPro(CO2)", "CO2センサー");
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.sleep).toBeNull();
    expect(metrics.preBed.end).toBe(at(PREV, "23:30"));
    expect(metrics.day.start).toBe(at(PREV, "07:00"));
    expect(metrics.room.co2Avg).toBeNull();
    expect(metrics.quality).toEqual({ hasSleep: false, heartRateCoverage: null, roomCoverage: null, presenceAvailable: false, usable: false });
  });

  it("睡眠中に Watch のデータが少ない夜は「使えない夜」にする", async () => {
    seedNight();
    db.sqlite.prepare(`DELETE FROM health_buckets WHERE type = 'heartRate' AND bucket_start >= ?`).run(at(D, "02:00"));
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.quality.heartRateCoverage).toBeLessThan(0.7);
    expect(metrics.quality.usable).toBe(false);
  });

  it("機器の役割は設定で変えられる", async () => {
    seedNight();
    // 窓のセンサーを「ドア」として扱う
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", { W1: "door", D1: "ignore" }));
    expect(metrics.preBed.windowOpenMin).toBeNull();
    expect(metrics.room.doorOpenMin).toBe(0);
  });
});

describe("processDayQueue", () => {
  it("新しい日から、決めた数だけ計算する。まだ来ていない日は計算せずに消す", async () => {
    seedNight();
    const now = at(D, "09:00");
    for (const date of ["2026-11-18", "2026-11-19", "2026-11-20", D, "2026-11-22"]) {
      db.sqlite.prepare(`INSERT INTO day_queue (user_id, date, queued_at) VALUES ('me', ?, ?)`).run(date, now - 1000);
    }

    const first = await processDayQueue(db.d1, "me", settings, now, 2);
    expect(first.dropped).toEqual(["2026-11-22"]);
    expect(first.computed).toEqual([D, "2026-11-20"]);
    expect(first.remaining).toBe(2);

    const second = await processDayQueue(db.d1, "me", settings, now, 2);
    expect(second.computed).toEqual(["2026-11-19", "2026-11-18"]);
    expect(second.remaining).toBe(0);

    const row = db.sqlite.prepare(`SELECT sleep_start, sleep_end, metrics_json FROM days WHERE date = ?`).get(D) as { sleep_start: number; sleep_end: number; metrics_json: string };
    expect(row.sleep_start).toBe(at(PREV, "23:40"));
    expect(row.sleep_end).toBe(at(D, "06:50"));
    expect(JSON.parse(row.metrics_json).sleep.asleepMin).toBe(425);
  });
});
