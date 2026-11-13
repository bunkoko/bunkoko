import { beforeEach, describe, expect, it } from "vitest";
import { handleRequest, type Env } from "../src/index";
import { localTimeOn } from "../src/time";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";

const BASE = "https://bunkoko.example.workers.dev";
const TZ = "Asia/Tokyo";
const NOW = localTimeOn("2026-11-21", "05:30", TZ);
const APP = "app-token";
const AI = "ai-token";

let db: FakeD1;
let env: Env;

beforeEach(() => {
  db = createFakeD1();
  env = {
    DB: db.d1,
    SWITCHBOT_TOKEN: "t",
    SWITCHBOT_SECRET: "s",
    WEBHOOK_SECRET: "w",
    ADMIN_TOKEN: "admin",
    APP_TOKEN: APP,
    AI_TASK_TOKEN: AI,
    TIME_ZONE: TZ,
  };
});

function call(method: string, path: string, token: string, body?: unknown): Promise<Response> {
  return handleRequest(
    new Request(`${BASE}${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}` },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    env,
    NOW,
  );
}

const estimate = {
  kcal: 850,
  proteinG: 30,
  fatG: 35,
  carbsG: 100,
  caffeineMg: 0,
  alcoholDrinks: 0,
  greasiness: 3,
  spiciness: 1,
  volume: 3,
  confidence: "medium",
  items: [{ name: "豚骨ラーメン", kcal: 650 }, { name: "餃子（6個）", kcal: 200 }],
};

async function recordMeals() {
  await call("POST", "/api/logs/batch", APP, {
    logs: [
      { id: "M-1", kind: "meal", occurredAt: localTimeOn("2026-11-20", "20:30", TZ), data: { text: "豚骨ラーメン、餃子", amount: "large", meal: "dinner" } },
      { id: "M-2", kind: "meal", occurredAt: localTimeOn("2026-11-20", "12:10", TZ), data: { text: "親子丼" } },
      { id: "M-OLD", kind: "meal", occurredAt: localTimeOn("2026-10-01", "12:00", TZ), data: { text: "古い記録" } },
      { id: "B-1", kind: "bath", occurredAt: localTimeOn("2026-11-20", "21:40", TZ) },
    ],
  });
  db.sqlite.prepare(`DELETE FROM day_queue`).run();
}

