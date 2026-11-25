// 過去の夜を条件で2つに分けて比べる計算（scripts/analyze.ts）。期待する値は手で計算したもの。

import { describe, expect, it } from "vitest";
import { analyze, compareGroups, formatReport, meanDiffInterval, toNights, type AnalysisDay, type Point } from "../scripts/analyze";
import { addDays, localTimeOn } from "../src/time";

const TZ = "Asia/Tokyo";
const at = (date: string, hhmm: string) => localTimeOn(date, hhmm, TZ);

/** 1日 = 1行の表の1行（ここで使う項目だけ）。onset は起きた日の時刻（"01:00"）か、前の日の時刻（"-23:30"） */
function day(date: string, values: { onset?: string; asleepMin?: number; steps?: number | null; usable?: boolean; unusual?: boolean } = {}): AnalysisDay {
  const onsetText = values.onset ?? "01:00";
  const onset = onsetText.startsWith("-") ? at(addDays(date, -1), onsetText.slice(1)) : at(date, onsetText);
  const asleepMin = values.asleepMin ?? 420;
  return {
    date,
    unusual: values.unusual ?? false,
    metrics: {
      sleep: { onset, wake: onset + (asleepMin + 20) * 60_000, asleepMin, efficiency: 0.95, awakenings: 1 },
      body: { hrvAvg: 50, restingHeartRate: 55 },
      day: { steps: values.steps ?? 8000, daylightMin: 20, morningDaylightMin: 10, workoutMin: 0 },
      morning: { feeling: null },
      nextDay: { condition: null },
      quality: { usable: values.usable ?? true },
    },
  };
}

/** start から n日分の日付 */
const dates = (start: string, n: number) => Array.from({ length: n }, (_, i) => addDays(start, i));

describe("差の範囲（ブートストラップ法）", () => {
  it("ばらつきのない2つのグループなら、差の範囲は差そのもの", () => {
    expect(meanDiffInterval([10, 10, 10, 10, 10, 10, 10], [0, 0, 0, 0, 0, 0, 0])).toEqual([10, 10]);
  });

  it("同じ値どうしなら、差の範囲は0をまたぐ", () => {
    const same = [1, 2, 3, 4, 5, 6, 7, 8];
    const [low, high] = meanDiffInterval(same, same);
    expect(low).toBeLessThan(0);
    expect(high).toBeGreaterThan(0);
  });
});

describe("2つのグループの比べ方（分析の設計 7章の決まり）", () => {
  // 32日に、A と B を1日ずつ交互に置く（前半16日・後半16日）
  const points = (valueA: (i: number) => number, valueB: (i: number) => number, countA = 16): Point[] =>
    dates("2026-01-01", 32).flatMap((date, i): Point[] =>
      i % 2 === 0 ? (i / 2 < countA ? [{ date, group: "A", value: valueA(i) }] : []) : [{ date, group: "B", value: valueB(i) }],
    );

  it("前半と後半の両方で同じ向きの差があれば「気づきの候補」", () => {
    const result = compareGroups(points((i) => 20 + (i % 3), (i) => 10 + (i % 3)));
    expect(result.verdict).toBe("気づきの候補");
    expect(result.nA).toBe(16);
    expect(result.nB).toBe(16);
    expect(result.diff).toBeCloseTo(10, 0);
  });

  it("前半だけ差が大きく、後半は逆の向きなら「前半と後半で向きが違う」", () => {
    // 前半: A 40・B 10（+30）、後半: A 10・B 12（−2）。全体では +14 で、差の範囲は0をまたがない
    const result = compareGroups(points((i) => (i < 16 ? 40 : 10), (i) => (i < 16 ? 10 : 12)));
    expect(result.halves[0]).toBe(30);
    expect(result.halves[1]).toBe(-2);
    expect(result.interval![0]).toBeGreaterThan(0);
    expect(result.verdict).toBe("前半と後半で向きが違う");
  });

  it("差の範囲が0をまたげば「差ははっきりしない」", () => {
    const result = compareGroups(points((i) => (i % 4 === 0 ? 1 : 5), (i) => (i % 4 === 1 ? 1 : 5)));
    expect(result.verdict).toBe("差ははっきりしない");
  });

  it("どちらかが7夜より少なければ「データ不足」で、差を出さない", () => {
    const result = compareGroups(points(() => 20, () => 10, 6));
    expect(result.nA).toBe(6);
    expect(result.verdict).toBe("データ不足");
    expect(result.diff).toBeNull();
  });
});

