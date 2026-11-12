import { beforeEach, describe, expect, it } from "vitest";
import { DEVICE_LIST_REFRESH_MS, collect } from "../src/collector";
import { SwitchBotApiError, type DeviceListBody, type DeviceStatus, type SwitchBotDevice } from "../src/switchbot";
import { createFakeD1, type FakeD1 } from "./helpers/fake-d1";
import { DEVICES, HUB_ID, STATUSES } from "./helpers/fixtures";

const T0 = Date.parse("2026-11-20T13:00:00Z");
const FIVE_MIN = 5 * 60 * 1000;

class FakeClient {
  devices: SwitchBotDevice[] = DEVICES;
  offline = new Set<string>();
  listFails = false;
  listCalls = 0;
  statusCalls: string[] = [];

  async listDevices(): Promise<DeviceListBody> {
    this.listCalls += 1;
    if (this.listFails) throw new SwitchBotApiError("HTTP 500: error", 500, null);
    return { deviceList: this.devices, infraredRemoteList: [{ deviceId: "02-IR", remoteType: "Air Conditioner" }] };
  }

  async getStatus(deviceId: string): Promise<DeviceStatus> {
    this.statusCalls.push(deviceId);
    if (this.offline.has(deviceId)) throw new SwitchBotApiError("statusCode 161: device offline", 200, 161);
    const status = STATUSES[deviceId];
    if (!status) throw new SwitchBotApiError("statusCode 152: device not found", 200, 152);
    return status;
  }
}

describe("collect", () => {
  let db: FakeD1;
  let client: FakeClient;
  let clock: number;
  const run = (options?: { forceDeviceListRefresh?: boolean }) =>
    collect({ db: db.d1, client, userId: "me", now: () => clock }, options);

  beforeEach(() => {
    db = createFakeD1();
    client = new FakeClient();
    clock = T0;
  });

  it("初回は機器一覧を取得し、全機器の値を保存する", async () => {
    const result = await run();

    expect(result.deviceListRefreshed).toBe(true);
    expect(result.devicesTotal).toBe(9);
    expect(result.devicesOk).toBe(9);
    expect(result.errors).toEqual([]);
    expect(client.listCalls).toBe(1);

    const co2 = db.sqlite
      .prepare(`SELECT value_num FROM readings WHERE device_id = 'C1C1C1C1C1C1' AND metric = 'CO2'`)
      .get();
    expect(co2).toEqual({ value_num: 1203 });

    const presence = db.sqlite
      .prepare(`SELECT value_num, value_text, measured_at, source FROM readings WHERE device_id = 'P1P1P1P1P1P1' AND metric = 'Detected'`)
      .get();
    expect(presence).toEqual({ value_num: 1, value_text: "true", measured_at: T0, source: "poll" });

    // 機器の情報（deviceId・version など）は値として保存しない
    const metadata = db.sqlite
      .prepare(`SELECT COUNT(*) AS n FROM readings WHERE metric IN ('deviceId', 'deviceType', 'hubDeviceId', 'version')`)
      .get();
    expect(metadata).toEqual({ n: 0 });

    const stored = db.sqlite.prepare(`SELECT COUNT(*) AS n FROM readings`).get() as { n: number };
    expect(result.readingsWritten).toBe(stored.n);

    const runs = db.sqlite.prepare(`SELECT devices_ok, devices_total, device_list_refreshed FROM collector_runs`).all();
    expect(runs).toEqual([{ devices_ok: 9, devices_total: 9, device_list_refreshed: 1 }]);
  });

  it("1時間以内は機器一覧を取り直さない（API の回数を節約する）", async () => {
    await run();
    clock += FIVE_MIN;
    const second = await run();

    expect(second.deviceListRefreshed).toBe(false);
    expect(client.listCalls).toBe(1);
    expect(client.statusCalls).toHaveLength(18);

    const times = db.sqlite
      .prepare(`SELECT DISTINCT measured_at FROM readings WHERE device_id = ?1 ORDER BY measured_at`)
      .all(HUB_ID);
    expect(times).toEqual([{ measured_at: T0 }, { measured_at: T0 + FIVE_MIN }]);
  });

  it("1時間たつと一覧を取り直し、アカウントから外した機器は取得しなくなる", async () => {
    await run();
    client.devices = DEVICES.filter((device) => device.deviceId !== "G2G2G2G2G2G2");
    clock += DEVICE_LIST_REFRESH_MS;
    client.statusCalls = [];

    const result = await run();
    expect(result.deviceListRefreshed).toBe(true);
    expect(result.devicesTotal).toBe(8);
    expect(client.statusCalls).not.toContain("G2G2G2G2G2G2");

    const removed = db.sqlite.prepare(`SELECT active FROM devices WHERE device_id = 'G2G2G2G2G2G2'`).get();
    expect(removed).toEqual({ active: 0 });
  });

  it("オフラインの機器があっても、ほかの機器の値は保存し、エラーを記録する", async () => {
    client.offline.add("W1W1W1W1W1W1");
    const result = await run();

    expect(result.devicesOk).toBe(8);
    expect(result.errors).toEqual([{ deviceId: "W1W1W1W1W1W1", message: "statusCode 161: device offline" }]);
    const errors = db.sqlite.prepare(`SELECT errors_json FROM collector_runs`).get() as { errors_json: string };
    expect(JSON.parse(errors.errors_json)).toHaveLength(1);
  });

  it("機器一覧が取れなくても止まらず、エラーを記録する", async () => {
    client.listFails = true;
    const result = await run();

    expect(result.devicesTotal).toBe(0);
    expect(result.errors[0].message).toContain("機器一覧の取得に失敗");
    expect(db.sqlite.prepare(`SELECT COUNT(*) AS n FROM collector_runs`).get()).toEqual({ n: 1 });
  });

  it("クラウドサービスが無効な機器と、状態を返さないハブは取得しない", async () => {
    client.devices = [
      ...DEVICES,
      { deviceId: "M1M1M1M1M1M1", deviceName: "ハブミニ", deviceType: "Hub Mini", enableCloudService: true },
      { deviceId: "X1X1X1X1X1X1", deviceName: "温湿度計", deviceType: "Meter", enableCloudService: false },
    ];
    const result = await run();

    expect(result.devicesTotal).toBe(9);
    expect(client.statusCalls).not.toContain("M1M1M1M1M1M1");
    expect(client.statusCalls).not.toContain("X1X1X1X1X1X1");
  });

  it("同じ時刻に2回保存しても重複しない", async () => {
    await run();
    const again = await run({ forceDeviceListRefresh: true });
    expect(again.readingsWritten).toBe(0);
  });
});
