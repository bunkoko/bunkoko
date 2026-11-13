// HTTP の共通処理: 認証・JSON の読み書き

/** 文字列を、長さや内容から推測されにくい方法で比べる */
export async function secretsMatch(given: string, expected: string | undefined): Promise<boolean> {
  if (!expected) return false;
  const encoder = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(given)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const left = new Uint8Array(a);
  const right = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) diff |= left[i] ^ right[i];
  return diff === 0;
}

/** Authorization: Bearer <token> が expected と一致するか */
export async function hasBearer(request: Request, expected: string | undefined): Promise<boolean> {
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  return match !== null && (await secretsMatch(match[1].trim(), expected));
}

export const unauthorized = () =>
  new Response("unauthorized", { status: 401, headers: { "WWW-Authenticate": "Bearer" } });

export const notFound = () => new Response("not found", { status: 404 });

export function jsonError(status: number, error: string, details?: string[]): Response {
  return Response.json({ ok: false, error, ...(details && details.length > 0 ? { details } : {}) }, { status });
}

export type JsonBody = { ok: true; value: unknown } | { ok: false; response: Response };

/** リクエストの本文を JSON として読む。大きすぎる・JSON でない場合はエラーの応答を返す */
export async function readJsonBody(request: Request, maxBytes: number): Promise<JsonBody> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > maxBytes) return { ok: false, response: jsonError(413, `本文は ${maxBytes} バイトまでです`) };
  const text = await request.text();
  if (text.length > maxBytes) return { ok: false, response: jsonError(413, `本文は ${maxBytes} バイトまでです`) };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false, response: jsonError(400, "JSON として読めません") };
  }
}
