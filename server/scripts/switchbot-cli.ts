// SwitchBot API を手元の Mac から試すためのコマンド
//
//   npm run switchbot -- devices                 機器の一覧と、このツールでの役割（トークンが正しいかの確認にも使う）
//   npm run switchbot -- devices --json          機器の一覧を、SwitchBot API が返したまま出す
//   npm run switchbot -- status <deviceId>       1台の機器の今の状態
//   npm run switchbot -- webhook-setup [url]     Webhook の送り先を登録する。url を省くと、.dev.vars の
//                                                SERVER_URL と WEBHOOK_SECRET から送り先を作る（合言葉を画面に出さない）
//   npm run switchbot -- webhook-query           登録済みの Webhook の送り先を見る
//   npm run switchbot -- webhook-delete <url>    Webhook の送り先を消す
//
// トークンとシークレットは、環境変数 SWITCHBOT_TOKEN / SWITCHBOT_SECRET か、
// server/.dev.vars ファイルから読み込む。出力に合言葉がまぎれていたら、名前に置き換えて出す。

import { pathToFileURL } from "node:url";
import { SwitchBotClient } from "../src/switchbot";
import { maskSecrets, roleLabel } from "./admin";
import { loadDevVars } from "./dev-vars";

/** Webhook の送り先。サーバーの URL だけを渡されたら、合言葉の入った受け口のパスを足す */
export function webhookUrl(arg: string | undefined, serverUrl: string | undefined, webhookSecret: string | undefined): string | null {
  const base = (arg ?? serverUrl ?? "").replace(/\/+$/, "");
  if (!base) return null;
  if (base.includes("/webhooks/")) return base;
  return webhookSecret ? `${base}/webhooks/switchbot/${webhookSecret}` : null;
}

const usage = `使い方:
  npm run switchbot -- devices
  npm run switchbot -- status <deviceId>
  npm run switchbot -- webhook-setup [url]
  npm run switchbot -- webhook-query
  npm run switchbot -- webhook-delete <url>`;

async function main(): Promise<void> {
  const [command, arg] = process.argv.slice(2);
  const devVars = loadDevVars();
  const token = process.env.SWITCHBOT_TOKEN ?? devVars.SWITCHBOT_TOKEN ?? "";
  const secret = process.env.SWITCHBOT_SECRET ?? devVars.SWITCHBOT_SECRET ?? "";
  if (!token || !secret) {
    console.error("SWITCHBOT_TOKEN と SWITCHBOT_SECRET を server/.dev.vars に書いてください（README 参照）");
    process.exit(1);
  }
  // テスト用: 環境変数 SWITCHBOT_API_BASE で問い合わせ先を変えられる（通常は設定しない）
  const client = new SwitchBotClient({ token, secret, baseUrl: process.env.SWITCHBOT_API_BASE });

  const secrets = { SWITCHBOT_TOKEN: token, SWITCHBOT_SECRET: secret, WEBHOOK_SECRET: devVars.WEBHOOK_SECRET };
  let result: unknown;
  switch (command) {
    case "devices": {
      const list = await client.listDevices();
      if (arg === "--json") {
        result = list;
        break;
      }
      console.log(`機器（${list.deviceList.length}台）: 名前（種類）→ このツールでの役割`);
      for (const device of list.deviceList) {
        const role = roleLabel(String(device.deviceType ?? ""), device.deviceName ?? null);
        const cloud = device.enableCloudService === false ? "  ※クラウドサービスがオフ（SwitchBot アプリでオンにする）" : "";
        console.log(`  ${device.deviceName ?? "(名前なし)"}（${device.deviceType ?? "?"}）→ ${role}${cloud}`);
      }
      if (list.infraredRemoteList.length > 0) {
        console.log(`赤外線リモコン（${list.infraredRemoteList.length}台）:`);
        for (const remote of list.infraredRemoteList) console.log(`  ${String(remote.deviceName ?? "")}（${String(remote.remoteType ?? "")}）`);
      }
      return;
    }
    case "status":
      if (!arg) throw new Error(usage);
      result = await client.getStatus(arg);
      break;
    case "webhook-setup": {
      const url = webhookUrl(arg, process.env.SERVER_URL ?? devVars.SERVER_URL, devVars.WEBHOOK_SECRET);
      if (!url) throw new Error(`送り先を作れませんでした。.dev.vars に SERVER_URL と WEBHOOK_SECRET があるか確かめてください\n${usage}`);
      result = await client.setupWebhook(url);
      console.log(`送り先: ${maskSecrets(url, secrets)}`);
      break;
    }
    case "webhook-query":
      result = await client.queryWebhookUrls();
      break;
    case "webhook-delete":
      if (!arg) throw new Error(usage);
      result = await client.deleteWebhook(arg);
      break;
    default:
      console.log(usage);
      return;
  }
  console.log(maskSecrets(JSON.stringify(result, null, 2), secrets));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error: unknown) => {
    const vars = loadDevVars();
    const message = error instanceof Error ? error.message : String(error);
    console.error(maskSecrets(message, { SWITCHBOT_TOKEN: vars.SWITCHBOT_TOKEN, SWITCHBOT_SECRET: vars.SWITCHBOT_SECRET, WEBHOOK_SECRET: vars.WEBHOOK_SECRET }));
    process.exit(1);
  });
}
