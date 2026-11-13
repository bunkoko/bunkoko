import { describe, expect, it } from "vitest";
import { MIN_BASELINE_DAYS, deviation, median } from "../src/baseline";

describe("median", () => {
  it("真ん中の値を返す（偶数個なら真ん中2つの平均）", () => {
    expect(median([3, 1, 2])).toBe(2);
    expect(median([4, 1, 3, 2])).toBe(2.5);
  });
});

describe("deviation", () => {
  const history = [50, 51, 49, 50, 52, 48, 50, 51, 49, 50, 50, 51, 49, 50];

  it(`${MIN_BASELINE_DAYS}日分に満たなければ出さない`, () => {
    expect(deviation(55, history.slice(1))).toBeNull();
    expect(deviation(null, history)).toBeNull();
  });

  it("中央値と、ふだんの幅（中央値からのずれの中央値 × 1.4826）で、いつもとの差を出す", () => {
    // 中央値 50、ずれの中央値 1 → 幅 1.48
    expect(deviation(53, history)).toEqual({ value: 53, median: 50, scale: 1.48, z: 2.02 });
  });

  it("極端な値が混ざっても、基準線は引っぱられにくい", () => {
    const withOutlier = [...history.slice(0, 13), 90];
    expect(deviation(50, withOutlier)?.median).toBe(50);
  });

  it("値がほとんど変わらない人でも、幅の下限より小さくはしない", () => {
    const flat = Array.from({ length: 20 }, () => 50);
    expect(deviation(51, flat, 1)?.z).toBe(1);
  });
});