describe("夜の表", () => {
  it("使えない夜と、体調がいつもと違う日を外して数える", () => {
    const rows = [day("2026-02-01"), day("2026-02-02", { usable: false }), day("2026-02-03", { unusual: true }), day("2026-02-04")];
    const { nights, excluded } = toNights(rows, TZ);
    expect(nights.map((night) => night.date)).toEqual(["2026-02-01", "2026-02-04"]);
    expect(excluded).toEqual({ unusable: 1, unusual: 1 });
  });

  it("いつもの寝つく時刻は、それまでの28日の夜（14夜以上）の中央値。日付をまたいでも続けて数える", () => {
    const rows = [...dates("2026-02-01", 14).map((date) => day(date, { onset: "-23:30" })), day("2026-02-15", { onset: "00:50" })];
    const { nights } = toNights(rows, TZ);
    expect(nights[13].usualOnsetMin).toBeNull(); // 13夜しかない
    // 23:30 は 18:00 から 330分。00:50 は 410分
    expect(nights[14].usualOnsetMin).toBe(330);
    expect(nights[14].onsetMin).toBe(410);
  });
});

describe("仮説ごとの比較", () => {
  it("H8: いつもより1時間以上遅く寝ついた夜と、いつも通り（前後30分以内）の夜に分ける", () => {
    const rows = [
      ...dates("2026-03-01", 20).map((date) => day(date, { onset: "01:00" })),
      day("2026-03-21", { onset: "02:30" }), // 90分遅い
      day("2026-03-22", { onset: "01:20" }), // 20分遅い（いつも通り）
      day("2026-03-23", { onset: "01:45" }), // 45分遅い（どちらにも入れない）
    ];
    const h8 = analyze(rows, TZ).comparisons.find((comparison) => comparison.id === "H8")!;
    // いつもの時刻ができるのは15夜目から: 3/15〜3/20 の6夜と 3/22
    expect(h8.nA).toBe(1);
    expect(h8.nB).toBe(7);
  });

  it("H9: その夜の睡眠と、そのあとの日中（次の行の歩数）を比べる", () => {
    // 偶数の日は8時間、奇数の日は5時間眠る。次の日中の歩数は、8時間の夜のあとは 9,000歩、5時間の夜のあとは 6,000歩
    const days = dates("2026-04-01", 30);
    const rows = days.map((date, i) => {
      const previousLong = i > 0 && (i - 1) % 2 === 0;
      return day(date, { asleepMin: i % 2 === 0 ? 480 : 300, steps: i === 0 ? 7000 : previousLong ? 9000 : 6000 });
    });
    const h9 = analyze(rows, TZ).comparisons.find((comparison) => comparison.id === "H9")!;
    const steps = h9.outcomes.find((outcome) => outcome.label.startsWith("歩数"))!;
    // 最後の夜は次の日がないので外れる: 8時間の夜15夜、5時間の夜14夜
    expect(steps.nA).toBe(15);
    expect(steps.nB).toBe(14);
    expect(steps.diff).toBe(3000);
    expect(steps.verdict).toBe("気づきの候補");

    const lines = formatReport(analyze(rows, TZ)).join("\n");
    expect(lines).toContain("H9");
    expect(lines).toContain("+3,000歩（差の範囲 +3,000〜+3,000）  気づきの候補");
    // 朝の体感・日中の調子は記録がないので、行を出さずにまとめて伝える
    expect(lines).not.toMatch(/^ {2}日中の調子/m);
    expect(lines).toContain("朝の体感・日中の調子は、記録がそれぞれ7夜以上たまってから比べます");
  });
});
