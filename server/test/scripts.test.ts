import { describe, expect, it } from "vitest";
import { clock, formatDay, formatDevice, hm, maskSecrets } from "../scripts/admin";
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
        sleep: { asleepMin: 477, onset, wake, efficiency: 0.919, deepMin: 41, remMin: 140, awakenings: 2, latencyMin: null },
        quality: { heartRateCoverage: 0.96, usable: true },
        morning: { feeling: 4 },
        nextDay: { condition: null, stress: null },
        room: { co2Max: null, temperatureAvg: null },
      },
    });
    expect(line).toContain("睡眠 7:57（2:07〜10:46）");
    expect(line).toContain("効率 92%");
    expect(line).toContain("心拍の記録 96%");
    expect(line).toContain("体感 4 調子 - ストレス -");
    expect(formatDevice({ deviceId: "A", name: "窓", type: "Contact Sensor", lastPolledAt: null, values: { openState: "close" } })).toContain("→ 窓");
    expect(formatDevice({ deviceId: "B", name: "開閉センサー 1F", type: "Contact Sensor", lastPolledAt: null, values: {} })).toContain("名前に「窓」か「ドア」を入れる");
    expect(formatDevice({ deviceId: "C", name: "プラグミニ 3A", type: "Plug Mini (JP)", lastPolledAt: null, values: {} })).toContain("名前に「加湿」か「テレビ」を入れる");
  });
});
