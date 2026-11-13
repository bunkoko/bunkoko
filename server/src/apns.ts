// Apple の通知の仕組み（APNs）に、サーバーから直接送る。
// 認証は、Apple Developer Program で作る通知用の鍵（.p8）で署名した JWT（ES256）を使う。
// 仕様: https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns

export interface ApnsConfig {
  /** .p8 ファイルの中身（-----BEGIN PRIVATE KEY----- から始まる文字列） */
  keyP8: string;
  keyId: string;
  teamId: string;
  /** アプリの ID（apns-topic に使う） */
  bundleId: string;
}

export interface ApnsMessage {
  title: string;
  body: string;
  /** アプリが通知を開いたときに使う情報 */
  data?: Record<string, string>;
}

export type ApnsResult = { ok: true } | { ok: false; status: number; reason: string; unregistered: boolean };

const HOSTS = {
  production: "https://api.push.apple.com",
  sandbox: "https://api.sandbox.push.apple.com",
} as const;

/** JWT は20〜60分ごとに作り直す決まりなので、50分使い回す */
const JWT_LIFETIME_MS = 50 * 60 * 1000;
let cachedJwt: { token: string; createdAt: number; keyId: string } | null = null;

function base64Url(bytes: Uint8Array | string): string {
  const raw = typeof bytes === "string" ? new TextEncoder().encode(bytes) : bytes;
  let binary = "";
  for (const byte of raw) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToPkcs8(pem: string): ArrayBuffer {
  const base64 = pem.replace(/-----(BEGIN|END) PRIVATE KEY-----/g, "").replace(/\s+/g, "");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export async function createApnsJwt(config: ApnsConfig, nowMs: number): Promise<string> {
  const key = await crypto.subtle.importKey("pkcs8", pemToPkcs8(config.keyP8), { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const header = base64Url(JSON.stringify({ alg: "ES256", kid: config.keyId }));
  const claims = base64Url(JSON.stringify({ iss: config.teamId, iat: Math.floor(nowMs / 1000) }));
  const signingInput = `${header}.${claims}`;
  // WebCrypto の ECDSA の署名は、JWT が求める形（r と s を並べた64バイト）で返る
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(signingInput));
  return `${signingInput}.${base64Url(new Uint8Array(signature))}`;
}

async function jwtFor(config: ApnsConfig, nowMs: number): Promise<string> {
  if (cachedJwt && cachedJwt.keyId === config.keyId && nowMs - cachedJwt.createdAt < JWT_LIFETIME_MS) return cachedJwt.token;
  const token = await createApnsJwt(config, nowMs);
  cachedJwt = { token, createdAt: nowMs, keyId: config.keyId };
  return token;
}

export function resetApnsJwtCache(): void {
  cachedJwt = null;
}

export async function sendApns(
  config: ApnsConfig,
  device: { token: string; environment: "sandbox" | "production" },
  message: ApnsMessage,
  nowMs: number,
  fetchFn: (input: string, init?: RequestInit) => Promise<Response> = (input, init) => fetch(input, init),
): Promise<ApnsResult> {
  let response: Response;
  try {
    response = await fetchFn(`${HOSTS[device.environment]}/3/device/${device.token}`, {
      method: "POST",
      headers: {
        authorization: `bearer ${await jwtFor(config, nowMs)}`,
        "apns-topic": config.bundleId,
        "apns-push-type": "alert",
        "apns-priority": "10",
        // 1時間以内に届けられなければ捨てる（古い「寝る前の確認」が朝に届かないように）
        "apns-expiration": String(Math.floor(nowMs / 1000) + 3600),
        "content-type": "application/json",
      },
      body: JSON.stringify({
        aps: { alert: { title: message.title, body: message.body }, sound: "default" },
        ...(message.data ?? {}),
      }),
    });
  } catch (error) {
    // 通信そのものができなかった（名前が引けない・つながらないなど）。送り先は消さず、次の機会に送り直す
    return { ok: false, status: 0, reason: error instanceof Error ? error.message : String(error), unregistered: false };
  }
  if (response.ok) return { ok: true };
  let reason = "";
  try {
    reason = ((await response.json()) as { reason?: string }).reason ?? "";
  } catch {
    reason = await response.text().catch(() => "");
  }
  // 410 Unregistered / 400 BadDeviceToken: アプリが消された・識別子が古いので、送り先から外す
  const unregistered = response.status === 410 || reason === "BadDeviceToken" || reason === "Unregistered";
  return { ok: false, status: response.status, reason, unregistered };
}
