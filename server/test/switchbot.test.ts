import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import { SwitchBotApiError, SwitchBotClient, createSignature, type FetchFn } from "../src/switchbot";

interface Captured {
  url: string;
  init?: RequestInit;
}

function fakeFetch(respond: (url: string) => Response): { fetchFn: FetchFn; calls: Captured[] } {
  const calls: Captured[] = [];
  const fetchFn: FetchFn = async (url, init) => {
    calls.push({ url, init });
    return respond(url);
  };
  return { fetchFn, calls };
}

const ok = (body: unknown) => Response.json({ statusCode: 100, message: "success", body });

function newClient(fetchFn: FetchFn) {
  return new SwitchBotClient({
    token: "my-token",
    secret: "my-secret",
    fetchFn,
    now: () => 1661927531000,
    nonce: () => "fixed-nonce",
  });
}

describe("createSignature", () => {
  it("公式の手順（HMAC-SHA256 を base64 にしたもの）と一致する", async () => {
    const expected = createHmac("sha256", "my-secret")
      .update("my-token" + "1661927531000" + "fixed-nonce")
      .digest("base64");
    await expect(createSignature("my-token", "my-secret", "1661927531000", "fixed-nonce")).resolves.toBe(expected);
  });
});

describe("SwitchBotClient", () => {
  it("認証用のヘッダーを付けて機器一覧を取得する", async () => {
    const { fetchFn, calls } = fakeFetch(() =>
      ok({ deviceList: [{ deviceId: "AAA", deviceType: "Hub 3" }], infraredRemoteList: [] }),
    );
    const result = await newClient(fetchFn).listDevices();

    expect(result.deviceList).toEqual([{ deviceId: "AAA", deviceType: "Hub 3" }]);
    expect(calls[0].url).toBe("https://api.switch-bot.com/v1.1/devices");
    const headers = calls[0].init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe("my-token");
    expect(headers.t).toBe("1661927531000");
    expect(headers.nonce).toBe("fixed-nonce");
    expect(headers.sign).toBe(await createSignature("my-token", "my-secret", "1661927531000", "fixed-nonce"));
  });

  it("一覧が空でも配列を返す", async () => {
    const { fetchFn } = fakeFetch(() => ok({}));
    await expect(newClient(fetchFn).listDevices()).resolves.toEqual({ deviceList: [], infraredRemoteList: [] });
  });

  it("機器の状態を取得する", async () => {
    const { fetchFn, calls } = fakeFetch(() => ok({ deviceId: "C1", CO2: 800 }));
    await expect(newClient(fetchFn).getStatus("C1")).resolves.toEqual({ deviceId: "C1", CO2: 800 });
    expect(calls[0].url).toBe("https://api.switch-bot.com/v1.1/devices/C1/status");
    expect(calls[0].init?.method).toBe("GET");
  });

  it("HTTP 401（トークンの誤り・回数の上限）はエラーにする", async () => {
    const { fetchFn } = fakeFetch(() => new Response('{"message":"Unauthorized"}', { status: 401 }));
    const error = await newClient(fetchFn).getStatus("C1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SwitchBotApiError);
    expect((error as SwitchBotApiError).httpStatus).toBe(401);
  });

  it("statusCode が 100 以外（例: 161 = 機器がオフライン）はエラーにする", async () => {
    const { fetchFn } = fakeFetch(() => Response.json({ statusCode: 161, message: "device offline", body: {} }));
    const error = await newClient(fetchFn).getStatus("C1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SwitchBotApiError);
    expect((error as SwitchBotApiError).statusCode).toBe(161);
  });

  it("Webhook の登録では送り先の URL と全機器を指定する", async () => {
    const { fetchFn, calls } = fakeFetch(() => ok({}));
    await newClient(fetchFn).setupWebhook("https://example.com/webhooks/switchbot/abc");
    expect(calls[0].url).toBe("https://api.switch-bot.com/v1.1/webhook/setupWebhook");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      action: "setupWebhook",
      url: "https://example.com/webhooks/switchbot/abc",
      deviceList: "ALL",
    });
  });

  it("トークンかシークレットがないと作れない", () => {
    expect(() => new SwitchBotClient({ token: "", secret: "x" })).toThrow();
  });
});
