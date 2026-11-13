// Claude Code のスケジュールされたタスク向けの API（/ai/*）。AI_TASK_TOKEN が必要。
// できることは「目安がまだの食事を読む」「目安を書き込む」だけにしている。

import { hasBearer, jsonError, notFound, readJsonBody, unauthorized } from "./http";
import { MAX_PENDING, listPendingMeals, parseEstimate, parseEstimateBatch, saveEstimates } from "./meals";
import { getSettings } from "./settings";
import { Problems, isRecord, optionalString } from "./validate";

export interface AiEnv {
  DB: D1Database;
  AI_TASK_TOKEN: string;
  TIME_ZONE?: string;
}

export async function handleAi(request: Request, url: URL, env: AiEnv, userId: string, now: number): Promise<Response> {
  if (!(await hasBearer(request, env.AI_TASK_TOKEN))) return unauthorized();
  const settings = await getSettings(env.DB, userId, env.TIME_ZONE || "Asia/Tokyo");

  if (request.method === "GET" && url.pathname === "/ai/meals/pending") {
    const limit = Math.min(Math.max(Number(url.searchParams.get("limit") ?? "50") || 50, 1), MAX_PENDING);
    const meals = await listPendingMeals(env.DB, userId, settings.timeZone, now, limit);
    return Response.json({ ok: true, meals });
  }

  if (request.method === "POST" && url.pathname === "/ai/meals/estimates") {
    const body = await readJsonBody(request, 256 * 1024);
    if (!body.ok) return body.response;
    const problems = new Problems();
    const inputs = parseEstimateBatch(body.value, problems);
    if (!problems.ok) return jsonError(400, "目安の形に問題があります", problems.list);
    return Response.json({ ok: true, ...(await saveEstimates(env.DB, userId, settings.timeZone, "ai", inputs, now)) });
  }

  const single = /^\/ai\/meals\/([A-Za-z0-9-]{1,64})\/estimate$/.exec(url.pathname);
  if (request.method === "POST" && single) {
    const body = await readJsonBody(request, 32 * 1024);
    if (!body.ok) return body.response;
    const problems = new Problems();
    const value = isRecord(body.value) ? body.value : {};
    const model = optionalString(problems, "model", value.model, 100);
    const estimate = parseEstimate(problems, "estimate", value.estimate);
    if (!problems.ok || !estimate) return jsonError(400, "目安の形に問題があります", problems.list);
    const result = await saveEstimates(env.DB, userId, settings.timeZone, "ai", [{ id: single[1], model, estimate }], now);
    if (result.saved.length === 0) return jsonError(404, "その食事の記録はありません");
    return Response.json({ ok: true, ...result });
  }

  return notFound();
}
