// SwitchBot API v1.1 のクライアント
// 仕様: https://github.com/OpenWonderLabs/SwitchBotAPI

export const DEFAULT_API_BASE = "https://api.switch-bot.com";

export interface SwitchBotDevice {
  deviceId: string;
  deviceName?: string;
  deviceType?: string;
  enableCloudService?: boolean;
  hubDeviceId?: string;
  [key: string]: unknown;
}

export interface DeviceListBody {
  deviceList: SwitchBotDevice[];
  infraredRemoteList: Record<string, unknown>[];
}

export type DeviceStatus = Record<string, unknown>;

export class SwitchBotApiError extends Error {
  readonly httpStatus: number;
  /** SwitchBot の statusCode（100 が成功。161 = 機器がオフライン、171 = ハブがオフライン など） */
  readonly statusCode: number | null;

  constructor(message: string, httpStatus: number, statusCode: number | null) {
    super(message);
    this.name = "SwitchBotApiError";
    this.httpStatus = httpStatus;
    this.statusCode = statusCode;
  }
}

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

export interface SwitchBotClientOptions {
  token: string;
  secret: string;
  baseUrl?: string;
  fetchFn?: FetchFn;
  now?: () => number;
  nonce?: () => string;
}

/** 署名 = base64(HMAC-SHA256(key = secret, message = token + t + nonce)) */
export async function createSignature(
  token: string,
  secret: string,
  t: string,
  nonce: string,
): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(token + t + nonce));
  return toBase64(new Uint8Array(signature));
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function truncate(text: string, max = 300): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export class SwitchBotClient {
  private readonly token: string;
  private readonly secret: string;
  private readonly baseUrl: string;
  private readonly fetchFn: FetchFn;
  private readonly now: () => number;
  private readonly nonce: () => string;

  constructor(options: SwitchBotClientOptions) {
    if (!options.token || !options.secret) {
      throw new Error("SwitchBot のトークンとシークレットが設定されていません");
    }
    this.token = options.token;
    this.secret = options.secret;
    this.baseUrl = (options.baseUrl ?? DEFAULT_API_BASE).replace(/\/+$/, "");
    // Workers では fetch を別のオブジェクトのメソッドとして呼ぶとエラーになるため、関数で包む
    this.fetchFn = options.fetchFn ?? ((input, init) => fetch(input, init));
    this.now = options.now ?? (() => Date.now());
    this.nonce = options.nonce ?? (() => crypto.randomUUID());
  }

  async authHeaders(): Promise<Record<string, string>> {
    const t = String(this.now());
    const nonce = this.nonce();
    const sign = await createSignature(this.token, this.secret, t, nonce);
    return {
      Authorization: this.token,
      sign,
      nonce,
      t,
      "Content-Type": "application/json; charset=utf8",
    };
  }

  private async request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    const response = await this.fetchFn(`${this.baseUrl}${path}`, {
      method,
      headers: await this.authHeaders(),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) {
      // 401 はトークンの誤り、または1日の利用回数の上限（10,000回）を超えたとき
      throw new SwitchBotApiError(`HTTP ${response.status}: ${truncate(text)}`, response.status, null);
    }
    let json: { statusCode?: number; message?: string; body?: unknown };
    try {
      json = JSON.parse(text);
    } catch {
      throw new SwitchBotApiError(`JSON ではない応答: ${truncate(text)}`, response.status, null);
    }
    if (json.statusCode !== 100) {
      throw new SwitchBotApiError(
        `statusCode ${json.statusCode ?? "なし"}: ${json.message ?? ""}`,
        response.status,
        typeof json.statusCode === "number" ? json.statusCode : null,
      );
    }
    return (json.body ?? {}) as T;
  }

  async listDevices(): Promise<DeviceListBody> {
    const body = await this.request<Partial<DeviceListBody>>("GET", "/v1.1/devices");
    return {
      deviceList: Array.isArray(body.deviceList) ? body.deviceList : [],
      infraredRemoteList: Array.isArray(body.infraredRemoteList) ? body.infraredRemoteList : [],
    };
  }

  getStatus(deviceId: string): Promise<DeviceStatus> {
    return this.request<DeviceStatus>("GET", `/v1.1/devices/${encodeURIComponent(deviceId)}/status`);
  }

  setupWebhook(url: string): Promise<unknown> {
    return this.request("POST", "/v1.1/webhook/setupWebhook", {
      action: "setupWebhook",
      url,
      deviceList: "ALL",
    });
  }

  queryWebhookUrls(): Promise<{ urls?: string[] }> {
    return this.request("POST", "/v1.1/webhook/queryWebhook", { action: "queryUrl" });
  }

  deleteWebhook(url: string): Promise<unknown> {
    return this.request("POST", "/v1.1/webhook/deleteWebhook", { action: "deleteWebhook", url });
  }
}
