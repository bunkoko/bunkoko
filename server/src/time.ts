// タイムゾーンと日付の計算。保存はすべて UTC のエポックミリ秒、日付は利用者のタイムゾーンでの YYYY-MM-DD。

export const MINUTE_MS = 60 * 1000;
export const HOUR_MS = 60 * MINUTE_MS;
export const DAY_MS = 24 * HOUR_MS;

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatterFor(timeZone: string): Intl.DateTimeFormat {
  let formatter = formatters.get(timeZone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    formatters.set(timeZone, formatter);
  }
  return formatter;
}

export function isValidTimeZone(timeZone: string): boolean {
  try {
    formatterFor(timeZone);
    return true;
  } catch {
    return false;
  }
}

const offsetCache = new Map<string, number>();

/** その時刻における、タイムゾーンの UTC からのずれ（ミリ秒）。1時間単位でキャッシュする */
export function timeZoneOffsetMs(epochMs: number, timeZone: string): number {
  const key = `${timeZone}|${Math.floor(epochMs / HOUR_MS)}`;
  const cached = offsetCache.get(key);
  if (cached !== undefined) return cached;

  const parts = formatterFor(timeZone).formatToParts(new Date(epochMs));
  const get = (type: string) => Number(parts.find((part) => part.type === type)?.value);
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  const offset = Math.round((asUtc - epochMs) / MINUTE_MS) * MINUTE_MS;
  if (offsetCache.size > 10_000) offsetCache.clear();
  offsetCache.set(key, offset);
  return offset;
}

/** エポックミリ秒 → そのタイムゾーンでの日付（YYYY-MM-DD） */
export function localDate(epochMs: number, timeZone: string): string {
  return new Date(epochMs + timeZoneOffsetMs(epochMs, timeZone)).toISOString().slice(0, 10);
}

/** エポックミリ秒 → そのタイムゾーンでの "YYYY-MM-DD HH:MM:SS" */
export function formatLocal(epochMs: number, timeZone: string): string {
  return new Date(epochMs + timeZoneOffsetMs(epochMs, timeZone)).toISOString().replace("T", " ").slice(0, 19);
}

/** その時刻の、タイムゾーンでの「0時からの分」 */
export function localMinuteOfDay(epochMs: number, timeZone: string): number {
  const shifted = new Date(epochMs + timeZoneOffsetMs(epochMs, timeZone));
  return shifted.getUTCHours() * 60 + shifted.getUTCMinutes();
}

export function addDays(date: string, days: number): string {
  const utc = Date.parse(`${date}T00:00:00Z`) + days * DAY_MS;
  return new Date(utc).toISOString().slice(0, 10);
}

export function isValidDate(date: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(date) && !Number.isNaN(Date.parse(`${date}T00:00:00Z`)) && addDays(date, 0) === date;
}

/** その日付の、そのタイムゾーンでの "HH:MM" の時刻（エポックミリ秒） */
export function localTimeOn(date: string, hhmm: string, timeZone: string): number {
  const [hours, minutes] = hhmm.split(":").map(Number);
  const naiveUtc = Date.parse(`${date}T00:00:00Z`) + hours * HOUR_MS + minutes * MINUTE_MS;
  // ずれは「その時刻付近」のものを使う（夏時間の切り替え日でも、ほぼ正しくなるように2回合わせる）
  const first = naiveUtc - timeZoneOffsetMs(naiveUtc, timeZone);
  return naiveUtc - timeZoneOffsetMs(first, timeZone);
}

/** その日付の、そのタイムゾーンでの0時（エポックミリ秒） */
export function localMidnight(date: string, timeZone: string): number {
  return localTimeOn(date, "00:00", timeZone);
}

export function isValidHhmm(value: string): boolean {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  return match !== null && Number(match[1]) < 24 && Number(match[2]) < 60;
}
