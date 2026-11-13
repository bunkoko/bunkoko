// 受け取ったデータの形を確かめる小さな道具。
// 無料プランは1回の処理時間が短いため、ライブラリを使わず、必要な分だけ手で確かめる。

export class Problems {
  readonly list: string[] = [];
  private static readonly MAX = 20;

  add(path: string, message: string): void {
    if (this.list.length < Problems.MAX) this.list.push(`${path}: ${message}`);
  }

  get ok(): boolean {
    return this.list.length === 0;
  }
}

export type Json = Record<string, unknown>;

export function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 2000年〜2100年の間のエポックミリ秒 */
export function isEpochMs(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 946_684_800_000 && (value as number) <= 4_102_444_800_000;
}

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function optionalNumber(problems: Problems, path: string, value: unknown, min = -Infinity, max = Infinity): number | null {
  if (value === undefined || value === null) return null;
  if (!isFiniteNumber(value) || value < min || value > max) {
    problems.add(path, `${min}〜${max} の数値にしてください`);
    return null;
  }
  return value;
}

export function optionalString(problems: Problems, path: string, value: unknown, maxLength: number): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string" || value.length > maxLength) {
    problems.add(path, `${maxLength} 文字以内の文字列にしてください`);
    return null;
  }
  return value;
}

export function requiredString(problems: Problems, path: string, value: unknown, maxLength: number, pattern?: RegExp): string | null {
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength || (pattern && !pattern.test(value))) {
    problems.add(path, `1〜${maxLength} 文字の正しい形の文字列にしてください`);
    return null;
  }
  return value;
}

export function oneOf<T extends string>(problems: Problems, path: string, value: unknown, allowed: readonly T[], optional = false): T | null {
  if (optional && (value === undefined || value === null)) return null;
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    problems.add(path, `${allowed.join(" / ")} のどれかにしてください`);
    return null;
  }
  return value as T;
}

export function arrayOf(problems: Problems, path: string, value: unknown, optional = true): unknown[] {
  if (value === undefined && optional) return [];
  if (!Array.isArray(value)) {
    problems.add(path, "配列にしてください");
    return [];
  }
  return value;
}
