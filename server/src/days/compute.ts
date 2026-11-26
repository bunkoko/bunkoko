// 1日 = 1行の表の計算（docs/phase1-design.md の 4.3）。
// 行の日付は「起きた日」。1行には、前の日に起きてから布団に入るまで（日中）・寝る前・睡眠・翌朝が入る。
// 無料プランの処理時間に収めるため、部屋の値の集計はデータベース（SQL）で行う。

import type { RoleMap } from "../roles";
import type { UserSettings } from "../settings";
import { HOUR_MS, MINUTE_MS, addDays, localMidnight, localTimeOn } from "../time";
import {
  IN_BED_LOOKBACK_MS,
  buildSessions,
  chooseNight,
  napMs,
  pickWatchSamples,
  presenceInBedStart,
  type PresenceObservation,
  type SleepSample,
  type SleepSegment,
} from "./sleep";

export const ALGO_VERSION = 4;
/** センサーの値を取る間隔（分）。「目安を超えた回数 × 5分」で時間にする */
export const POLL_INTERVAL_MIN = 5;
/** 人感センサー Pro の明るさ（1〜20）がこれ以上なら「明るい」。実測（2026-11-25〜26）: 天井の照明 11、間接照明 2〜4、消灯 1（docs/phase1-design.md の 4.3） */
export const BRIGHT_LIGHT_LEVEL = 5;
/** プラグミニの消費電力（W）がこれを超えたら「動いている」 [TBD: 実データで調整] */
export const PLUG_ON_WATTS = 2;
/** 「使える夜」とみなす、睡眠中のデータの割合 */
export const MIN_COVERAGE = 0.7;
/**
 * Watch を着けていたかを見る区切り（分）。Watch は睡眠中も数分おきにしか心拍を測らないので、
 * 5分ごとの区切りで見ると、着けていても「データが少ない」になってしまう。15分の区切りに1つでも心拍があれば「着けていた」とみなす
 */
export const COVERAGE_WINDOW_MIN = 15;
/** HealthKit の記録1つの長さの上限の目安（手首皮膚温など、ひと晩にまたがる記録があるため）。問い合わせの範囲を絞るのに使う */
const SAMPLE_MAX_SPAN_MS = 24 * HOUR_MS;
/** 1日の計算で使う記録の種類（ふり返りは別に問い合わせる） */
const LOG_KINDS_FOR_DAY = ["meal", "bath", "caffeine", "alcohol", "screenUsage", "charger", "morningFeeling"];

const SAMPLE_TYPES = [
  "sleepAnalysis",
  "heartRateVariabilitySDNN",
  "respiratoryRate",
  "oxygenSaturation",
  "appleSleepingWristTemperature",
  "appleSleepingBreathingDisturbances",
  "restingHeartRate",
  "stateOfMind",
  "mindfulSession",
  "dietaryCaffeine",
  "numberOfAlcoholicBeverages",
];
const HOURLY_TYPES = [
  "stepCount",
  "appleExerciseTime",
  "appleStandTime",
  "activeEnergyBurned",
  "timeInDaylight",
];

export interface DayMetrics {
  algoVersion: number;
  date: string;
  sleep: {
    onset: number;
    wake: number;
    inBedStart: number | null;
    inBedSource: "presence" | "healthkit" | null;
    asleepMin: number;
    deepMin: number;
    remMin: number;
    coreMin: number;
    awakeMin: number;
    awakenings: number;
    timeInBedMin: number;
    efficiency: number;
    latencyMin: number | null;
    segments: SleepSegment[];
  } | null;
  body: {
    hrAvg: number | null;
    hrMin: number | null;
    hrvAvg: number | null;
    respiratoryRateAvg: number | null;
    spo2Avg: number | null;
    spo2Min: number | null;
    wristTemperature: number | null;
    breathingDisturbances: number | null;
    restingHeartRate: number | null;
  };
  room: {
    co2Avg: number | null;
    co2Max: number | null;
    co2OverMin: number | null;
    co2AtBed: number | null;
    temperatureAvg: number | null;
    temperatureMin: number | null;
    temperatureMax: number | null;
    humidityAvg: number | null;
    lightLevelAvg: number | null;
    brightMin: number | null;
    windowOpenMin: number | null;
    doorOpenMin: number | null;
    purifierMin: number | null;
  };
  preBed: {
    start: number;
    end: number;
    heartRateAvg: number | null;
    temperatureAvg: number | null;
    lightOnMin: number | null;
    lightBrightnessAvg: number | null;
    lightColorTemperatureAvg: number | null;
    lightsOffAt: number | null;
    /** 部屋の明るさ（人感センサー Pro の照度 1〜20）の平均。シーリングライトがなくても取れる */
    roomLightLevelAvg: number | null;
    /** 照度が BRIGHT_LIGHT_LEVEL 以上だった時間 */
    roomBrightMin: number | null;
    /** 部屋が暗くなった時刻: 寝る前〜寝ついて1時間後で、最後に明るかったあと、初めて暗かった時刻。暗くならなかった夜は null */
    roomDarkAt: number | null;
    tvOnMin: number | null;
    windowOpenMin: number | null;
    windowOpenings: number | null;
    purifierMin: number | null;
  };
  behavior: {
    bathToBedMin: number | null;
    lastMealToBedMin: number | null;
    lastMeal: { kcal: number | null; greasiness: number | null; spiciness: number | null; volume: number | null; estimated: boolean } | null;
    lateSnack: boolean;
    eveningKcal: number | null;
    caffeineMg: number;
    lastCaffeineToBedMin: number | null;
    alcoholDrinks: number;
    lastPhoneUseAt: number | null;
    phoneInBedMin: number | null;
    nightPhoneMin: number | null;
    chargerConnectedAt: number | null;
    mindfulMin: number;
  };
  day: {
    start: number;
    end: number;
    steps: number | null;
    exerciseMin: number | null;
    standMin: number | null;
    activeKcal: number | null;
    daylightMin: number | null;
    morningDaylightMin: number | null;
    workoutMin: number;
    workoutMaxEffort: number | null;
    lastWorkoutToBedMin: number | null;
    napMin: number;
    valenceAvg: number | null;
    /** この夜の前の日中のふり返り（要因）。1〜5 */
    condition: number | null;
    stress: number | null;
  };
  morning: { feeling: number | null };
  /** この夜のあとの日中のふり返り（結果）。1〜5。その日の夜に記録されたら入る */
  nextDay: DayCheckin;
  quality: {
    hasSleep: boolean;
    heartRateCoverage: number | null;
    roomCoverage: number | null;
    presenceAvailable: boolean;
    usable: boolean;
  };
}

