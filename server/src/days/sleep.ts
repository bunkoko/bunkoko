// 睡眠の区切り方と、布団に入った時刻の判定（データベースを使わない計算だけ）
// 決まりは docs/analysis.md の 3.1、docs/phase1-design.md の 4.3 を参照。

import { MINUTE_MS, HOUR_MS, addDays, localTimeOn } from "../time";

export const ASLEEP_STAGES = new Set(["asleepCore", "asleepDeep", "asleepREM", "asleepUnspecified"]);
/** 眠っている区間の切れ目がこれ以下なら、同じ睡眠としてつなげる */
export const SESSION_GAP_MS = 60 * MINUTE_MS;
/** 中途覚醒として数える切れ目の長さ [TBD: 実データで調整] */
export const AWAKENING_MIN_MS = 2 * MINUTE_MS;
/** 昼寝として数える長さ */
export const NAP_MIN_MS = 15 * MINUTE_MS;
/** 布団に入った時刻を探す範囲（寝ついた時刻の何時間前まで） */
export const IN_BED_LOOKBACK_MS = 3 * HOUR_MS;
/** 人感センサーの「いない」がこれより短ければ、布団から出ていないとみなす */
export const PRESENCE_GAP_TOLERANCE_MS = 10 * MINUTE_MS;

export interface SleepSample {
  start: number;
  end: number;
  stage: string;
  sourceName: string | null;
  sourceProduct: string | null;
}

export interface SleepSegment {
  start: number;
  end: number;
  stage: string;
}

export interface SleepSession {
  onset: number;
  wake: number;
  asleepMs: number;
  stageMs: Record<string, number>;
  awakeMs: number;
  awakenings: number;
  segments: SleepSegment[];
}

const isWatch = (sample: SleepSample) =>
  (sample.sourceProduct ?? "").startsWith("Watch") || /watch/i.test(sample.sourceName ?? "");

/** Apple Watch の睡眠データだけを使う。どの記録にも機器の情報がなければ、すべてを使う */
export function pickWatchSamples(samples: SleepSample[]): SleepSample[] {
  const watch = samples.filter(isWatch);
  if (watch.length > 0) return watch;
  return samples.every((sample) => !sample.sourceProduct && !sample.sourceName) ? samples : [];
}

/** 重なった区間をまとめる */
function union(intervals: [number, number][]): [number, number][] {
  const sorted = [...intervals].sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [start, end] of sorted) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

/** 眠っている区間を、切れ目が60分以内ならつなげて「1つの睡眠」にする */
export function buildSessions(samples: SleepSample[], gapMs = SESSION_GAP_MS): SleepSession[] {
  const asleep = samples
    .filter((sample) => ASLEEP_STAGES.has(sample.stage) && sample.end > sample.start)
    .sort((a, b) => a.start - b.start);

  const groups: SleepSample[][] = [];
  let groupEnd = -Infinity;
  for (const sample of asleep) {
    if (groups.length > 0 && sample.start - groupEnd <= gapMs) {
      groups[groups.length - 1].push(sample);
      groupEnd = Math.max(groupEnd, sample.end);
    } else {
      groups.push([sample]);
      groupEnd = sample.end;
    }
  }

  return groups.map((group) => {
    const intervals = union(group.map((sample) => [sample.start, sample.end] as [number, number]));
    const onset = intervals[0][0];
    const wake = intervals[intervals.length - 1][1];
    const asleepMs = intervals.reduce((total, [start, end]) => total + (end - start), 0);
    let awakenings = 0;
    for (let i = 1; i < intervals.length; i += 1) {
      if (intervals[i][0] - intervals[i - 1][1] >= AWAKENING_MIN_MS) awakenings += 1;
    }
    const stageMs: Record<string, number> = {};
    for (const sample of group) stageMs[sample.stage] = (stageMs[sample.stage] ?? 0) + (sample.end - sample.start);

    const awakeSegments = samples
      .filter((sample) => sample.stage === "awake" && sample.end > onset && sample.start < wake)
      .map((sample) => ({ start: Math.max(sample.start, onset), end: Math.min(sample.end, wake), stage: "awake" }));
    const segments = [...group.map(({ start, end, stage }) => ({ start, end, stage })), ...awakeSegments].sort(
      (a, b) => a.start - b.start,
    );
    return { onset, wake, asleepMs, stageMs, awakeMs: wake - onset - asleepMs, awakenings, segments };
  });
}

/** その日（起きた日）の夜: 前の日の18時〜その日の8時に始まった睡眠のうち、いちばん長いもの */
export function chooseNight(sessions: SleepSession[], date: string, timeZone: string): SleepSession | null {
  const from = localTimeOn(addDays(date, -1), "18:00", timeZone);
  const to = localTimeOn(date, "08:00", timeZone);
  let best: SleepSession | null = null;
  for (const session of sessions) {
    if (session.onset < from || session.onset >= to) continue;
    if (!best || session.asleepMs > best.asleepMs) best = session;
  }
  return best;
}

/** その日の行に入る昼寝: 前の日の8時〜18時に始まった、15分以上の睡眠 */
export function napMs(sessions: SleepSession[], date: string, timeZone: string): number {
  const from = localTimeOn(addDays(date, -1), "08:00", timeZone);
  const to = localTimeOn(addDays(date, -1), "18:00", timeZone);
  return sessions
    .filter((session) => session.onset >= from && session.onset < to && session.asleepMs >= NAP_MIN_MS)
    .reduce((total, session) => total + session.asleepMs, 0);
}

export interface PresenceObservation {
  t: number;
  present: boolean;
}

/**
 * 布団に入った時刻: 人感センサー Pro が、寝つく前からずっと人を検知し続けていた最初の時刻。
 * - 10分以内の「いない」は、寝返りなどでセンサーが見失っただけとみなす
 * - 寝つく前の15分以内に「いる」がなければ、判定できない（null）
 * - 3時間さかのぼっても「いない」がなければ、3時間前の最初の「いる」を使う
 */
export function presenceInBedStart(
  observations: PresenceObservation[],
  onset: number,
  lookbackMs = IN_BED_LOOKBACK_MS,
  toleranceMs = PRESENCE_GAP_TOLERANCE_MS,
): number | null {
  const windowStart = onset - lookbackMs;
  const inWindow = observations
    .filter((observation) => observation.t >= windowStart && observation.t <= onset)
    .sort((a, b) => a.t - b.t);
  const recentPresent = inWindow.some((observation) => observation.present && observation.t >= onset - 15 * MINUTE_MS);
  if (!recentPresent) return null;

  // 寝つく時刻からさかのぼり、長い「いない」が見つかったところで止める
  let inBedStart: number | null = null;
  let nextPresentAt = onset;
  for (let i = inWindow.length - 1; i >= 0; i -= 1) {
    const observation = inWindow[i];
    if (observation.present) {
      inBedStart = observation.t;
      nextPresentAt = observation.t;
      continue;
    }
    // 「いない」がどれだけ続いたか: この観測から、次に「いる」になるまで
    let absentFrom = observation.t;
    while (i - 1 >= 0 && !inWindow[i - 1].present) {
      i -= 1;
      absentFrom = inWindow[i].t;
    }
    if (nextPresentAt - absentFrom > toleranceMs) break;
  }
  return inBedStart;
}
