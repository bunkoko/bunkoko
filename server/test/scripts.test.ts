import { describe, expect, it } from "vitest";
import { clock, formatDay, formatDevice, formatNight, hm, maskSecrets } from "../scripts/admin";
import { cleanPastedValue, secretsToUpload } from "../scripts/setup-secrets";
import { webhookUrl } from "../scripts/switchbot-cli";

describe("手元のコマンド", () => {
  it("出力にまぎれた合言葉を名前に置き換える", () => {
    expect(maskSecrets("https://x/webhooks/switchbot/abcdef0123456789", { WEBHOOK_SECRET: "abcdef0123456789", EMPTY: undefined })).toBe(
      "https://x/webhooks/switchbot/<WEBHOOK_SECRET>",
    );
  });

  it("Webhook の送り先は、サーバーの URL と合言葉から作る", () => {
    expect(webhookUrl(undefined, "https://s.example.workers.dev/", "secret123")).toBe("https://s.example.workers.dev/webhooks/switchbot/secret123");
    expect(webhookUrl("https://s.example.workers.dev/webhooks/switchbot/zzz", undefined, "secret123")).toBe("https://s.example.workers.dev/webhooks/switchbot/zzz");
    expect(webhookUrl(undefined, undefined, "secret123")).toBeNull();
    expect(webhookUrl(undefined, "https://s.example.workers.dev", undefined)).toBeNull();
  });

  it("貼り付けた値は前後の空白を取り、途中に空白があればまちがいとみなす", () => {
    expect(cleanPastedValue("  abc123\n")).toBe("abc123");
    expect(cleanPastedValue("abc 123")).toBeNull();
    expect(cleanPastedValue("   ")).toBeNull();
  });

  it("手元だけで使う値と空の値は、Cloudflare に登録しない", () => {
    expect(secretsToUpload({ SERVER_URL: "https://x", SWITCHBOT_API_BASE: "http://127.0.0.1:8788", APP_TOKEN: "a", SWITCHBOT_TOKEN: "" })).toEqual({ APP_TOKEN: "a" });
  });

  it("1日 = 1行の表と機器を、1行ずつの文字にする", () => {
    const onset = Date.parse("2026-11-23T17:07:00Z");
    const wake = Date.parse("2026-11-24T01:46:00Z");
    expect(clock(onset)).toBe("2:07");
    expect(hm(477)).toBe("7:57");
    const line = formatDay({
      date: "2026-11-24",
      metrics: {
        sleep: { asleepMin: 477, onset, wake, efficiency: 0.919, deepMin: 41, remMin: 140, awakenings: 2, latencyMin: 17, inBedStart: Date.parse("2026-11-23T16:50:00Z") },
        quality: { heartRateCoverage: 0.96, usable: true },
        morning: { feeling: 4 },
        nextDay: { condition: null, stress: null },
        room: { co2Max: 1450, temperatureAvg: 21.5 },
        preBed: { roomDarkAt: Date.parse("2026-11-23T16:55:00Z") },
      },
    });
    expect(line).toContain("睡眠 7:57（2:07〜10:46）");
    expect(line).toContain("布団に入った 1:50");
    expect(line).toContain("寝つき 17分");
    expect(line).toContain("CO2最高 1450ppm 室温 21.5℃");
    expect(line).toContain("部屋が暗くなった 1:55");
    // まだない値は「-」
    expect(formatDay({ date: "2026-11-01", metrics: { sleep: null, quality: {}, morning: {}, nextDay: {}, room: {}, preBed: {} } })).toContain("布団に入った -");
    expect(line).toContain("効率 92%");
    expect(line).toContain("心拍の記録 96%");
    expect(line).toContain("体感 4 調子 - ストレス -");
    expect(formatDevice({ deviceId: "A", name: "窓", type: "Contact Sensor", lastPolledAt: null, values: { openState: "close" } })).toContain("→ 窓");
    expect(formatDevice({ deviceId: "B", name: "開閉センサー 1F", type: "Contact Sensor", lastPolledAt: null, values: {} })).toContain("名前に「窓」か「ドア」を入れる");
    expect(formatDevice({ deviceId: "C", name: "プラグミニ 3A", type: "Plug Mini (JP)", lastPolledAt: null, values: {} })).toContain("名前に「加湿」か「テレビ」を入れる");
  });

  it("夜の様子を15分ごとの表にする（明るさ・布団・CO2・室温・心拍の記録があった5分の数）", () => {
    // 正午より前は 11/26、あとは 11/25（日本時間）
    const t = (hhmm: string) => Date.parse(`2026-11-${hhmm < "12:00" ? "26" : "25"}T${hhmm}:00+09:00`);
    const points = (values: [string, number][]): [number, number][] => values.map(([hhmm, v]) => [t(hhmm), v]);
    const lines = formatNight({
      date: "2026-11-26",
      window: { start: t("22:24"), end: t("01:21") },
      sleep: { inBedStart: t("00:24"), onset: t("00:49"), wake: t("08:21") },
      heartRate: points([["00:45", 60], ["00:50", 58], ["01:00", 56], ["01:05", 55], ["01:10", 55]]),
      series: {
        lightLevel: points([["22:30", 12], ["22:35", 12], ["22:40", 12], ["23:15", 3], ["23:20", 1], ["23:25", 1]]),
        inBed: points([["00:15", 0], ["00:20", 0], ["00:25", 1], ["00:30", 1], ["00:35", 1], ["00:40", 1]]),
        co2: points([["22:30", 1100], ["22:35", 1120], ["22:40", 1140]]),
        temperature: points([["22:30", 23.04], ["22:35", 23.1], ["22:40", 23.2]]),
      },
      events: [
        { t: t("19:30"), kind: "meal", label: "カレー" },
        { t: t("23:15"), kind: "roomDark" },
        { t: t("23:40"), kind: "windowOpen" },
      ],
    });
    expect(lines).toEqual([
      "2026-11-26 の夜（15分ごと）",
      "        19:30 食事（カレー）",
      "時刻  明るさ  布団  CO2   室温  心拍",
      "22:30 12      -     1120  23.1  0/3",
      "22:45 -       -     -     -     0/3",
      "23:00 -       -     -     -     0/3",
      "23:15 1〜3    -     -     -     0/3",
      "        23:15 部屋が暗くなった",
      "23:30 -       -     -     -     0/3",
      "        23:40 窓を開けた",
      "23:45 -       -     -     -     0/3",
      "0:00  -       -     -     -     0/3",
      "0:15  -       1/3   -     -     0/3",
      "        0:24 布団に入った",
      "0:30  -       3/3   -     -     0/3",
      "0:45  -       -     -     -     2/3",
      "        0:49 寝ついた",
      "1:00  -       -     -     -     3/3",
      "        8:21 起きた",
      "明るさ: 人感センサーの値（1〜20。5 より下を「暗い」とみなしている）。布団・心拍: 15分のうち記録があった数（5分ごと）",
    ]);
  });
});
