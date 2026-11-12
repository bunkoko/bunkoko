// SwitchBot API を手元の Mac から試すためのコマンド
//
//   npm run switchbot -- devices                 機器の一覧（トークンが正しいかの確認にも使う）
//   npm run switchbot -- status <deviceId>       1台の機器の今の状態
//   npm run switchbot -- webhook-setup <url>     Webhook の送り先を登録する
//   npm run switchbot -- webhook-query           登録済みの Webhook の送り先を見る
//   npm run switchbot -- webhook-delete <url>    Webhook の送り先を消す
//
// トークンとシークレットは、環境変数 SWITCHBOT_TOKEN / SWITCHBOT_SECRET か、
// server/.dev.vars ファイルから読み込む。

import { SwitchBotClient } from "../src/switchbot";
import { loadDevVars } from "./dev-vars";

const usage = `使い方:
  npm run switchbot -- devices
  npm run switchbot -- status <deviceId>
  npm run switchbot -- webhook-setup <url>
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

  let result: unknown;
  switch (command) {
    case "devices":
      result = await client.listDevices();
      break;
    case "status":
      if (!arg) throw new Error(usage);
      result = await client.getStatus(arg);
      break;
    case "webhook-setup":
      if (!arg) throw new Error(usage);
      result = await client.setupWebhook(arg);
      break;
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
  console.log(JSON.stringify(result, null, 2));
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
