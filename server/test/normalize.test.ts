import { describe, expect, it } from "vitest";
import { fahrenheitToCelsius, normalizeDeviceId, toReadings } from "../src/normalize";

describe("toReadings", () => {
  it("CO2センサーの状態から、機器の情報を除いた値だけを取り出す", () => {
    const readings = toReadings({
      deviceId: "C1",
      deviceType: "MeterPro(CO2)",
      hubDeviceId: "H1",
      version: "V1.2",
      battery: 90,
      temperature: 22.1,
      humidity: 50,
      CO2: 1203,
    });
    expect(readings).toEqual([
      { metric: "battery", valueNum: 90, valueText: null },
      { metric: "temperature", valueNum: 22.1, valueText: null },
      { metric: "humidity", valueNum: 50, valueText: null },
      { metric: "CO2", valueNum: 1203, valueText: null },
    ]);
  });

  it("真偽値は 1/0、数字の文字列は数値としても保存する", () => {
    expect(toReadings({ Detected: true, moving: false, slidePosition: "75" })).toEqual([
      { metric: "Detected", valueNum: 1, valueText: "true" },
      { metric: "moving", valueNum: 0, valueText: "false" },
      { metric: "slidePosition", valueNum: 75, valueText: "75" },
    ]);
  });

  it("文字の値とオブジェクトは文字列で保存し、null は飛ばす", () => {
    expect(
      toReadings({ openState: "open", filterElement: { usedHours: 20 }, power: null, extra: undefined }),
    ).toEqual([
      { metric: "openState", valueNum: null, valueText: "open" },
      { metric: "filterElement", valueNum: null, valueText: '{"usedHours":20}' },
    ]);
  });

  it("数値でない数（NaN など）は保存しない", () => {
    expect(toReadings({ temperature: Number.NaN })).toEqual([]);
  });
});

describe("normalizeDeviceId", () => {
  it("区切りを取り、大文字にそろえる", () => {
    expect(normalizeDeviceId("b0:e9:fe:58:29:74")).toBe("B0E9FE582974");
  });
});

describe("fahrenheitToCelsius", () => {
  it("小数第1位までの摂氏にする", () => {
    expect(fahrenheitToCelsius(72.5)).toBe(22.5);
  });
});
