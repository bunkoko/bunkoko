// テスト用: 1晩を丸ごとまねたデータ（days.test.ts・read.test.ts で使う）
//
// 2026-11-21（起きた日）の行。日本時間で:
//   前の夜: 11/19 23:30 〜 11/20 07:00（日中は 11/20 07:00 から）
//   昼寝: 11/20 14:00〜14:30 / ワークアウト: 18:00〜18:45 / 夕食: 20:30 / 入浴: 21:40
//   布団に入る: 23:10（人感センサー）/ 寝つく: 23:40 / 起きる: 06:50
//   CO2: 寝る前 700、寝ついてから 03:00 まで 900、03:00 から 1,200

import { MINUTE_MS, localTimeOn } from "../../src/time";
import type { FakeD1 } from "./fake-d1";

export const TZ = "Asia/Tokyo";
export const D = "2026-11-21";
export const PREV = "2026-11-20";
export const at = (date: string, hhmm: string) => localTimeOn(date, hhmm, TZ);

let db: FakeD1;
/** 以降の insert* が書き込むデータベースを決める */
export function useDb(target: FakeD1) {
  db = target;
}

export function insertDevice(id: string, type: string, name: string) {
  db.sqlite
    .prepare(`INSERT INTO devices (user_id, device_id, device_name, device_type, enable_cloud_service, active, first_seen_at, last_seen_at, raw_json) VALUES ('me', ?, ?, ?, 1, 1, 0, 0, '{}')`)
    .run(id, name, type);
}

export function insertReading(deviceId: string, t: number, metric: string, value: number | string, source = "poll") {
  db.sqlite
    .prepare(`INSERT INTO readings (user_id, device_id, measured_at, metric, source, value_num, value_text) VALUES ('me', ?, ?, ?, ?, ?, ?)`)
    .run(deviceId, t, metric, source, typeof value === "number" ? value : null, typeof value === "string" ? value : null);
}

let sampleId = 0;
export function resetSampleIds() {
  sampleId = 0;
}
export function insertSample(type: string, start: number, end: number, value: number | string, product = "Watch7,1") {
  sampleId += 1;
  db.sqlite
    .prepare(`INSERT INTO health_samples (user_id, uuid, type, start_at, end_at, value_num, value_text, source_name, source_product, deleted, updated_at) VALUES ('me', ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)`)
    .run(`S-${sampleId}`, type, start, end, typeof value === "number" ? value : null, typeof value === "string" ? value : null, product.startsWith("Watch") ? "Apple Watch" : "iPhone", product);
}

export function insertBucket(type: string, minutes: number, start: number, values: { sum?: number; avg?: number; min?: number }) {
  db.sqlite
    .prepare(`INSERT INTO health_buckets (user_id, type, bucket_minutes, bucket_start, sum, avg, min, max, updated_at) VALUES ('me', ?, ?, ?, ?, ?, ?, NULL, 0)`)
    .run(type, minutes, start, values.sum ?? null, values.avg ?? null, values.min ?? null);
}

export function insertLog(id: string, kind: string, t: number, data: unknown) {
  db.sqlite
    .prepare(`INSERT INTO user_logs (user_id, id, kind, occurred_at, data_json, deleted, created_at, updated_at) VALUES ('me', ?, ?, ?, ?, 0, 0, 0)`)
    .run(id, kind, t, JSON.stringify(data));
}

/** from から to の手前まで、5分ごとに fn を呼ぶ */
export function every5(from: number, to: number, fn: (t: number) => void) {
  for (let t = from; t < to; t += 5 * MINUTE_MS) fn(t);
}

export function seedNight() {
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

