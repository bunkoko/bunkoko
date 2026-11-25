// 過去の夜を条件で2つに分け、結果の平均を比べる（docs/analysis.md の 5章の②と、7章の決まり）。
// npm run admin -- analyze [from] [to] で使う。
//
// いまは Apple Watch のデータだけで調べられる仮説（H5・H6・H8・H9）。過去1年分があるので、すぐに始められる。
// 朝の体感・日中の調子（目的変数）は、記録が7夜以上たまった比較から出す。

import { median } from "../src/baseline";
import { addDays, localMinuteOfDay } from "../src/time";

/** 比べるグループに必要な夜の数（7章） */
export const MIN_NIGHTS = 7;
/** 前半・後半で向きを確かめるときに、それぞれに必要な夜の数 */
const MIN_HALF_NIGHTS = 4;
/** いつもの寝つく時刻を出すのに使う日数と、必要な夜の数（4章の基準線と同じ） */
const USUAL_DAYS = 28;
const USUAL_MIN_NIGHTS = 14;

/** /api/days の1行のうち、ここで使う項目 */
export interface AnalysisDay {
  date: string;
  unusual?: boolean;
  metrics: {
    sleep: { onset: number; wake: number; asleepMin: number; efficiency: number; awakenings: number } | null;
    body?: { hrvAvg?: number | null; restingHeartRate?: number | null };
    day?: { steps?: number | null; daylightMin?: number | null; morningDaylightMin?: number | null; workoutMin?: number | null };
    morning?: { feeling?: number | null };
    nextDay?: { condition?: number | null };
    quality?: { usable?: boolean };
  };
}

export interface Night {
  date: string;
  /** 寝ついた時刻（18:00 からの分）。23:30 → 330、0:50 → 410 */
  onsetMin: number;
  /** 起きた時刻（0:00 からの分） */
  wakeMin: number;
  asleepMin: number;
  efficiencyPct: number;
  awakenings: number;
  hrvAvg: number | null;
  /** 起きた日の安静時心拍 */
  restingHeartRate: number | null;
  /** この夜の前の日中 */
  daylightMin: number | null;
  morningDaylightMin: number | null;
  workoutMin: number | null;
  feeling: number | null;
  /** この夜のあとの日中 */
  condition: number | null;
  nextSteps: number | null;
  /** それまでの28日の夜（14夜以上）の、寝ついた時刻の中央値 */
  usualOnsetMin: number | null;
}

const minutesFrom18 = (epochMs: number, timeZone: string) => (localMinuteOfDay(epochMs, timeZone) - 18 * 60 + 24 * 60) % (24 * 60);

/** 1日 = 1行の表から、比べる夜を作る。使えない夜と、体調がいつもと違う日は外す */
export function toNights(rows: AnalysisDay[], timeZone: string): { nights: Night[]; excluded: { unusable: number; unusual: number } } {
  const sorted = [...rows].sort((a, b) => a.date.localeCompare(b.date));
  const byDate = new Map(sorted.map((row) => [row.date, row]));
  const usable = sorted.filter((row) => row.metrics.sleep && row.metrics.quality?.usable);
  const onsets = usable.map((row) => ({ date: row.date, onsetMin: minutesFrom18(row.metrics.sleep!.onset, timeZone) }));

  const nights: Night[] = [];
  let unusual = 0;
  usable.forEach((row, index) => {
    if (row.unusual) {
      unusual += 1;
      return;
    }
    const sleep = row.metrics.sleep!;
    const windowStart = addDays(row.date, -USUAL_DAYS);
    const history = onsets.slice(0, index).filter((past) => past.date >= windowStart);
    nights.push({
      date: row.date,
      onsetMin: onsets[index].onsetMin,
      wakeMin: localMinuteOfDay(sleep.wake, timeZone),
      asleepMin: sleep.asleepMin,
      efficiencyPct: sleep.efficiency * 100,
      awakenings: sleep.awakenings,
      hrvAvg: row.metrics.body?.hrvAvg ?? null,
      restingHeartRate: row.metrics.body?.restingHeartRate ?? null,
      daylightMin: row.metrics.day?.daylightMin ?? null,
      morningDaylightMin: row.metrics.day?.morningDaylightMin ?? null,
      workoutMin: row.metrics.day?.workoutMin ?? null,
      feeling: row.metrics.morning?.feeling ?? null,
      condition: row.metrics.nextDay?.condition ?? null,
      // 次の行の「日中」が、この夜のあとの日中
      nextSteps: byDate.get(addDays(row.date, 1))?.metrics.day?.steps ?? null,
      usualOnsetMin: history.length >= USUAL_MIN_NIGHTS ? median(history.map((past) => past.onsetMin)) : null,
    });
  });
  return { nights, excluded: { unusable: sorted.length - usable.length, unusual } };
}

