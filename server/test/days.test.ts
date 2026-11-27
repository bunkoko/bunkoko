// 1晩を丸ごとまねたデータ（helpers/night.ts）で、1日 = 1行の表の計算を確かめる。
// 期待する値は、そのデータから手で計算したもの。

import { beforeEach, describe, expect, it } from "vitest";
import { computeDay } from "../src/days/compute";
import { addDays } from "../src/time";
import { processDayQueue } from "../src/days/queue";
import { loadRoles } from "../src/roles";
import { defaultSettings } from "../src/settings";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";
import { D, PREV, TZ, at, insertLog, insertSample, insertDevice, resetSampleIds, seedNight, useDb } from "./helpers/night";

const settings = defaultSettings(TZ);
let db: FakeD1;

beforeEach(() => {
  db = createFakeD1();
  useDb(db);
  resetSampleIds();
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
      roomLightLevelAvg: 12,
      roomBrightMin: 120,
      roomDarkAt: at(PREV, "23:40"), // 人感センサーの照度は、寝つくまで明るいまま
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
      condition: null,
      stress: null,
    });

    expect(metrics.morning).toEqual({ feeling: 4 });
    expect(metrics.nextDay).toEqual({ condition: null, stress: null });
    expect(metrics.quality).toEqual({ hasSleep: true, heartRateCoverage: 1, roomCoverage: 1, presenceAvailable: true, usable: true });
  });

  it("朝の体感は 18時までに入れたものを使う", async () => {
    seedNight();
    db.sqlite.prepare(`DELETE FROM user_logs WHERE id = 'F-1'`).run();
    insertLog("F-late", "morningFeeling", at(D, "15:30"), { score: 2 });
    insertLog("F-evening", "morningFeeling", at(D, "19:00"), { score: 5 });
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.morning.feeling).toBe(2);
  });

  it("1日のふり返りは、前の日の分を「前の日中」（要因）に、その日の分を「翌日」（結果）に入れる", async () => {
    seedNight();
    insertLog("Q-1", "dayCheckin", at(PREV, "22:00"), { day: PREV, condition: 2, stress: 5 });
    // 同じ日を記録し直したら、最後のものを使う
    insertLog("Q-2", "dayCheckin", at(PREV, "22:30"), { day: PREV, condition: 3, stress: 4 });
    // その日の分を、忘れて次の朝に入れた
    insertLog("Q-3", "dayCheckin", at(addDays(D, 1), "08:00"), { day: D, condition: 5, stress: null });
    // 取り消した記録は使わない
    insertLog("Q-4", "dayCheckin", at(D, "21:00"), { day: D, condition: 1, stress: 5 });
    db.sqlite.prepare(`UPDATE user_logs SET deleted = 1 WHERE id = 'Q-4'`).run();

    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect([metrics.day.condition, metrics.day.stress]).toEqual([3, 4]);
    expect(metrics.nextDay).toEqual({ condition: 5, stress: null });
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
    // 部屋の明るさも出さない（0分などとしない）
    expect(metrics.preBed.roomLightLevelAvg).toBeNull();
    expect(metrics.preBed.roomBrightMin).toBeNull();
    expect(metrics.preBed.roomDarkAt).toBeNull();
  });

  it("シーリングライトがなくても、寝る前の部屋の明るさと、部屋が暗くなった時刻を人感センサーの照度から出す", async () => {
    seedNight();
    db.sqlite.prepare(`DELETE FROM devices WHERE device_id = 'L1'`).run();
    db.sqlite.prepare(`DELETE FROM readings WHERE device_id = 'L1'`).run();
    // 22:55 までは明るさ 12、23:00 から 1（今の照明を消した）
    db.sqlite.prepare(`UPDATE readings SET value_num = 1 WHERE device_id = 'P1' AND metric = 'lightLevel' AND measured_at >= ?`).run(at(PREV, "23:00"));
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.preBed).toMatchObject({
      lightOnMin: null,
      lightBrightnessAvg: null,
      lightColorTemperatureAvg: null,
      lightsOffAt: null,
      // 寝る前の2時間（21:10〜23:10）の24回のうち、明るいのは 22:55 までの22回
      roomLightLevelAvg: 11.1, // (12 × 22 + 1 × 2) ÷ 24 = 11.08
      roomBrightMin: 110,
      roomDarkAt: at(PREV, "23:00"),
    });
  });

  it("布団に入る2時間より前に暗くした夜も、部屋が暗くなった時刻を出す（布団に入る6時間前まで見る）", async () => {
    seedNight();
    // 20:30 から間接照明（明るさ 3）。寝る前の2時間（21:10〜23:10）は、ずっと暗い
    db.sqlite.prepare(`UPDATE readings SET value_num = 3 WHERE device_id = 'P1' AND metric = 'lightLevel' AND measured_at >= ?`).run(at(PREV, "20:30"));
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.preBed.roomDarkAt).toBe(at(PREV, "20:30"));
    expect(metrics.preBed.roomBrightMin).toBe(0);
    expect(metrics.preBed.roomLightLevelAvg).toBe(3);
  });

  it("照明をつけたまま寝た夜は、部屋が暗くなった時刻を出さない", async () => {
    seedNight();
    db.sqlite.prepare(`UPDATE readings SET value_num = 12 WHERE device_id = 'P1' AND metric = 'lightLevel'`).run();
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.preBed.roomDarkAt).toBeNull();
    expect(metrics.room.brightMin).toBe(430); // 寝ついてから起きるまで明るいまま
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

  it("心拍が数分おきにしか記録されていなくても、着けていた夜は「使える夜」にする", async () => {
    seedNight();
    // 5分ごとの区切りのうち、半分にしか心拍がない（Watch が10分おきに測った）
    const rows = db.sqlite.prepare(`SELECT bucket_start FROM health_buckets WHERE type = 'heartRate' ORDER BY bucket_start`).all() as { bucket_start: number }[];
    rows.forEach((row, index) => {
      if (index % 2 === 1) db.sqlite.prepare(`DELETE FROM health_buckets WHERE type = 'heartRate' AND bucket_start = ?`).run(row.bucket_start);
    });
    const metrics = await computeDay(db.d1, "me", D, settings, await loadRoles(db.d1, "me", {}));
    expect(metrics.quality.heartRateCoverage).toBe(1);
    expect(metrics.quality.usable).toBe(true);
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
