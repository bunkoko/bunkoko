import { generateKeyPairSync, verify } from "node:crypto";
import { beforeEach, describe, expect, it } from "vitest";
import { createApnsJwt, resetApnsJwtCache, sendApns, type ApnsConfig } from "../src/apns";
import type { DayMetrics } from "../src/days/compute";
import { evaluateRoom, morningReport, runNotifications, type RoomNow } from "../src/notify";
import { defaultSettings } from "../src/settings";
import { localTimeOn } from "../src/time";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";

const TZ = "Asia/Tokyo";
const settings = defaultSettings(TZ);
const at = (date: string, hhmm: string) => localTimeOn(date, hhmm, TZ);

const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
const config: ApnsConfig = {
  keyP8: privateKey.export({ format: "pem", type: "pkcs8" }).toString(),
  keyId: "KEY1234567",
  teamId: "TEAM123456",
  bundleId: "com.example.bunkoko",
};

const room = (values: Partial<RoomNow>): RoomNow => ({
  measuredAt: 1,
  co2: 600,
  temperature: 21,
  humidity: 50,
  lightLevel: 3,
  inBed: false,
  ceilingLight: null,
  ...values,
});

describe("evaluateRoom", () => {
  it("目安を外れた項目ごとに、何をすればよいかを返す", () => {
    const advice = evaluateRoom(room({ co2: 1250, humidity: 32 }), settings);
    expect(advice).toEqual([
      { kind: "co2", message: "CO2 が 1,250ppm です。寝る前に5分ほど換気しましょう" },
      { kind: "humidityLow", message: "湿度が 32% と低めです。加湿をつけましょう" },
    ]);
  });

  it("室温は、目安を決めるまで（null の間）は見ない", () => {
    expect(evaluateRoom(room({ temperature: 30 }), settings)).toEqual([]);
    const withRange = { ...settings, comfort: { ...settings.comfort, temperatureMin: 18, temperatureMax: 24 } };
    expect(evaluateRoom(room({ temperature: 26.5 }), withRange)[0].kind).toBe("temperatureHigh");
  });

  it("照明は寝る前だけ見る", () => {
    const lit = room({ ceilingLight: { on: true, brightness: 90, colorTemperature: 6000 } });
    expect(evaluateRoom(lit, settings)).toEqual([]);
    expect(evaluateRoom(lit, settings, { includeLight: true }).map((a) => a.kind)).toEqual(["lightColor", "lightBright"]);
  });

  it("部屋のデータがなければ何も言わない", () => {
    expect(evaluateRoom(room({ measuredAt: null, co2: 2000 }), settings)).toEqual([]);
  });
});

const metrics = (overrides: Partial<DayMetrics> = {}): DayMetrics =>
  ({
    date: "2026-11-21",
    sleep: { asleepMin: 400, latencyMin: 12, awakenings: 1 },
    room: { co2Max: 1450, temperatureMin: 20, temperatureMax: 22 },
    quality: { usable: true },
    ...overrides,
  }) as unknown as DayMetrics;

describe("morningReport", () => {
  it("睡眠時間・寝つき・中途覚醒と、部屋で目立ったことを伝える", () => {
    expect(morningReport(metrics(), settings)).toEqual({
      title: "昨夜の睡眠",
      body: "6時間40分眠りました（寝つき12分・中途覚醒1回）。CO2 が最高 1,450ppm まで上がりました。",
      data: { kind: "morning", date: "2026-11-21" },
    });
  });

  it("睡眠のデータがなければ送らない", () => {
    expect(morningReport(metrics({ sleep: null }), settings)).toBeNull();
  });
});

describe("APNs", () => {
  beforeEach(() => resetApnsJwtCache());

  it("JWT を ES256 で正しく署名する", async () => {
    const jwt = await createApnsJwt(config, Date.parse("2026-11-21T00:00:00Z"));
    const [header, claims, signature] = jwt.split(".");
    const decode = (part: string) => JSON.parse(Buffer.from(part, "base64url").toString());
    expect(decode(header)).toEqual({ alg: "ES256", kid: "KEY1234567" });
    expect(decode(claims)).toEqual({ iss: "TEAM123456", iat: Math.floor(Date.parse("2026-11-21T00:00:00Z") / 1000) });
    const valid = verify("sha256", Buffer.from(`${header}.${claims}`), { key: publicKey, dsaEncoding: "ieee-p1363" }, Buffer.from(signature, "base64url"));
    expect(valid).toBe(true);
  });

  it("決まった宛先とヘッダーで送る", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const result = await sendApns(
      config,
      { token: "ab".repeat(32), environment: "sandbox" },
      { title: "t", body: "b", data: { kind: "morning" } },
      Date.parse("2026-11-21T00:00:00Z"),
      async (url, init) => {
        calls.push({ url, init });
        return new Response(null, { status: 200 });
      },
    );
    expect(result).toEqual({ ok: true });
    expect(calls[0].url).toBe(`https://api.sandbox.push.apple.com/3/device/${"ab".repeat(32)}`);
    const headers = calls[0].init?.headers as Record<string, string>;
    expect(headers["apns-topic"]).toBe("com.example.bunkoko");
    expect(headers["apns-push-type"]).toBe("alert");
    expect(headers.authorization).toMatch(/^bearer ey/);
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ aps: { alert: { title: "t", body: "b" }, sound: "default" }, kind: "morning" });
  });

  it("通信そのものに失敗しても止まらず、失敗として返す（送り先は消さない）", async () => {
    const result = await sendApns(config, { token: "ef".repeat(32), environment: "production" }, { title: "t", body: "b" }, 0, async () => {
      throw new Error("failed to lookup address information");
    });
    expect(result).toEqual({ ok: false, status: 0, reason: "failed to lookup address information", unregistered: false });
  });

  it("410（アプリが消された）は、送り先から外すべきものとして返す", async () => {
    const result = await sendApns(config, { token: "cd".repeat(32), environment: "production" }, { title: "t", body: "b" }, 0, async () =>
      Response.json({ reason: "Unregistered" }, { status: 410 }),
    );
    expect(result).toEqual({ ok: false, status: 410, reason: "Unregistered", unregistered: true });
  });
});