/** 再現できる乱数（同じデータなら、毎回同じ結果になるように） */
function random(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const mean = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / values.length;

/** 平均の差（a − b）の「差の範囲」（95%。ブートストラップ法） */
export function meanDiffInterval(a: number[], b: number[], iterations = 2000, seed = 1): [number, number] {
  const next = random(seed);
  const resampledMean = (values: number[]) => {
    let sum = 0;
    for (let i = 0; i < values.length; i += 1) sum += values[Math.floor(next() * values.length)];
    return sum / values.length;
  };
  const diffs = Array.from({ length: iterations }, () => resampledMean(a) - resampledMean(b)).sort((x, y) => x - y);
  return [diffs[Math.floor(0.025 * (iterations - 1))], diffs[Math.ceil(0.975 * (iterations - 1))]];
}

export type Verdict = "データ不足" | "差ははっきりしない" | "前半・後半で確かめられない" | "前半と後半で向きが違う" | "気づきの候補";

export interface GroupComparison {
  nA: number;
  nB: number;
  diff: number | null;
  interval: [number, number] | null;
  /** データの前半・後半それぞれでの差 */
  halves: [number | null, number | null];
  verdict: Verdict;
}

export interface Point {
  date: string;
  group: "A" | "B";
  value: number;
}

/** 2つのグループの平均を比べ、7章の決まりで結論を出す */
export function compareGroups(points: Point[]): GroupComparison {
  const valuesOf = (subset: Point[], group: "A" | "B") => subset.filter((point) => point.group === group).map((point) => point.value);
  const a = valuesOf(points, "A");
  const b = valuesOf(points, "B");
  if (a.length < MIN_NIGHTS || b.length < MIN_NIGHTS) {
    return { nA: a.length, nB: b.length, diff: null, interval: null, halves: [null, null], verdict: "データ不足" };
  }
  const diff = mean(a) - mean(b);
  const interval = meanDiffInterval(a, b);

  const dates = [...new Set(points.map((point) => point.date))].sort();
  const splitDate = dates[Math.floor(dates.length / 2)];
  const half = (first: boolean): number | null => {
    const subset = points.filter((point) => point.date < splitDate === first);
    const ha = valuesOf(subset, "A");
    const hb = valuesOf(subset, "B");
    return ha.length >= MIN_HALF_NIGHTS && hb.length >= MIN_HALF_NIGHTS ? mean(ha) - mean(hb) : null;
  };
  const halves: [number | null, number | null] = [half(true), half(false)];

  let verdict: Verdict;
  if (interval[0] <= 0 && interval[1] >= 0) verdict = "差ははっきりしない";
  else if (halves[0] === null || halves[1] === null) verdict = "前半・後半で確かめられない";
  else if (Math.sign(halves[0]) !== Math.sign(diff) || Math.sign(halves[1]) !== Math.sign(diff)) verdict = "前半と後半で向きが違う";
  else verdict = "気づきの候補";
  return { nA: a.length, nB: b.length, diff, interval, halves, verdict };
}

type Unit = "分" | "ポイント" | "回" | "ms" | "拍" | "歩" | "点";

interface OutcomeDef {
  label: string;
  unit: Unit;
  value: (night: Night) => number | null;
  /** 本人の感覚の記録（記録がたまるまでは行を出さない） */
  subjective?: boolean;
}

interface ComparisonDef {
  id: string;
  title: string;
  /** 1つ目・2つ目のグループの説明（threshold はグループを分ける値） */
  labels: (threshold: number | null) => [string, string];
  threshold?: (nights: Night[]) => number | null;
  group: (night: Night, threshold: number | null) => "A" | "B" | null;
  outcomes: OutcomeDef[];
}

const OUTCOMES = {
  efficiency: { label: "睡眠効率", unit: "ポイント", value: (n) => n.efficiencyPct },
  awakenings: { label: "中途覚醒", unit: "回", value: (n) => n.awakenings },
  asleep: { label: "睡眠時間", unit: "分", value: (n) => n.asleepMin },
  onset: { label: "寝ついた時刻（+は遅い）", unit: "分", value: (n) => n.onsetMin },
  wake: { label: "起きた時刻（+は遅い）", unit: "分", value: (n) => n.wakeMin },
  hrv: { label: "HRV（睡眠中）", unit: "ms", value: (n) => n.hrvAvg },
  feeling: { label: "朝の体感", unit: "点", value: (n) => n.feeling, subjective: true },
} satisfies Record<string, OutcomeDef>;

/** 真ん中の値で分ける（0 しかないときは分けない） */
const medianOf = (pick: (night: Night) => number | null) => (nights: Night[]) => {
  const values = nights.map(pick).filter((value): value is number => value !== null);
  const middle = values.length > 0 ? Math.round(median(values)) : null;
  return middle !== null && middle > 0 ? middle : null;
};

const COMPARISONS: ComparisonDef[] = [
  {
    id: "H8",
    title: "寝ついた時刻のずれ",
    labels: () => ["いつもより1時間以上遅く寝ついた夜", "いつも通り（前後30分以内）の夜"],
    group: (n) => {
      if (n.usualOnsetMin === null) return null;
      const late = n.onsetMin - n.usualOnsetMin;
      return late >= 60 ? "A" : Math.abs(late) <= 30 ? "B" : null;
    },
    outcomes: [OUTCOMES.efficiency, OUTCOMES.awakenings, OUTCOMES.asleep, OUTCOMES.wake, OUTCOMES.feeling],
  },
  {
    id: "H6",
    title: "前の日中の日光（合計）",
    labels: (t) => [`日光が${t}分以上の日`, `${t}分未満の日`],
    threshold: medianOf((n) => n.daylightMin),
    group: (n, t) => (t === null || n.daylightMin === null ? null : n.daylightMin >= t ? "A" : "B"),
    outcomes: [OUTCOMES.onset, OUTCOMES.efficiency, OUTCOMES.asleep, OUTCOMES.feeling],
  },
  {
    id: "H6",
    title: "前の日の午前中の日光",
    labels: (t) => [`午前中の日光が${t}分以上の日`, `${t}分未満の日`],
    threshold: medianOf((n) => n.morningDaylightMin),
    group: (n, t) => (t === null || n.morningDaylightMin === null ? null : n.morningDaylightMin >= t ? "A" : "B"),
    outcomes: [OUTCOMES.onset, OUTCOMES.efficiency, OUTCOMES.asleep, OUTCOMES.feeling],
  },
  {
    id: "H5",
    title: "前の日のワークアウト",
    labels: () => ["ワークアウトをした日", "しなかった日"],
    group: (n) => (n.workoutMin === null ? null : n.workoutMin > 0 ? "A" : "B"),
    outcomes: [OUTCOMES.hrv, OUTCOMES.efficiency, OUTCOMES.awakenings, OUTCOMES.feeling],
  },
  {
    id: "H9",
    title: "睡眠時間と、次の日中",
    labels: () => ["7時間以上眠れた夜", "7時間未満の夜"],
    group: (n) => (n.asleepMin >= 420 ? "A" : "B"),
    outcomes: [
      { label: "安静時心拍（その日）", unit: "拍", value: (n) => n.restingHeartRate },
      { label: "歩数（その日）", unit: "歩", value: (n) => n.nextSteps },
      { label: "日中の調子", unit: "点", value: (n) => n.condition, subjective: true },
    ],
  },
];

export interface OutcomeResult extends GroupComparison {
  label: string;
  unit: Unit;
  subjective: boolean;
}

export interface ComparisonResult {
  id: string;
  title: string;
  labelA: string;
  labelB: string;
  nA: number;
  nB: number;
  outcomes: OutcomeResult[];
}

export interface Report {
  from: string | null;
  to: string | null;
  nights: number;
  excluded: { unusable: number; unusual: number };
  comparisons: ComparisonResult[];
}

export function analyze(rows: AnalysisDay[], timeZone: string): Report {
  const { nights, excluded } = toNights(rows, timeZone);
  const dates = rows.map((row) => row.date).sort();
  const comparisons = COMPARISONS.map((def): ComparisonResult => {
    const threshold = def.threshold ? def.threshold(nights) : null;
    const grouped = nights.map((night) => ({ night, group: def.group(night, threshold) }));
    const [labelA, labelB] = def.labels(threshold);
    return {
      id: def.id,
      title: def.title,
      labelA,
      labelB,
      nA: grouped.filter((entry) => entry.group === "A").length,
      nB: grouped.filter((entry) => entry.group === "B").length,
      outcomes: def.outcomes.map((outcome) => {
        const points: Point[] = [];
        for (const { night, group } of grouped) {
          const value = outcome.value(night);
          if (group !== null && value !== null) points.push({ date: night.date, group, value });
        }
        return { label: outcome.label, unit: outcome.unit, subjective: outcome.subjective ?? false, ...compareGroups(points) };
      }),
    };
  });
  return { from: dates[0] ?? null, to: dates[dates.length - 1] ?? null, nights: nights.length, excluded, comparisons };
}

const DIGITS: Record<Unit, number> = { 分: 0, ポイント: 1, 回: 1, ms: 1, 拍: 1, 歩: 0, 点: 1 };

function signed(value: number, unit: Unit): string {
  const digits = DIGITS[unit];
  const rounded = Number(value.toFixed(digits));
  const text = Math.abs(rounded).toLocaleString("ja-JP", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return rounded > 0 ? `+${text}` : rounded < 0 ? `−${text}` : text;
}

/** 結果を、ここに貼れる長さの文字にする */
export function formatReport(report: Report): string[] {
  const lines = [
    `対象: ${report.from ?? "-"}〜${report.to ?? "-"} の ${report.nights}夜（使えない夜 ${report.excluded.unusable}、体調がいつもと違う日 ${report.excluded.unusual} を除く）`,
    "見方: 差 = 1つ目の夜の平均 − 2つ目の夜の平均。差の範囲が0をまたぐときは「差ははっきりしない」。前半と後半の両方で同じ向きなら「気づきの候補」",
  ];
  let subjectiveMissing = false;
  for (const comparison of report.comparisons) {
    lines.push("", `${comparison.id} ${comparison.title}: ${comparison.labelA}（${comparison.nA}夜） − ${comparison.labelB}（${comparison.nB}夜）`);
    for (const outcome of comparison.outcomes) {
      if (outcome.subjective && outcome.verdict === "データ不足") {
        subjectiveMissing = true;
        continue;
      }
      const label = outcome.label.padEnd(12, "　");
      if (outcome.diff === null || outcome.interval === null) {
        lines.push(`  ${label}  データ不足（${outcome.nA}夜 / ${outcome.nB}夜）`);
        continue;
      }
      const [low, high] = outcome.interval;
      lines.push(`  ${label}  ${signed(outcome.diff, outcome.unit)}${outcome.unit}（差の範囲 ${signed(low, outcome.unit)}〜${signed(high, outcome.unit)}）  ${outcome.verdict}`);
    }
  }
  if (subjectiveMissing) lines.push("", `朝の体感・日中の調子は、記録がそれぞれ${MIN_NIGHTS}夜以上たまってから比べます`);
  return lines;
}
