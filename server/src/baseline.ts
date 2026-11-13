// 「いつもの自分」（docs/analysis.md の 4章）
// 直近28日の中央値と、ふだんのばらつきの幅（中央値からのずれの中央値 × 1.4826）を使う。
// 平均と標準偏差ではなく中央値を使うのは、体調が悪かった日などの極端な値に引っぱられにくいため。

export const BASELINE_DAYS = 28;
export const MIN_BASELINE_DAYS = 14;
/** これ以上ずれていたら「珍しい」 [TBD: 実データで調整] */
export const UNUSUAL_Z = 2;

export function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle];
}

export interface Deviation {
  value: number;
  median: number;
  /** ふだんのばらつきの幅 */
  scale: number;
  /** いつもとの差 ÷ ふだんの幅（±1 ならよくある範囲、±2 を超えたら珍しい） */
  z: number;
}

/** history は「その日より前」の値（新しい順でも古い順でもよい） */
export function deviation(value: number | null | undefined, history: number[], minScale = 1e-6): Deviation | null {
  if (value === null || value === undefined || history.length < MIN_BASELINE_DAYS) return null;
  const center = median(history);
  const scale = Math.max(1.4826 * median(history.map((v) => Math.abs(v - center))), minScale);
  return {
    value,
    median: Math.round(center * 100) / 100,
    scale: Math.round(scale * 100) / 100,
    z: Math.round(((value - center) / scale) * 100) / 100,
  };
}
