/**
 * 合言葉（WEBHOOK_SECRET・ADMIN_TOKEN・APP_TOKEN・AI_TASK_TOKEN）を作って server/.dev.vars に保存し、
 * .dev.vars にある値をすべて Cloudflare に登録する（wrangler secret bulk）。
 *
 *   npm run secrets                 # 足りない合言葉を作って、登録する（何度実行してもよい。作った合言葉は変わらない）
 *   npm run secrets -- --dry-run    # .dev.vars を作るだけで、Cloudflare には登録しない
 *   npm run secrets -- --copy APP_TOKEN   # 値をクリップボードにコピーする（画面には出さない）
 *
 * SwitchBot のトークンとシークレットは、.dev.vars に書き込んでからもう一度実行すると登録される。
 */
import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { chmodSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { loadDevVars } from "./dev-vars";

const serverDir = resolve(import.meta.dirname, "..");
const devVarsPath = resolve(serverDir, ".dev.vars");
const examplePath = resolve(serverDir, ".dev.vars.example");

/** 自動で作る合言葉 */
const GENERATED = ["WEBHOOK_SECRET", "ADMIN_TOKEN", "APP_TOKEN", "AI_TASK_TOKEN"] as const;

function newToken(): string {
  return randomBytes(24).toString("hex");
}

/** .dev.vars.example の並びとコメントを残したまま、値を埋めた中身を作る */
export function renderDevVars(template: string, values: Record<string, string>): string {
  const seen = new Set<string>();
  const body = template.replace(/\s+$/, "");
  const lines = (body ? body.split(/\r?\n/) : []).map((line) => {
    const match = /^\s*([A-Za-z0-9_]+)\s*=/.exec(line);
    if (!match || line.trimStart().startsWith("#")) return line;
    seen.add(match[1]);
    return `${match[1]}=${values[match[1]] ?? ""}`;
  });
  for (const [key, value] of Object.entries(values)) {
    if (!seen.has(key)) lines.push(`${key}=${value}`);
  }
  return lines.join("\n").replace(/\n*$/, "\n");
}

/** 空の合言葉だけを作る。すでにある値は変えない */
export function fillMissing(values: Record<string, string>, make: () => string = newToken): { values: Record<string, string>; created: string[] } {
  const next = { ...values };
  const created: string[] = [];
  for (const key of GENERATED) {
    if (!next[key]) {
      next[key] = make();
      created.push(key);
    }
  }
  return { values: next, created };
}

function main(): void {
  const args = process.argv.slice(2);
  const copyIndex = args.indexOf("--copy");
  if (copyIndex >= 0) {
    const key = args[copyIndex + 1];
    const value = key ? loadDevVars(devVarsPath)[key] : undefined;
    if (!value) {
      console.error(`${key ?? "(名前なし)"} が .dev.vars にありません。先に npm run secrets を実行してください`);
      process.exit(1);
    }
    const copied = spawnSync("pbcopy", { input: value });
    if (copied.error || copied.status !== 0) {
      console.error("クリップボードにコピーできませんでした（pbcopy は Mac のコマンドです）");
      process.exit(1);
    }
    console.log(`${key} をクリップボードにコピーしました（画面には出していません）`);
    return;
  }

  const dryRun = args.includes("--dry-run");
  const template = existsSync(examplePath) ? readFileSync(examplePath, "utf8") : "";
  const { values, created } = fillMissing(loadDevVars(devVarsPath));
  writeFileSync(devVarsPath, renderDevVars(template, values));
  chmodSync(devVarsPath, 0o600);
  console.log(created.length > 0 ? `新しく作った合言葉: ${created.join(", ")}` : "合言葉はすでにあります（変えていません）");
  console.log(`保存した場所: ${devVarsPath}（Git には含まれません。このファイルは消さないでください）`);

  const upload = Object.fromEntries(Object.entries(values).filter(([, value]) => value !== ""));
  console.log(`Cloudflare に登録する値: ${Object.keys(upload).join(", ")}`);
  if (dryRun) {
    console.log("--dry-run なので、Cloudflare には登録していません");
    return;
  }
  const result = spawnSync("npx", ["wrangler", "secret", "bulk"], {
    cwd: serverDir,
    input: JSON.stringify(upload),
    stdio: ["pipe", "inherit", "inherit"],
  });
  if (result.status !== 0) {
    console.error("登録に失敗しました。上に出たエラーを確かめてください（npx wrangler login と npm run deploy は済んでいますか）");
    process.exit(1);
  }
  console.log("登録しました");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
