import { beforeEach, describe, expect, it, vi } from "vitest";
import { runScheduled, type Env } from "../src/index";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";
import { D, at, resetSampleIds, seedNight, useDb } from "./helpers/night";

let db: FakeD1;

beforeEach(() => {
  db = createFakeD1();
  useDb(db);
  resetSampleIds();
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});

function baseEnv(extra: Partial<Env> = {}): Env {
  return { DB: db.d1, WEBHOOK_SECRET: "hook", ADMIN_TOKEN: "admin", APP_TOKEN: "app", AI_TASK_TOKEN: "ai", TIME_ZONE: "Asia/Tokyo", ...extra } as Env;
}

const count = (sql: string) => (db.sqlite.prepare(sql).get() as { n: number }).n;

describe("runScheduled", () => {
  it("SwitchBot のトークンがなくても、1日 = 1行の計算はする", async () => {
    seedNight();
    db.sqlite.prepare(`INSERT INTO day_queue (user_id, date, queued_at) VALUES ('me', ?, ?)`).run(D, 0);

    await runScheduled(baseEnv(), at(D, "09:00"));

    expect(count(`SELECT COUNT(*) AS n FROM days WHERE date = '${D}'`)).toBe(1);
    expect(count(`SELECT COUNT(*) AS n FROM collector_runs`)).toBe(0);
  });

  it("SwitchBot の取得に失敗しても、計算は続ける", async () => {
    seedNight();
    db.sqlite.prepare(`INSERT INTO day_queue (user_id, date, queued_at) VALUES ('me', ?, ?)`).run(D, 0);
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network down"); }));
    try {
      await runScheduled(baseEnv({ SWITCHBOT_TOKEN: "t", SWITCHBOT_SECRET: "s" }), at(D, "09:00"));
    } finally {
      vi.unstubAllGlobals();
    }

    expect(count(`SELECT COUNT(*) AS n FROM days WHERE date = '${D}'`)).toBe(1);
    expect(count(`SELECT COUNT(*) AS n FROM collector_runs`)).toBe(1);
  });
});