describe("runNotifications", () => {
  let db: FakeD1;
  let sent: { title: string; body: string }[];
  const fetchOk = async (_url: string, init?: RequestInit) => {
    sent.push(JSON.parse(String(init?.body)).aps.alert);
    return new Response(null, { status: 200 });
  };
  const roomNow = (values: Partial<RoomNow>) => async () => room(values);

  beforeEach(() => {
    db = createFakeD1();
    sent = [];
    resetApnsJwtCache();
    db.sqlite.prepare(`INSERT INTO apns_devices VALUES ('me', ?, 'sandbox', 0, 0)`).run("ab".repeat(32));
  });

  it("目標の就寝時刻（23:30）の60分前に、部屋が目安を外れていれば1回だけ送る", async () => {
    const run = (now: number) => runNotifications(db.d1, "me", settings, config, now, roomNow({ co2: 1300 }), fetchOk);
    expect((await run(at("2026-11-20", "22:20"))).bedtime).toBe("notYet");
    expect((await run(at("2026-11-20", "22:30"))).bedtime).toBe("sent");
    expect((await run(at("2026-11-20", "22:35"))).bedtime).toBe("alreadyDone");
    expect((await run(at("2026-11-20", "22:50"))).bedtime).toBe("notYet");
    expect(sent).toEqual([{ title: "寝る前の確認", body: "CO2 が 1,300ppm です。寝る前に5分ほど換気しましょう。" }]);
    // この夜は「起きる日」の 11/21 として記録する
    expect(db.sqlite.prepare(`SELECT kind, date FROM notifications_sent`).all()).toEqual([{ kind: "bedtime", date: "2026-11-21" }]);
  });

  it("部屋に問題がなければ送らない", async () => {
    const result = await runNotifications(db.d1, "me", settings, config, at("2026-11-20", "22:30"), roomNow({}), fetchOk);
    expect(result.bedtime).toBe("nothingToSay");
    expect(sent).toEqual([]);
  });

  it("朝のレポートは、起きて20分たち、起きたあとに計算した行があるときに1回だけ送る", async () => {
    const wake = at("2026-11-21", "06:50");
    const insertDay = (computedAt: number) =>
      db.sqlite
        .prepare(`INSERT OR REPLACE INTO days (user_id, date, algo_version, computed_at, sleep_start, sleep_end, metrics_json) VALUES ('me', '2026-11-21', 1, ?, ?, ?, ?)`)
        .run(computedAt, at("2026-11-20", "23:40"), wake, JSON.stringify(metrics()));
    const run = (now: number) => runNotifications(db.d1, "me", settings, config, now, roomNow({}), fetchOk);

    insertDay(at("2026-11-21", "03:00")); // 夜の途中で計算した行
    expect((await run(at("2026-11-21", "07:30"))).morning).toBe("notYet");

    insertDay(at("2026-11-21", "07:00"));
    expect((await run(at("2026-11-21", "07:05"))).morning).toBe("notYet"); // 起きて20分たっていない
    expect((await run(at("2026-11-21", "07:15"))).morning).toBe("sent");
    expect((await run(at("2026-11-21", "07:20"))).morning).toBe("alreadyDone");
    expect(sent.map((message) => message.title)).toEqual(["昨夜の睡眠"]);
  });

  it("使われなくなった送り先は消す", async () => {
    await runNotifications(db.d1, "me", settings, config, at("2026-11-20", "22:30"), roomNow({ co2: 1300 }), async () =>
      Response.json({ reason: "BadDeviceToken" }, { status: 400 }),
    );
    expect(db.sqlite.prepare(`SELECT COUNT(*) AS n FROM apns_devices`).get()).toEqual({ n: 0 });
    // 届かなかったので「送った」とは記録しない
    expect(db.sqlite.prepare(`SELECT COUNT(*) AS n FROM notifications_sent`).get()).toEqual({ n: 0 });
  });

  it("通知の設定（鍵）がなければ何もしない", async () => {
    const result = await runNotifications(db.d1, "me", settings, null, at("2026-11-20", "22:30"), roomNow({ co2: 1300 }), fetchOk);
    expect(result).toEqual({ bedtime: "off", morning: "off" });
  });
});