describe("/ai/*（Claude Code のタスク）", () => {
  it("AI_TASK_TOKEN でしか使えない（アプリの合言葉でも使えない）", async () => {
    expect((await call("GET", "/ai/meals/pending", APP)).status).toBe(401);
    expect((await call("GET", "/ai/meals/pending", "admin")).status).toBe(401);
  });

  it("目安がまだの食事を、古い順に、料理名・量・日本時間の時刻だけで返す（30日より古いものは除く）", async () => {
    await recordMeals();
    const response = await call("GET", "/ai/meals/pending", AI);
    expect(await response.json()).toEqual({
      ok: true,
      meals: [
        { id: "M-2", text: "親子丼", amount: null, meal: null, eatenAt: "2026-11-20 12:10" },
        { id: "M-1", text: "豚骨ラーメン、餃子", amount: "large", meal: "dinner", eatenAt: "2026-11-20 20:30" },
      ],
    });
  });

  it("目安をまとめて書き込むと、その食事は「目安がまだ」から外れ、日の計算待ちに入る", async () => {
    await recordMeals();
    const response = await call("POST", "/ai/meals/estimates", AI, {
      estimates: [
        { id: "M-1", model: "claude-opus-5-5", estimate },
        { id: "NOPE", model: "claude-opus-5-5", estimate },
        { id: "B-1", model: "claude-opus-5-5", estimate },
      ],
    });
    expect(await response.json()).toEqual({ ok: true, saved: ["M-1"], unknown: ["NOPE", "B-1"] });

    const pending = (await (await call("GET", "/ai/meals/pending", AI)).json()) as { meals: { id: string }[] };
    expect(pending.meals.map((meal) => meal.id)).toEqual(["M-2"]);
    expect(db.sqlite.prepare(`SELECT date FROM day_queue ORDER BY date`).all()).toEqual([{ date: "2026-11-20" }, { date: "2026-11-21" }]);
    const stored = db.sqlite.prepare(`SELECT source, model, estimate_json FROM meal_estimates`).get() as { source: string; model: string; estimate_json: string };
    expect(stored.source).toBe("ai");
    expect(stored.model).toBe("claude-opus-5-5");
    expect(JSON.parse(stored.estimate_json)).toEqual(estimate);
  });

  it("1件ずつでも書き込める。存在しない食事は 404", async () => {
    await recordMeals();
    expect((await call("POST", "/ai/meals/M-2/estimate", AI, { model: "m", estimate: { ...estimate, kcal: 700 } })).status).toBe(200);
    expect((await call("POST", "/ai/meals/NOPE/estimate", AI, { model: "m", estimate })).status).toBe(404);
  });

  it("値の範囲がおかしい目安は 400 で、保存しない", async () => {
    await recordMeals();
    const response = await call("POST", "/ai/meals/estimates", AI, {
      estimates: [{ id: "M-1", estimate: { ...estimate, kcal: 99999, greasiness: 5, confidence: "sure" } }],
    });
    expect(response.status).toBe(400);
    const body = (await response.json()) as { details: string[] };
    expect(body.details).toHaveLength(3);
    expect(db.sqlite.prepare(`SELECT COUNT(*) AS n FROM meal_estimates`).get()).toEqual({ n: 0 });
  });
});

describe("/api/meals（アプリ）", () => {
  it("食事の一覧を、目安と一緒に新しい順で返す", async () => {
    await recordMeals();
    await call("POST", "/ai/meals/estimates", AI, { estimates: [{ id: "M-1", model: "m", estimate }] });
    const response = await call("GET", "/api/meals?from=2026-11-20&to=2026-11-20", APP);
    const body = (await response.json()) as { meals: { id: string; estimateSource: string | null; estimate: { kcal: number } | null }[] };
    expect(body.meals.map((meal) => [meal.id, meal.estimateSource, meal.estimate?.kcal ?? null])).toEqual([
      ["M-1", "ai", 850],
      ["M-2", null, null],
    ]);
  });

  it("利用者が直した目安は、生成AI の目安より優先される", async () => {
    await recordMeals();
    await call("POST", "/ai/meals/estimates", AI, { estimates: [{ id: "M-1", model: "m", estimate }] });
    const response = await call("PUT", "/api/meals/M-1/estimate", APP, { estimate: { ...estimate, kcal: 1100, volume: 3 } });
    expect(response.status).toBe(200);
    const body = (await (await call("GET", "/api/meals?from=2026-11-20&to=2026-11-20", APP)).json()) as { meals: { id: string; estimateSource: string; estimate: { kcal: number } }[] };
    const meal = body.meals.find((item) => item.id === "M-1");
    expect(meal?.estimateSource).toBe("user");
    expect(meal?.estimate.kcal).toBe(1100);
  });

  it("食事の内容を直したら、目安は消えて、生成AI がまた推定する", async () => {
    await recordMeals();
    await call("POST", "/ai/meals/estimates", AI, { estimates: [{ id: "M-1", model: "m", estimate }] });
    await call("POST", "/api/logs/batch", APP, {
      logs: [{ id: "M-1", kind: "meal", occurredAt: localTimeOn("2026-11-20", "20:30", TZ), data: { text: "醤油ラーメン" } }],
    });
    const pending = (await (await call("GET", "/ai/meals/pending", AI)).json()) as { meals: { id: string; text: string }[] };
    expect(pending.meals.find((meal) => meal.id === "M-1")?.text).toBe("醤油ラーメン");
  });
});
