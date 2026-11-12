// SwitchBot の応答（機器の状態・Webhook）を、保存用の「項目ごとの値」に変換する。
// 機器の種類ごとの対応表は持たず、値の型から機械的に変換する。
// こうしておくと、新しい機器や項目が増えても取りこぼさない。

/** 値ではなく、機器を表す情報なので保存しない項目 */
const METADATA_KEYS = new Set([
  "deviceId",
  "deviceType",
  "deviceName",
  "hubDeviceId",
  "version",
  "deviceMac",
  "timeOfSample",
  "scale",
]);

const NUMERIC_TEXT = /^-?\d+(?:\.\d+)?$/;

export interface Reading {
  metric: string;
  valueNum: number | null;
  valueText: string | null;
}

export function toReadings(values: Record<string, unknown>): Reading[] {
  const readings: Reading[] = [];
  for (const [metric, value] of Object.entries(values)) {
    if (METADATA_KEYS.has(metric) || value === null || value === undefined) continue;

    if (typeof value === "number") {
      if (Number.isFinite(value)) readings.push({ metric, valueNum: value, valueText: null });
      continue;
    }
    if (typeof value === "boolean") {
      readings.push({ metric, valueNum: value ? 1 : 0, valueText: String(value) });
      continue;
    }
    if (typeof value === "string") {
      const trimmed = value.trim();
      readings.push({
        metric,
        valueNum: NUMERIC_TEXT.test(trimmed) ? Number(trimmed) : null,
        valueText: value,
      });
      continue;
    }
    // オブジェクトや配列（例: 加湿器のフィルター情報）は JSON の文字列で保存する
    readings.push({ metric, valueNum: null, valueText: JSON.stringify(value) });
  }
  return readings;
}

/** SwitchBot の Webhook の deviceMac を、API の deviceId と同じ形（大文字・区切りなし）にする */
export function normalizeDeviceId(deviceMac: string): string {
  return deviceMac.replace(/[:\-\s]/g, "").toUpperCase();
}

export function fahrenheitToCelsius(fahrenheit: number): number {
  return Math.round(((fahrenheit - 32) * 5 * 10) / 9) / 10;
}
