import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

/** server/.dev.vars（KEY=VALUE 形式）を読み込む */
export function loadDevVars(path = resolve(import.meta.dirname, "..", ".dev.vars")): Record<string, string> {
  if (!existsSync(path)) return {};
  const vars: Record<string, string> = {};
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    if (line.trimStart().startsWith("#")) continue;
    const match = /^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$/.exec(line);
    if (match) vars[match[1]] = match[2].replace(/^(["'])(.*)\1$/, "$2");
  }
  return vars;
}
