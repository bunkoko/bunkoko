import { describe, expect, it } from "vitest";
import {
  addDays,
  formatLocal,
  isValidDate,
  isValidHhmm,
  isValidTimeZone,
  localDate,
  localMidnight,
  localMinuteOfDay,
  localTimeOn,
} from "../src/time";

const TOKYO = "Asia/Tokyo";

describe("localDate", () => {
  it("UTC では前日でも、日本時間の日付を返す", () => {
    expect(localDate(Date.parse("2026-11-20T15:30:00Z"), TOKYO)).toBe("2026-11-21");
    expect(localDate(Date.parse("2026-11-20T14:59:59Z"), TOKYO)).toBe("2026-11-20");
  });

  it("夏時間のあるタイムゾーンでも日付がずれない", () => {
    // ニューヨークの夏時間の終わり（2026-11-01 2:00 に 1:00 へ戻る）
    expect(localDate(Date.parse("2026-11-01T03:30:00Z"), "America/New_York")).toBe("2026-10-31");
    expect(localDate(Date.parse("2026-11-01T05:30:00Z"), "America/New_York")).toBe("2026-11-01");
  });
});

describe("localTimeOn / localMidnight", () => {
  it("日本時間のその日の時刻を UTC にする", () => {
    expect(localTimeOn("2026-11-20", "23:30", TOKYO)).toBe(Date.parse("2026-11-20T14:30:00Z"));
    expect(localMidnight("2026-11-21", TOKYO)).toBe(Date.parse("2026-11-20T15:00:00Z"));
  });

  it("夏時間の切り替え日でも正しい", () => {
    expect(localTimeOn("2026-11-01", "12:00", "America/New_York")).toBe(Date.parse("2026-11-01T17:00:00Z"));
  });
});

describe("そのほか", () => {
  it("日付を足し引きする", () => {
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });

  it("日本時間で表示する", () => {
    expect(formatLocal(Date.parse("2026-11-20T13:00:00Z"), TOKYO)).toBe("2026-11-20 22:00:00");
    expect(localMinuteOfDay(Date.parse("2026-11-20T13:05:00Z"), TOKYO)).toBe(22 * 60 + 5);
  });

  it("形を確かめる", () => {
    expect(isValidTimeZone("Asia/Tokyo")).toBe(true);
    expect(isValidTimeZone("Mars/Base")).toBe(false);
    expect(isValidDate("2026-02-30")).toBe(false);
    expect(isValidDate("2026-02-28")).toBe(true);
    expect(isValidHhmm("23:30")).toBe(true);
    expect(isValidHhmm("24:00")).toBe(false);
  });
});
