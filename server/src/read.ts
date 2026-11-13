// アプリに見せる API（docs/phase1-design.md の 3.2）
//   GET /api/home                  … 今の部屋の状態・昨夜の要点・就寝まであと何分か
//   GET /api/days?from=&to=        … 1日 = 1行の表（いつもとの差つき）
//   GET /api/nights/:date/timeline … その夜の睡眠・心拍・部屋の値・出来事を時刻つきで

import { BASELINE_DAYS, UNUSUAL_Z, deviation, type Deviation } from "./baseline";
import type { DayMetrics } from "./days/compute";
import { evaluateRoom, upcomingBedtime, type RoomNow } from "./notify";
import { loadRoles, type RoleMap } from "./roles";
import type { UserSettings } from "./settings";
import { DAY_MS, HOUR_MS, MINUTE_MS, addDays, localDate } from "./time";

/** これより古い値は「今の部屋の状態」として使わない */
const ROOM_STALE_MS = 30 * MINUTE_MS;

// ---------------------------------------------------------------------------
// 今の部屋

export async function latestRoom(db: D1Database, userId: string, roles: RoleMap, now: number): Promise<RoomNow> {
  const devices = [...new Set([...roles.co2, ...roles.hub, ...roles.bedPresence, ...roles.ceilingLight])];
  const { results } = await db
    .prepare(
      `SELECT r.device_id, r.metric, r.value_num, r.value_text, r.measured_at
       FROM readings AS r
       WHERE r.user_id = ?1 AND r.source = 'poll' AND r.device_id IN (SELECT value FROM json_each(?2))
         AND r.metric IN ('CO2', 'temperature', 'humidity', 'lightLevel', 'Detected', 'power', 'brightness', 'colorTemperature')
         AND r.measured_at = (SELECT MAX(r2.measured_at) FROM readings AS r2
                              WHERE r2.user_id = r.user_id AND r2.device_id = r.device_id AND r2.source = 'poll')
         AND r.measured_at >= ?3`,
    )
    .bind(userId, JSON.stringify(devices), now - ROOM_STALE_MS)
    .all<{ device_id: string; metric: string; value_num: number | null; value_text: string | null; measured_at: number }>();

  const pick = (role: keyof RoleMap, metric: string) =>
    results.find((row) => roles[role].includes(row.device_id) && row.metric === metric) ?? null;
  const num = (role: keyof RoleMap, metric: string) => pick(role, metric)?.value_num ?? null;
  const power = pick("ceilingLight", "power")?.value_text;
  const measured = results.map((row) => row.measured_at);

  return {
    measuredAt: measured.length > 0 ? Math.max(...measured) : null,
    co2: num("co2", "CO2"),
    temperature: num("co2", "temperature") ?? num("hub", "temperature"),
    humidity: num("co2", "humidity") ?? num("hub", "humidity"),
    lightLevel: num("bedPresence", "lightLevel"),
    inBed: pick("bedPresence", "Detected") ? num("bedPresence", "Detected") === 1 : null,
    ceilingLight: power
      ? { on: power.toLowerCase() === "on", brightness: num("ceilingLight", "brightness"), colorTemperature: num("ceilingLight", "colorTemperature") }
      : null,
  };
}

// ---------------------------------------------------------------------------
// 1日 = 1行の表

/** いつもとの差を見る項目と、ふだんの幅の下限（値がほとんど変わらない人でも、わずかな差を「珍しい」にしないため） */
const BASELINE_METRICS = {
  restingHeartRate: { get: (m: DayMetrics) => m.body.restingHeartRate, minScale: 1 },
  hrv: { get: (m: DayMetrics) => m.body.hrvAvg, minScale: 3 },
  sleepingHeartRate: { get: (m: DayMetrics) => m.body.hrAvg, minScale: 1 },
  respiratoryRate: { get: (m: DayMetrics) => m.body.respiratoryRateAvg, minScale: 0.3 },
  wristTemperature: { get: (m: DayMetrics) => m.body.wristTemperature, minScale: 0.1 },
} as const;
type BaselineKey = keyof typeof BASELINE_METRICS;

/** 体調がいつもと違うかもしれない日: この3つのうち2つ以上が、いつもと大きく違う */
const UNUSUAL_KEYS: BaselineKey[] = ["restingHeartRate", "wristTemperature", "respiratoryRate"];

export interface DayView {
  date: string;
  metrics: Omit<DayMetrics, "sleep"> & { sleep: Omit<NonNullable<DayMetrics["sleep"]>, "segments"> | null };
  baseline: Record<BaselineKey, Deviation | null>;
  hrv7dAvg: number | null;
  unusual: boolean;
}

