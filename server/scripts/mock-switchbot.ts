// 機器が届く前に動作を試すための、偽の SwitchBot API サーバー
//
//   npm run mock:switchbot
//
// 購入リストの9台の機器があるふりをして、少しずつ変わる値を返す。
// 本物と同じように署名を確かめるので、署名の作り方が間違っていれば 401 を返す。
// .dev.vars に SWITCHBOT_API_BASE=http://127.0.0.1:8788 と書くと、wrangler dev がこちらを使う。

import { createHmac } from "node:crypto";
import { createServer } from "node:http";
import { DEVICES, STATUSES } from "../test/helpers/fixtures";
import { loadDevVars } from "./dev-vars";

const vars = loadDevVars();
const token = process.env.SWITCHBOT_TOKEN ?? vars.SWITCHBOT_TOKEN ?? "";
const secret = process.env.SWITCHBOT_SECRET ?? vars.SWITCHBOT_SECRET ?? "";
const port = Number(process.env.PORT ?? 8788);

if (!token || !secret) {
  console.error("SWITCHBOT_TOKEN と SWITCHBOT_SECRET を .dev.vars に書いてください（偽の値で構いません）");
  process.exit(1);
}

let co2 = 650;
let calls = 0;

function currentStatus(deviceId: string) {
  const status = STATUSES[deviceId];
  if (!status) return null;
  if (status.deviceType === "MeterPro(CO2)") {
    // 閉め切った部屋で少しずつ上がる CO2 をまねる
    co2 = Math.min(2500, co2 + Math.round(Math.random() * 30));
    return { ...status, CO2: co2, temperature: Math.round((21 + Math.random()) * 10) / 10 };
  }
  return status;
}

function isSigned(headers: Record<string, string | string[] | undefined>): boolean {
  const t = String(headers.t ?? "");
  const nonce = String(headers.nonce ?? "");
  const expected = createHmac("sha256", secret).update(token + t + nonce).digest("base64");
  return headers.authorization === token && headers.sign === expected;
}

createServer((request, response) => {
  calls += 1;
  const url = new URL(request.url ?? "/", `http://127.0.0.1:${port}`);
  const send = (status: number, body: unknown) => {
    response.writeHead(status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(body));
  };

  if (!isSigned(request.headers)) {
    console.log(`[${calls}] ${request.method} ${url.pathname} → 401（署名が一致しません）`);
    return send(401, { message: "Unauthorized" });
  }

  console.log(`[${calls}] ${request.method} ${url.pathname}`);
  if (url.pathname === "/v1.1/devices") {
    return send(200, { statusCode: 100, message: "success", body: { deviceList: DEVICES, infraredRemoteList: [] } });
  }
  const match = /^\/v1\.1\/devices\/([^/]+)\/status$/.exec(url.pathname);
  if (match) {
    const status = currentStatus(decodeURIComponent(match[1]));
    return status
      ? send(200, { statusCode: 100, message: "success", body: status })
      : send(200, { statusCode: 152, message: "device not found", body: {} });
  }
  return send(404, { message: "not found" });
}).listen(port, "127.0.0.1", () => {
  console.log(`偽の SwitchBot API: http://127.0.0.1:${port}`);
});