/** 1日のふり返り。condition: 調子（1 悪い〜5 よい）、stress: ストレス（1 なかった〜5 とても強い） */
export interface DayCheckin {
  condition: number | null;
  stress: number | null;
}

const EMPTY_CHECKIN: DayCheckin = { condition: null, stress: null };

const round = (value: number | null | undefined, digits = 1): number | null => {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
};
const minutes = (ms: number) => Math.round(ms / MINUTE_MS);
const average = (values: number[]) => (values.length > 0 ? values.reduce((a, b) => a + b, 0) / values.length : null);

/** 目標の就寝時刻（"HH:MM"）を、その日（起きた日）の夜の時刻にする。12時より前なら日付が変わった後とみなす */
export function targetBedtimeFor(date: string, settings: UserSettings): number {
  const hour = Number(settings.targetBedtime.slice(0, 2));
  return localTimeOn(hour < 12 ? date : addDays(date, -1), settings.targetBedtime, settings.timeZone);
}

interface RoomAggregate {
  sAvg: number | null;
  sMin: number | null;
  sMax: number | null;
  sN: number;
  sHit: number;
  pAvg: number | null;
  pN: number;
  pHit: number;
  bAvg: number | null;
}

/** 同じ役割の機器が複数あるときは、値をまとめる */
function mergeAggregates(rows: RoomAggregate[]): RoomAggregate | null {
  if (rows.length === 0) return null;
  const weighted = (key: "sAvg" | "pAvg" | "bAvg", weightKey: "sN" | "pN") => {
    const parts = rows.filter((row) => row[key] !== null);
    const total = parts.reduce((sum, row) => sum + (row[weightKey] || 1), 0);
    return parts.length > 0 ? parts.reduce((sum, row) => sum + (row[key] as number) * (row[weightKey] || 1), 0) / total : null;
  };
  const values = (key: "sMin" | "sMax") => rows.map((row) => row[key]).filter((value): value is number => value !== null);
  return {
    sAvg: weighted("sAvg", "sN"),
    sMin: values("sMin").length > 0 ? Math.min(...values("sMin")) : null,
    sMax: values("sMax").length > 0 ? Math.max(...values("sMax")) : null,
    sN: rows.reduce((sum, row) => sum + row.sN, 0),
    sHit: rows.reduce((sum, row) => sum + row.sHit, 0),
    pAvg: weighted("pAvg", "pN"),
    pN: rows.reduce((sum, row) => sum + row.pN, 0),
    pHit: rows.reduce((sum, row) => sum + row.pHit, 0),
    bAvg: weighted("bAvg", "pN"),
  };
}

interface LogRow {
  id: string;
  kind: string;
  occurred_at: number;
  data_json: string;
  user_est: string | null;
  ai_est: string | null;
}

