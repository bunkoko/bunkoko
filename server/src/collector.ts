// 5分ごとに SwitchBot の全機器の状態を取得して保存する

import {
  getLastDeviceSync,
  getPollableDevices,
  insertCollectorRun,
  insertReadings,
  syncDevices,
  type CollectorError,
  type DeviceReadings,
} from "./db";
import { toReadings } from "./normalize";
import type { SwitchBotClient } from "./switchbot";

/** 機器一覧を取り直す間隔。機器の追加・削除に追いつくため、1時間に1回だけ取る */
export const DEVICE_LIST_REFRESH_MS = 60 * 60 * 1000;

/** 状態を返さない機器（ハブの中継専用モデルなど）。取得しても API の回数を使うだけなので飛ばす */
const TYPES_WITHOUT_STATUS = new Set(["Hub", "Hub Plus", "Hub Mini", "Remote"]);

export interface CollectorDeps {
  db: D1Database;
  client: Pick<SwitchBotClient, "listDevices" | "getStatus">;
  userId: string;
  now?: () => number;
}

export interface CollectionResult {
  startedAt: number;
  deviceListRefreshed: boolean;
  devicesTotal: number;
  devicesOk: number;
  readingsWritten: number;
  errors: CollectorError[];
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export async function collect(
  deps: CollectorDeps,
  options: { forceDeviceListRefresh?: boolean } = {},
): Promise<CollectionResult> {
  const now = deps.now ?? (() => Date.now());
  const { db, client, userId } = deps;
  const startedAt = now();
  const errors: CollectorError[] = [];

  // 1. 必要なときだけ機器一覧を取り直す
  let deviceListRefreshed = false;
  const lastSync = await getLastDeviceSync(db, userId);
  if (
    options.forceDeviceListRefresh ||
    lastSync === null ||
    startedAt - lastSync >= DEVICE_LIST_REFRESH_MS
  ) {
    try {
      const { deviceList } = await client.listDevices();
      await syncDevices(db, userId, deviceList, startedAt);
      deviceListRefreshed = true;
    } catch (error) {
      // 一覧が取れなくても、前回までに分かっている機器の値は取りに行く
      errors.push({ deviceId: null, message: `機器一覧の取得に失敗: ${errorMessage(error)}` });
    }
  }

  // 2. 全機器の状態を並行して取得する
  const devices = (await getPollableDevices(db, userId)).filter(
    (device) => !TYPES_WITHOUT_STATUS.has(device.deviceType ?? ""),
  );
  const results = await Promise.allSettled(devices.map((device) => client.getStatus(device.deviceId)));

  const items: DeviceReadings[] = [];
  let devicesOk = 0;
  results.forEach((result, index) => {
    const device = devices[index];
    if (result.status === "fulfilled") {
      devicesOk += 1;
      items.push({ deviceId: device.deviceId, measuredAt: startedAt, readings: toReadings(result.value) });
    } else {
      errors.push({ deviceId: device.deviceId, message: errorMessage(result.reason) });
    }
  });

  // 3. まとめて保存し、実行記録を残す
  const readingsWritten = await insertReadings(db, userId, "poll", items);
  const result: CollectionResult = {
    startedAt,
    deviceListRefreshed,
    devicesTotal: devices.length,
    devicesOk,
    readingsWritten,
    errors,
  };
  await insertCollectorRun(db, { userId, finishedAt: now(), ...result });
  return result;
}