export async function loadDays(db: D1Database, userId: string, from: string, to: string): Promise<DayView[]> {
  // いつもとの差を出すため、表示する期間の前の28日も読む
  const { results } = await db
    .prepare(`SELECT date, metrics_json FROM days WHERE user_id = ?1 AND date >= ?2 AND date <= ?3 ORDER BY date`)
    .bind(userId, addDays(from, -BASELINE_DAYS), to)
    .all<{ date: string; metrics_json: string }>();
  const rows = results.map((row) => ({ date: row.date, metrics: JSON.parse(row.metrics_json) as DayMetrics }));

  const views: DayView[] = [];
  rows.forEach((row, index) => {
    if (row.date < from) return;
    const windowStart = addDays(row.date, -BASELINE_DAYS);
    // 比べるのは、その日より前で、データが十分にあり、体調がいつもと違わない日
    const history = rows.slice(0, index).filter((past) => past.date >= windowStart && past.metrics.quality.usable);

    const baseline = {} as Record<BaselineKey, Deviation | null>;
    for (const [key, { get, minScale }] of Object.entries(BASELINE_METRICS) as [BaselineKey, (typeof BASELINE_METRICS)[BaselineKey]][]) {
      const values = history.map((past) => get(past.metrics)).filter((value): value is number => value !== null);
      baseline[key] = deviation(get(row.metrics), values, minScale);
    }
    const unusual = UNUSUAL_KEYS.filter((key) => Math.abs(baseline[key]?.z ?? 0) >= UNUSUAL_Z).length >= 2;

    const recentHrv = rows
      .slice(Math.max(0, index - 6), index + 1)
      .filter((past) => past.date > addDays(row.date, -7))
      .map((past) => past.metrics.body.hrvAvg)
      .filter((value): value is number => value !== null);

    const { segments: _segments, ...sleep } = row.metrics.sleep ?? { segments: null };
    views.push({
      date: row.date,
      metrics: { ...row.metrics, sleep: row.metrics.sleep ? (sleep as DayView["metrics"]["sleep"]) : null },
      baseline,
      hrv7dAvg: recentHrv.length > 0 ? Math.round((recentHrv.reduce((a, b) => a + b, 0) / recentHrv.length) * 10) / 10 : null,
      unusual,
    });
  });
  return views.reverse();
}

// ---------------------------------------------------------------------------
// ホーム

export interface HomeView {
  now: number;
  room: RoomNow;
  roomAdvice: { kind: string; message: string }[];
  lastNight: DayView | null;
  bedtime: { target: number; minutesUntil: number };
  mealsWithoutEstimate: number;
}

export async function buildHome(db: D1Database, userId: string, settings: UserSettings, now: number): Promise<HomeView> {
  const roles = await loadRoles(db, userId, settings.deviceRoles);
  const today = localDate(now, settings.timeZone);
  const [room, days, pending] = await Promise.all([
    latestRoom(db, userId, roles, now),
    loadDays(db, userId, addDays(today, -1), today),
    db
      .prepare(
        `SELECT COUNT(*) AS n FROM user_logs AS l WHERE l.user_id = ?1 AND l.kind = 'meal' AND l.deleted = 0 AND l.occurred_at >= ?2
           AND NOT EXISTS (SELECT 1 FROM meal_estimates AS e WHERE e.user_id = l.user_id AND e.log_id = l.id)`,
      )
      .bind(userId, now - 7 * DAY_MS)
      .first<number>("n"),
  ]);

  // 目標の就寝時刻: 昨夜の目標から6時間以内（夜ふかし中）なら昨夜の目標（あと何分かは負になる）、それ以外は今夜の目標
  const { target } = upcomingBedtime(now, settings);
  const minutesUntil = Math.round((target - now) / MINUTE_MS);

  return {
    now,
    room,
    // 照明の助言は、寝る2時間前から
    roomAdvice: evaluateRoom(room, settings, { includeLight: minutesUntil >= -360 && minutesUntil <= 120 }),
    lastNight: days.find((day) => day.metrics.sleep !== null) ?? null,
    bedtime: { target, minutesUntil },
    mealsWithoutEstimate: pending ?? 0,
  };
}

// ---------------------------------------------------------------------------
// 夜の詳細

type Point = [number, number];

export interface TimelineView {
  date: string;
  window: { start: number; end: number };
  sleep: DayMetrics["sleep"];
  heartRate: Point[];
  series: {
    co2: Point[];
    temperature: Point[];
    humidity: Point[];
    lightLevel: Point[];
    inBed: Point[];
    /** [時刻, 明るさ（消えていれば 0）, 色温度] */
    ceilingLight: [number, number, number | null][];
  };
  events: { t: number; kind: string; label?: string }[];
}

