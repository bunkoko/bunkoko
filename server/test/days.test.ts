// 1晩を丸ごとまねたデータ（helpers/night.ts）で、1日 = 1行の表の計算を確かめる。
// 期待する値は、そのデータから手で計算したもの。

import { beforeEach, describe, expect, it } from "vitest";
import { computeDay } from "../src/days/compute";
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
