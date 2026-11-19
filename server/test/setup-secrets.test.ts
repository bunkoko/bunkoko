import { describe, expect, it } from "vitest";
import { fillMissing, renderDevVars } from "../scripts/setup-secrets";

describe("setup-secrets", () => {
  it("creates only the missing tokens and keeps existing values", () => {
    let n = 0;
    const { values, created } = fillMissing({ ADMIN_TOKEN: "keep", SWITCHBOT_TOKEN: "sb" }, () => `t${++n}`);
    expect(created).toEqual(["WEBHOOK_SECRET", "APP_TOKEN", "AI_TASK_TOKEN"]);
    expect(values).toEqual({
      ADMIN_TOKEN: "keep",
      SWITCHBOT_TOKEN: "sb",
      WEBHOOK_SECRET: "t1",
      APP_TOKEN: "t2",
      AI_TASK_TOKEN: "t3",
    });
    expect(fillMissing(values).created).toEqual([]);
  });

  it("keeps the comments and order of the example file", () => {
    const template = "# comment\nSWITCHBOT_TOKEN=\nAPP_TOKEN=\n";
    expect(renderDevVars(template, { APP_TOKEN: "a", SWITCHBOT_TOKEN: "", EXTRA: "x" })).toBe(
      "# comment\nSWITCHBOT_TOKEN=\nAPP_TOKEN=a\nEXTRA=x\n",
    );
  });

  it("generates 48 hex characters", () => {
    const { values } = fillMissing({});
    expect(values.APP_TOKEN).toMatch(/^[0-9a-f]{48}$/);
  });
});