export async function buildTimeline(
  db: D1Database,
  userId: string,
  date: string,
  settings: UserSettings,
): Promise<TimelineView | null> {
  const row = await db
    .prepare(`SELECT metrics_json FROM days WHERE user_id = ?1 AND date = ?2`)
    .bind(userId, date)
    .first<string>("metrics_json");
  if (!row) return null;
  const metrics = JSON.parse(row) as DayMetrics;
  const start = metrics.preBed.start;
  const end = metrics.sleep ? metrics.sleep.wake + HOUR_MS : start + 12 * HOUR_MS;
  const roles = await loadRoles(db, userId, settings.deviceRoles);
  const devices = [...new Set([...roles.co2, ...roles.hub, ...roles.bedPresence, ...roles.ceilingLight, ...roles.window, ...roles.door])];

  const [readings, buckets, logs] = await db.batch([
    db
      .prepare(
        `SELECT device_id, metric, measured_at, value_num, value_text, source FROM readings
         WHERE user_id = ?1 AND device_id IN (SELECT value FROM json_each(?2)) AND measured_at >= ?3 AND measured_at < ?4
           AND metric IN ('CO2', 'temperature', 'humidity', 'lightLevel', 'Detected', 'detectionState', 'power', 'brightness', 'colorTemperature', 'openState')
         ORDER BY measured_at`,
      )
      .bind(userId, JSON.stringify(devices), start, end),
    db
      .prepare(
        `SELECT bucket_start, avg FROM health_buckets
         WHERE user_id = ?1 AND type = 'heartRate' AND bucket_minutes = 5 AND bucket_start >= ?2 AND bucket_start < ?3
         ORDER BY bucket_start`,
      )
      .bind(userId, start, end),
    db
      .prepare(
        `SELECT kind, occurred_at, data_json FROM user_logs
         WHERE user_id = ?1 AND deleted = 0 AND occurred_at >= ?2 AND occurred_at < ?3
           AND kind IN ('meal', 'bath', 'caffeine', 'alcohol', 'charger')
         ORDER BY occurred_at`,
      )
      .bind(userId, start - 6 * HOUR_MS, end),
  ]);

  type Reading = { device_id: string; metric: string; measured_at: number; value_num: number | null; value_text: string | null; source: string };
  const all = readings.results as Reading[];
  const poll = all.filter((r) => r.source === "poll");
  const series = (role: keyof RoleMap, metric: string): Point[] =>
    poll.filter((r) => roles[role].includes(r.device_id) && r.metric === metric && r.value_num !== null).map((r) => [r.measured_at, r.value_num as number]);

  const lightAt = new Map<number, { on?: boolean; brightness?: number; colorTemperature?: number }>();
  for (const r of poll.filter((reading) => roles.ceilingLight.includes(reading.device_id))) {
    const entry = lightAt.get(r.measured_at) ?? {};
    if (r.metric === "power") entry.on = (r.value_text ?? "").toLowerCase() === "on";
    if (r.metric === "brightness" && r.value_num !== null) entry.brightness = r.value_num;
    if (r.metric === "colorTemperature" && r.value_num !== null) entry.colorTemperature = r.value_num;
    lightAt.set(r.measured_at, entry);
  }

  const events: TimelineView["events"] = [];
  for (const log of logs.results as { kind: string; occurred_at: number; data_json: string }[]) {
    const data = JSON.parse(log.data_json) as Record<string, unknown>;
    // 食事・飲み物・入浴は、表示する範囲の6時間前から（寝る前の様子として）。充電は範囲の中だけ
    if (log.occurred_at < start && log.kind === "charger") continue;
    const label = log.kind === "meal" ? String(data.text ?? "") : log.kind === "charger" ? String(data.state ?? "") : typeof data.item === "string" ? data.item : undefined;
    events.push({ t: log.occurred_at, kind: log.kind, ...(label ? { label } : {}) });
  }
  if (metrics.behavior.lastPhoneUseAt !== null) events.push({ t: metrics.behavior.lastPhoneUseAt, kind: "phoneLastUse" });
  if (metrics.preBed.lightsOffAt !== null) events.push({ t: metrics.preBed.lightsOffAt, kind: "lightsOff" });
  // 窓・ドアの開け閉め（状態が変わった時刻だけ）
  for (const role of ["window", "door"] as const) {
    let previous: string | null = null;
    for (const r of all.filter((reading) => roles[role].includes(reading.device_id) && reading.metric === "openState")) {
      const open = r.value_text === "open" || r.value_text === "timeOutNotClose";
      const state = open ? "Open" : "Close";
      if (state !== previous && (previous !== null || open)) events.push({ t: r.measured_at, kind: `${role}${state}` });
      previous = state;
    }
  }
  events.sort((a, b) => a.t - b.t);

  return {
    date,
    window: { start, end },
    sleep: metrics.sleep,
    heartRate: (buckets.results as { bucket_start: number; avg: number | null }[])
      .filter((bucket) => bucket.avg !== null)
      .map((bucket) => [bucket.bucket_start, bucket.avg as number]),
    series: {
      co2: series("co2", "CO2"),
      temperature: series("co2", "temperature").length > 0 ? series("co2", "temperature") : series("hub", "temperature"),
      humidity: series("co2", "humidity").length > 0 ? series("co2", "humidity") : series("hub", "humidity"),
      lightLevel: series("bedPresence", "lightLevel"),
      inBed: series("bedPresence", "Detected"),
      ceilingLight: [...lightAt.entries()]
        .filter(([, entry]) => entry.on !== undefined)
        .sort((a, b) => a[0] - b[0])
        .map(([t, entry]) => [t, entry.on ? (entry.brightness ?? 0) : 0, entry.colorTemperature ?? null]),
    },
    events,
  };
}