export async function computeDay(
  db: D1Database,
  userId: string,
  date: string,
  settings: UserSettings,
  roles: RoleMap,
): Promise<DayMetrics> {
  const tz = settings.timeZone;
  const fetchFrom = localTimeOn(addDays(date, -2), "18:00", tz);
  const fetchTo = localTimeOn(date, "14:00", tz);
  /** 朝の体感は、起きてから 18時までに入れたものを使う（アプリが朝の体感を聞くのも 18時まで） */
  const feelingTo = localTimeOn(date, "18:00", tz);

  // --- 1. HealthKit の測った値（睡眠・体・心の状態・飲み物）
  // 無料プランの読み取り行数の上限に収めるため、種類と開始時刻の索引で範囲を絞る（INDEXED BY で必ず使わせる）
  const { results: sampleRows } = await db
    .prepare(
      `SELECT type, start_at, end_at, value_num, value_text, source_name, source_product
       FROM health_samples INDEXED BY idx_health_samples_type_time
       WHERE user_id = ?1 AND type IN (SELECT value FROM json_each(?4))
         AND start_at >= ?5 AND start_at < ?3 AND end_at >= ?2 AND deleted = 0`,
    )
    .bind(userId, fetchFrom, fetchTo, JSON.stringify(SAMPLE_TYPES), fetchFrom - SAMPLE_MAX_SPAN_MS)
    .all<{ type: string; start_at: number; end_at: number; value_num: number | null; value_text: string | null; source_name: string | null; source_product: string | null }>();

  const sleepSamples: SleepSample[] = pickWatchSamples(
    sampleRows
      .filter((row) => row.type === "sleepAnalysis" && row.value_text)
      .map((row) => ({ start: row.start_at, end: row.end_at, stage: row.value_text as string, sourceName: row.source_name, sourceProduct: row.source_product })),
  );
  const sessions = buildSessions(sleepSamples);
  const night = chooseNight(sessions, date, tz);
  const previousNight = chooseNight(sessions, addDays(date, -1), tz);

  // --- 2. 時間の区切り
  const dayStart = previousNight?.wake ?? localTimeOn(addDays(date, -1), "07:00", tz);
  const presenceFrom = night ? night.onset - IN_BED_LOOKBACK_MS : 0;
  const presenceTo = night ? night.onset + 5 * MINUTE_MS : 0;

  // --- 3. 部屋の値・人感センサー（まとめて1回で問い合わせる）
  const allDevices = [...new Set(Object.values(roles).flat())];
  const bedPresence = JSON.stringify(roles.bedPresence);

  // 布団に入った時刻は、人感センサーの値が必要なので先に調べる
  const presenceRows = night
    ? (
        await db
          .prepare(
            `SELECT measured_at, metric, value_num, value_text FROM readings
             WHERE user_id = ?1 AND device_id IN (SELECT value FROM json_each(?2))
               AND metric IN ('detected', 'detectionState') AND measured_at >= ?3 AND measured_at <= ?4`,
          )
          .bind(userId, bedPresence, presenceFrom, presenceTo)
          .all<{ measured_at: number; metric: string; value_num: number | null; value_text: string | null }>()
      ).results
    : [];
  // 5分ごとの値は detected（真偽値）、Webhook は detectionState（"DETECTED" / "NOT_DETECTED"）
  const observations: PresenceObservation[] = presenceRows.map((row) => ({
    t: row.measured_at,
    present: row.metric === "detected" ? row.value_num === 1 : row.value_text === "DETECTED",
  }));

  let inBedStart: number | null = null;
  let inBedSource: "presence" | "healthkit" | null = null;
  if (night) {
    inBedStart = presenceInBedStart(observations, night.onset);
    if (inBedStart !== null) inBedSource = "presence";
    else {
      const inBed = sleepSamples
        .filter((sample) => sample.stage === "inBed" && sample.start <= night.onset && sample.end > night.onset - IN_BED_LOOKBACK_MS)
        .map((sample) => Math.max(sample.start, night.onset - IN_BED_LOOKBACK_MS));
      if (inBed.length > 0) {
        inBedStart = Math.min(...inBed);
        inBedSource = "healthkit";
      }
    }
  }

  const bedRef = inBedStart ?? night?.onset ?? targetBedtimeFor(date, settings);
  const preStart = bedRef - 2 * HOUR_MS;
  const sleepStart = night?.onset ?? bedRef;
  const sleepEnd = night?.wake ?? bedRef;
  const ceiling = JSON.stringify(roles.ceilingLight);

  const [aggregateResult, lightResult, lightsOffResult, roomDarkResult, openingsResult, bucketResult, workoutResult, logResult, checkinResult] = await db.batch([
    db
      .prepare(
        `SELECT device_id, metric,
           AVG(CASE WHEN measured_at >= ?2 AND measured_at < ?3 THEN value_num END) AS s_avg,
           MIN(CASE WHEN measured_at >= ?2 AND measured_at < ?3 THEN value_num END) AS s_min,
           MAX(CASE WHEN measured_at >= ?2 AND measured_at < ?3 THEN value_num END) AS s_max,
           COUNT(CASE WHEN measured_at >= ?2 AND measured_at < ?3 THEN 1 END) AS s_n,
           SUM(CASE WHEN measured_at >= ?2 AND measured_at < ?3 AND (
                 (metric = 'CO2' AND value_num > ?7) OR (metric = 'lightLevel' AND value_num >= ?8)
                 OR (metric = 'weight' AND value_num > ?9) OR (metric = 'openState' AND value_text IN ('open', 'timeOutNotClose'))
                 OR (metric = 'power' AND lower(value_text) = 'on')) THEN 1 ELSE 0 END) AS s_hit,
           AVG(CASE WHEN measured_at >= ?4 AND measured_at < ?5 THEN value_num END) AS p_avg,
           COUNT(CASE WHEN measured_at >= ?4 AND measured_at < ?5 THEN 1 END) AS p_n,
           SUM(CASE WHEN measured_at >= ?4 AND measured_at < ?5 AND (
                 (metric = 'CO2' AND value_num > ?7) OR (metric = 'lightLevel' AND value_num >= ?8)
                 OR (metric = 'weight' AND value_num > ?9) OR (metric = 'openState' AND value_text IN ('open', 'timeOutNotClose'))
                 OR (metric = 'power' AND lower(value_text) = 'on')) THEN 1 ELSE 0 END) AS p_hit,
           AVG(CASE WHEN measured_at >= ?10 AND measured_at < ?5 THEN value_num END) AS b_avg
         FROM readings
         WHERE user_id = ?1 AND source = 'poll' AND device_id IN (SELECT value FROM json_each(?6))
           AND metric IN ('CO2', 'temperature', 'humidity', 'lightLevel', 'openState', 'weight', 'power')
           AND measured_at >= ?4 AND measured_at < MAX(?3, ?5)
         GROUP BY device_id, metric`,
      )
      .bind(userId, sleepStart, sleepEnd, preStart, bedRef, JSON.stringify(allDevices), settings.comfort.co2Max, BRIGHT_LIGHT_LEVEL, PLUG_ON_WATTS, bedRef - 15 * MINUTE_MS),
    // 寝る前の照明: ついていたときの明るさと色温度
    db
      .prepare(
        `SELECT AVG(b.value_num) AS brightness, AVG(c.value_num) AS color_temperature
         FROM readings AS p
         JOIN readings AS b ON b.user_id = p.user_id AND b.device_id = p.device_id AND b.measured_at = p.measured_at
                           AND b.metric = 'brightness' AND b.source = 'poll'
         LEFT JOIN readings AS c ON c.user_id = p.user_id AND c.device_id = p.device_id AND c.measured_at = p.measured_at
                                AND c.metric = 'colorTemperature' AND c.source = 'poll'
         WHERE p.user_id = ?1 AND p.source = 'poll' AND p.device_id IN (SELECT value FROM json_each(?2))
           AND p.metric = 'power' AND lower(p.value_text) = 'on' AND p.measured_at >= ?3 AND p.measured_at < ?4`,
      )
      .bind(userId, ceiling, preStart, bedRef),
    // 消灯した時刻: 寝る前〜寝ついて1時間後までで、最後に照明がついていた時刻
    db
      .prepare(
        `SELECT MAX(measured_at) AS last_on FROM readings
         WHERE user_id = ?1 AND device_id IN (SELECT value FROM json_each(?2))
           AND metric = 'power' AND lower(value_text) = 'on' AND measured_at >= ?3 AND measured_at < ?4`,
      )
      .bind(userId, ceiling, preStart, sleepStart + HOUR_MS),
    // 部屋が暗くなった時刻（人感センサーの照度）。同じ範囲で、最後に明るかった時刻より後の、最初の暗い値
    db
      .prepare(
        `SELECT MIN(d.measured_at) AS dark_at FROM readings AS d
         WHERE d.user_id = ?1 AND d.source = 'poll' AND d.device_id IN (SELECT value FROM json_each(?2))
           AND d.metric = 'lightLevel' AND d.value_num < ?3 AND d.measured_at < ?5
           AND d.measured_at > (
             SELECT MAX(b.measured_at) FROM readings AS b
             WHERE b.user_id = ?1 AND b.source = 'poll' AND b.device_id IN (SELECT value FROM json_each(?2))
               AND b.metric = 'lightLevel' AND b.value_num >= ?3 AND b.measured_at >= ?4 AND b.measured_at < ?5)`,
      )
      .bind(userId, JSON.stringify(roles.bedPresence), BRIGHT_LIGHT_LEVEL, preStart, sleepStart + HOUR_MS),
    // 寝る前に窓を開けた回数（Webhook の「開いた」の数）
    db
      .prepare(
        `SELECT COUNT(*) AS n FROM readings
         WHERE user_id = ?1 AND source = 'webhook' AND device_id IN (SELECT value FROM json_each(?2))
           AND metric = 'openState' AND value_text = 'open' AND measured_at >= ?3 AND measured_at < ?4`,
      )
      .bind(userId, JSON.stringify(roles.window), preStart, bedRef),
    // HealthKit の集計（心拍数は5分ごと、歩数などは1時間ごと）。種類ごとに主キーの範囲で読む（OR でまとめると全件を読んでしまう）
    db
      .prepare(
        `SELECT type, bucket_minutes, bucket_start, sum, avg, min FROM health_buckets
         WHERE user_id = ?1 AND type = 'heartRate' AND bucket_minutes = 5 AND bucket_start >= ?2 AND bucket_start < ?3
         UNION ALL
         SELECT type, bucket_minutes, bucket_start, sum, avg, min FROM health_buckets
         WHERE user_id = ?1 AND type IN (SELECT value FROM json_each(?4)) AND bucket_minutes = 60 AND bucket_start >= ?2 AND bucket_start < ?3`,
      )
      .bind(userId, Math.min(dayStart, preStart) - HOUR_MS, Math.max(sleepEnd, bedRef) + MINUTE_MS, JSON.stringify(HOURLY_TYPES)),
    db
      .prepare(
        `SELECT start_at, end_at, effort FROM workouts
         WHERE user_id = ?1 AND start_at >= ?4 AND start_at < ?3 AND end_at > ?2 AND deleted = 0`,
      )
      .bind(userId, dayStart, bedRef, dayStart - SAMPLE_MAX_SPAN_MS),
    // 記録（食事は目安も一緒に。利用者が直した目安を優先する）
    db
      .prepare(
        `SELECT l.id, l.kind, l.occurred_at, l.data_json, u.estimate_json AS user_est, a.estimate_json AS ai_est
         FROM user_logs AS l INDEXED BY idx_user_logs_kind_time
         LEFT JOIN meal_estimates AS u ON u.user_id = l.user_id AND u.log_id = l.id AND u.source = 'user'
         LEFT JOIN meal_estimates AS a ON a.user_id = l.user_id AND a.log_id = l.id AND a.source = 'ai'
         WHERE l.user_id = ?1 AND l.kind IN (SELECT value FROM json_each(?4))
           AND l.occurred_at >= ?2 AND l.occurred_at < ?3 AND l.deleted = 0`,
      )
      .bind(userId, Math.min(dayStart, bedRef - 12 * HOUR_MS), feelingTo, JSON.stringify(LOG_KINDS_FOR_DAY)),
    // 1日のふり返り: 前の日（この夜の前の日中）と、その日（この夜のあとの日中）。忘れて翌日に入れた分も拾う
    db
      .prepare(
        `SELECT occurred_at, data_json FROM user_logs
         WHERE user_id = ?1 AND kind = 'dayCheckin' AND deleted = 0
           AND occurred_at >= ?2 AND occurred_at < ?3 AND data_json ->> '$.day' IN (?4, ?5)
         ORDER BY occurred_at`,
      )
      .bind(userId, localMidnight(addDays(date, -1), tz), localMidnight(addDays(date, 3), tz), addDays(date, -1), date),
  ]);

  // --- 4. 部屋
  const aggregates = new Map<string, RoomAggregate>();
  for (const row of aggregateResult.results as Record<string, number | string | null>[]) {
    aggregates.set(`${row.device_id}|${row.metric}`, {
      sAvg: row.s_avg as number | null,
      sMin: row.s_min as number | null,
      sMax: row.s_max as number | null,
      sN: Number(row.s_n ?? 0),
      sHit: Number(row.s_hit ?? 0),
      pAvg: row.p_avg as number | null,
      pN: Number(row.p_n ?? 0),
      pHit: Number(row.p_hit ?? 0),
      bAvg: row.b_avg as number | null,
    });
  }
  const forRole = (role: keyof RoleMap, metric: string) =>
    mergeAggregates(roles[role].map((id) => aggregates.get(`${id}|${metric}`)).filter((row): row is RoomAggregate => !!row));
  const hitMinutes = (aggregate: RoomAggregate | null, which: "sHit" | "pHit") =>
    aggregate ? aggregate[which] * POLL_INTERVAL_MIN : null;

  const co2 = forRole("co2", "CO2");
  const temperature = forRole("co2", "temperature") ?? forRole("hub", "temperature");
  const humidity = forRole("co2", "humidity") ?? forRole("hub", "humidity");
  const light = forRole("bedPresence", "lightLevel");
  const windowOpen = forRole("window", "openState");
  const doorOpen = forRole("door", "openState");
  const purifier = forRole("purifier", "weight");
  const tv = forRole("tv", "weight");
  const ceilingPower = forRole("ceilingLight", "power");

  const expectedPolls = night ? (night.wake - night.onset) / (POLL_INTERVAL_MIN * MINUTE_MS) : 0;
  const roomCoverage = night && co2 && expectedPolls > 0 ? Math.min(1, co2.sN / expectedPolls) : night ? 0 : null;

  // --- 5. HealthKit の集計
  const buckets = bucketResult.results as { type: string; bucket_minutes: number; bucket_start: number; sum: number | null; avg: number | null; min: number | null }[];
  const heartRate = buckets.filter((bucket) => bucket.type === "heartRate" && bucket.bucket_minutes === 5);
  const sleepHr = night ? heartRate.filter((bucket) => bucket.bucket_start >= night.onset && bucket.bucket_start < night.wake) : [];
  const preHr = heartRate.filter((bucket) => bucket.bucket_start >= preStart && bucket.bucket_start < bedRef);
  const coverageWindowMs = COVERAGE_WINDOW_MIN * MINUTE_MS;
  const expectedWindows = night ? Math.ceil((night.wake - night.onset) / coverageWindowMs) : 0;
  const coveredWindows = night ? new Set(sleepHr.map((bucket) => Math.floor((bucket.bucket_start - night.onset) / coverageWindowMs))).size : 0;
  const heartRateCoverage = night && expectedWindows > 0 ? Math.min(1, coveredWindows / expectedWindows) : null;

  const hourStart = Math.floor(dayStart / HOUR_MS) * HOUR_MS;
  const noon = localTimeOn(addDays(date, -1), "12:00", tz);
  const hourlySum = (type: string, to = bedRef): number | null => {
    const parts = buckets.filter((bucket) => bucket.type === type && bucket.bucket_minutes === 60 && bucket.bucket_start >= hourStart && bucket.bucket_start < to);
    return parts.length > 0 ? parts.reduce((sum, bucket) => sum + (bucket.sum ?? 0), 0) : null;
  };

  // --- 6. 体（睡眠中）
  const samplesOf = (type: string, from: number, to: number, overlap = false) =>
    sampleRows
      .filter((row) => row.type === type && row.value_num !== null && (overlap ? row.start_at < to && row.end_at > from : row.start_at >= from && row.start_at < to))
      .map((row) => row.value_num as number);
  const dayMidnight = localMidnight(date, tz);
  const restingHeartRate = average(samplesOf("restingHeartRate", dayMidnight, dayMidnight + 24 * HOUR_MS));

  // --- 7. 記録
  const logs = (logResult.results as unknown as LogRow[]).map((row) => ({ ...row, data: JSON.parse(row.data_json) as Record<string, unknown> }));
  const logsOf = (kind: string, from: number, to: number) =>
    logs.filter((log) => log.kind === kind && log.occurred_at >= from && log.occurred_at < to).sort((a, b) => a.occurred_at - b.occurred_at);
  const last = <T>(items: T[]) => (items.length > 0 ? items[items.length - 1] : null);
  const toBed = (t: number | null | undefined) => (t === null || t === undefined ? null : Math.max(0, minutes(bedRef - t)));

  const bath = last(logsOf("bath", bedRef - 6 * HOUR_MS, bedRef + 1));
  const meals = logsOf("meal", bedRef - 8 * HOUR_MS, bedRef + 1);
  const lastMealLog = last(meals);
  const estimateOf = (log: LogRow | null) => {
    const raw = log?.user_est ?? log?.ai_est;
    return raw ? (JSON.parse(raw) as Record<string, number | null>) : null;
  };
  const lastEstimate = estimateOf(lastMealLog);
  const eveningMeals = logsOf("meal", bedRef - 6 * HOUR_MS, bedRef + 1);
  const eveningKcalParts = eveningMeals.map((log) => estimateOf(log)?.kcal ?? null);

  const caffeineLogs = logsOf("caffeine", dayStart, bedRef + 1);
  const dayMeals = logsOf("meal", dayStart, bedRef + 1);
  const caffeineEvents: { t: number; mg: number }[] = [
    ...caffeineLogs.map((log) => ({ t: log.occurred_at, mg: Number(log.data.mg ?? 0) })),
    ...dayMeals.map((log) => ({ t: log.occurred_at, mg: Number(estimateOf(log)?.caffeineMg ?? 0) })).filter((event) => event.mg > 0),
    ...sampleRows
      .filter((row) => row.type === "dietaryCaffeine" && row.start_at >= dayStart && row.start_at < bedRef && row.value_num !== null)
      .map((row) => ({ t: row.start_at, mg: row.value_num as number })),
  ];
  const caffeineMg = caffeineEvents.reduce((sum, event) => sum + event.mg, 0);
  const lastCaffeine = caffeineEvents.length > 0 ? Math.max(...caffeineEvents.map((event) => event.t)) : null;

  const alcoholFrom = bedRef - 8 * HOUR_MS;
  const alcoholDrinks =
    logsOf("alcohol", alcoholFrom, bedRef + 1).reduce((sum, log) => sum + Number(log.data.drinks ?? 1), 0) +
    logsOf("meal", alcoholFrom, bedRef + 1).reduce((sum, log) => sum + Number(estimateOf(log)?.alcoholDrinks ?? 0), 0) +
    samplesOf("numberOfAlcoholicBeverages", alcoholFrom, bedRef + 1).reduce((sum, value) => sum + value, 0);

  const phoneFrom = localTimeOn(addDays(date, -1), "20:00", tz);
  const screenLogs = logsOf("screenUsage", phoneFrom, sleepEnd + 1);
  const beforeSleep = screenLogs.filter((log) => log.occurred_at <= sleepStart + 15 * MINUTE_MS);
  const lastPhoneUseAt = last(beforeSleep)?.occurred_at ?? null;
  const nightPhoneMin = screenLogs.length > 0 ? Math.max(...screenLogs.map((log) => Number(log.data.cumulativeMinutes ?? 0))) : null;
  const charger = last(
    logsOf("charger", bedRef - 3 * HOUR_MS, sleepStart + HOUR_MS).filter((log) => log.data.state === "connected"),
  );
  const feeling = last(logsOf("morningFeeling", (night?.wake ?? localTimeOn(date, "05:00", tz)) - HOUR_MS, feelingTo));

  // 同じ日のふり返りが複数あるときは、最後に記録したものを使う
  const checkins = new Map<string, DayCheckin>();
  for (const row of checkinResult.results as { occurred_at: number; data_json: string }[]) {
    const data = JSON.parse(row.data_json) as Record<string, unknown>;
    const score = (value: unknown) => (typeof value === "number" ? value : null);
    checkins.set(String(data.day), { condition: score(data.condition), stress: score(data.stress) });
  }
  const before = checkins.get(addDays(date, -1)) ?? EMPTY_CHECKIN;

  // --- 8. 日中
  const workoutRows = workoutResult.results as { start_at: number; end_at: number; effort: number | null }[];
  const workoutMs = workoutRows.reduce((sum, row) => sum + (Math.min(row.end_at, bedRef) - Math.max(row.start_at, dayStart)), 0);
  const efforts = workoutRows.map((row) => row.effort).filter((value): value is number => value !== null);
  const lastWorkoutEnd = workoutRows.length > 0 ? Math.max(...workoutRows.map((row) => row.end_at)) : null;

  const lightRow = (lightResult.results[0] ?? {}) as { brightness: number | null; color_temperature: number | null };
  const lastOn = ((lightsOffResult.results[0] ?? {}) as { last_on: number | null }).last_on;
  const darkAt = ((roomDarkResult.results[0] ?? {}) as { dark_at: number | null }).dark_at ?? null;
  const openings = Number(((openingsResult.results[0] ?? {}) as { n: number }).n ?? 0);
  const hasRole = (role: keyof RoleMap) => roles[role].length > 0;

  const mindfulMin = minutes(
    sampleRows
      .filter((row) => row.type === "mindfulSession" && row.end_at > preStart && row.start_at < bedRef)
      .reduce((sum, row) => sum + (Math.min(row.end_at, bedRef) - Math.max(row.start_at, preStart)), 0),
  );

  return {
    algoVersion: ALGO_VERSION,
    date,
    sleep: night
      ? {
          onset: night.onset,
          wake: night.wake,
          inBedStart,
          inBedSource,
          asleepMin: minutes(night.asleepMs),
          deepMin: minutes(night.stageMs.asleepDeep ?? 0),
          remMin: minutes(night.stageMs.asleepREM ?? 0),
          coreMin: minutes(night.stageMs.asleepCore ?? 0),
          awakeMin: minutes(night.awakeMs),
          awakenings: night.awakenings,
          timeInBedMin: minutes(night.wake - (inBedStart ?? night.onset)),
          efficiency: round(night.asleepMs / (night.wake - (inBedStart ?? night.onset)), 3) ?? 0,
          latencyMin: inBedStart !== null ? Math.max(0, minutes(night.onset - inBedStart)) : null,
          segments: night.segments,
        }
      : null,
    body: {
      hrAvg: round(average(sleepHr.map((bucket) => bucket.avg).filter((v): v is number => v !== null))),
      hrMin: sleepHr.length > 0 ? round(Math.min(...sleepHr.map((bucket) => bucket.min ?? Infinity))) : null,
      hrvAvg: night ? round(average(samplesOf("heartRateVariabilitySDNN", night.onset, night.wake))) : null,
      respiratoryRateAvg: night ? round(average(samplesOf("respiratoryRate", night.onset, night.wake))) : null,
      spo2Avg: night ? round(average(samplesOf("oxygenSaturation", night.onset, night.wake))) : null,
      spo2Min: night && samplesOf("oxygenSaturation", night.onset, night.wake).length > 0 ? round(Math.min(...samplesOf("oxygenSaturation", night.onset, night.wake))) : null,
      wristTemperature: night ? round(average(samplesOf("appleSleepingWristTemperature", night.onset, night.wake, true)), 2) : null,
      breathingDisturbances: night ? round(average(samplesOf("appleSleepingBreathingDisturbances", night.onset, night.wake, true)), 2) : null,
      restingHeartRate: round(restingHeartRate),
    },
    room: {
      co2Avg: night ? round(co2?.sAvg, 0) : null,
      co2Max: night ? round(co2?.sMax, 0) : null,
      co2OverMin: night ? hitMinutes(co2, "sHit") : null,
      co2AtBed: round(co2?.bAvg, 0),
      temperatureAvg: night ? round(temperature?.sAvg) : null,
      temperatureMin: night ? round(temperature?.sMin) : null,
      temperatureMax: night ? round(temperature?.sMax) : null,
      humidityAvg: night ? round(humidity?.sAvg, 0) : null,
      lightLevelAvg: night ? round(light?.sAvg) : null,
      brightMin: night ? hitMinutes(light, "sHit") : null,
      windowOpenMin: night && hasRole("window") ? (hitMinutes(windowOpen, "sHit") ?? 0) : null,
      doorOpenMin: night && hasRole("door") ? (hitMinutes(doorOpen, "sHit") ?? 0) : null,
      purifierMin: night && hasRole("purifier") ? (hitMinutes(purifier, "sHit") ?? 0) : null,
    },
    preBed: {
      start: preStart,
      end: bedRef,
      heartRateAvg: round(average(preHr.map((bucket) => bucket.avg).filter((v): v is number => v !== null))),
      temperatureAvg: round(temperature?.pAvg),
      lightOnMin: hasRole("ceilingLight") ? (hitMinutes(ceilingPower, "pHit") ?? 0) : null,
      lightBrightnessAvg: round(lightRow.brightness, 0),
      lightColorTemperatureAvg: round(lightRow.color_temperature, 0),
      lightsOffAt: lastOn !== null && lastOn !== undefined ? lastOn + POLL_INTERVAL_MIN * MINUTE_MS : null,
      roomLightLevelAvg: round(light?.pAvg),
      roomBrightMin: light && light.pN > 0 ? hitMinutes(light, "pHit") : null,
      roomDarkAt: darkAt,
      tvOnMin: hasRole("tv") ? (hitMinutes(tv, "pHit") ?? 0) : null,
      windowOpenMin: hasRole("window") ? (hitMinutes(windowOpen, "pHit") ?? 0) : null,
      windowOpenings: hasRole("window") ? openings : null,
      purifierMin: hasRole("purifier") ? (hitMinutes(purifier, "pHit") ?? 0) : null,
    },
    behavior: {
      bathToBedMin: toBed(bath?.occurred_at),
      lastMealToBedMin: toBed(lastMealLog?.occurred_at),
      lastMeal: lastMealLog
        ? {
            kcal: lastEstimate?.kcal ?? null,
            greasiness: lastEstimate?.greasiness ?? null,
            spiciness: lastEstimate?.spiciness ?? null,
            volume: lastEstimate?.volume ?? null,
            estimated: lastEstimate !== null,
          }
        : null,
      lateSnack: meals.some((log) => log.occurred_at >= bedRef - 2 * HOUR_MS),
      eveningKcal: eveningKcalParts.length > 0 && eveningKcalParts.every((kcal) => kcal !== null)
        ? eveningKcalParts.reduce((sum: number, kcal) => sum + (kcal as number), 0)
        : null,
      caffeineMg: Math.round(caffeineMg),
      lastCaffeineToBedMin: toBed(lastCaffeine),
      alcoholDrinks: round(alcoholDrinks) ?? 0,
      lastPhoneUseAt,
      phoneInBedMin: lastPhoneUseAt !== null && inBedStart !== null ? Math.max(0, minutes(lastPhoneUseAt - inBedStart)) : null,
      nightPhoneMin,
      chargerConnectedAt: charger?.occurred_at ?? null,
      mindfulMin,
    },
    day: {
      start: dayStart,
      end: bedRef,
      steps: hourlySum("stepCount"),
      exerciseMin: hourlySum("appleExerciseTime"),
      standMin: hourlySum("appleStandTime"),
      activeKcal: round(hourlySum("activeEnergyBurned"), 0),
      daylightMin: hourlySum("timeInDaylight"),
      morningDaylightMin: hourlySum("timeInDaylight", Math.min(noon, bedRef)),
      workoutMin: minutes(Math.max(0, workoutMs)),
      workoutMaxEffort: efforts.length > 0 ? Math.max(...efforts) : null,
      lastWorkoutToBedMin: toBed(lastWorkoutEnd),
      napMin: minutes(napMs(sessions, date, tz)),
      valenceAvg: round(average(samplesOf("stateOfMind", dayStart, bedRef)), 2),
      condition: before.condition,
      stress: before.stress,
    },
    morning: { feeling: feeling ? Number(feeling.data.score) : null },
    nextDay: checkins.get(date) ?? EMPTY_CHECKIN,
    quality: {
      hasSleep: night !== null,
      heartRateCoverage: round(heartRateCoverage, 2),
      roomCoverage: round(roomCoverage, 2),
      presenceAvailable: observations.length > 0,
      usable: night !== null && (heartRateCoverage ?? 0) >= MIN_COVERAGE,
    },
  };
}
