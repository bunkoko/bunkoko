// テスト用: 購入リストの機器をまねた SwitchBot の応答（項目名は公式の API 資料に合わせている）

import type { DeviceStatus, SwitchBotDevice } from "../../src/switchbot";

export const HUB_ID = "B0E9FE582974";

export const DEVICES: SwitchBotDevice[] = [
  { deviceId: HUB_ID, deviceName: "ハブ3", deviceType: "Hub 3", enableCloudService: true, hubDeviceId: HUB_ID },
  { deviceId: "C1C1C1C1C1C1", deviceName: "CO2センサー", deviceType: "MeterPro(CO2)", enableCloudService: true, hubDeviceId: HUB_ID },
  { deviceId: "P1P1P1P1P1P1", deviceName: "人感センサーPro", deviceType: "Presence Sensor", enableCloudService: true, hubDeviceId: HUB_ID },
  { deviceId: "W1W1W1W1W1W1", deviceName: "窓", deviceType: "Contact Sensor", enableCloudService: true, hubDeviceId: HUB_ID },
  { deviceId: "D1D1D1D1D1D1", deviceName: "ドア", deviceType: "Contact Sensor", enableCloudService: true, hubDeviceId: HUB_ID },
  { deviceId: "L1L1L1L1L1L1", deviceName: "シーリングライト", deviceType: "Ceiling Light", enableCloudService: true, hubDeviceId: "000000000000" },
  { deviceId: "K1K1K1K1K1K1", deviceName: "カーテン", deviceType: "Curtain3", enableCloudService: true, hubDeviceId: HUB_ID },
  { deviceId: "G1G1G1G1G1G1", deviceName: "加湿空気清浄機", deviceType: "Plug Mini (JP)", enableCloudService: true, hubDeviceId: "000000000000" },
  { deviceId: "G2G2G2G2G2G2", deviceName: "テレビ", deviceType: "Plug Mini (JP)", enableCloudService: true, hubDeviceId: "000000000000" },
];

export const STATUSES: Record<string, DeviceStatus> = {
  [HUB_ID]: { deviceId: HUB_ID, deviceType: "Hub 3", hubDeviceId: HUB_ID, version: "V1.0", temperature: 22.4, humidity: 48, lightLevel: 3, moveDetected: false, onlineStatus: "online" },
  C1C1C1C1C1C1: { deviceId: "C1C1C1C1C1C1", deviceType: "MeterPro(CO2)", hubDeviceId: HUB_ID, battery: 90, version: "V1.2", temperature: 22.1, humidity: 50, CO2: 1203 },
  P1P1P1P1P1P1: { deviceId: "P1P1P1P1P1P1", deviceType: "Presence Sensor", hubDeviceId: HUB_ID, battery: 100, lightLevel: 2, Detected: true },
  W1W1W1W1W1W1: { deviceId: "W1W1W1W1W1W1", deviceType: "Contact Sensor", hubDeviceId: HUB_ID, battery: 100, moveDetected: false, openState: "close", brightness: "dim" },
  D1D1D1D1D1D1: { deviceId: "D1D1D1D1D1D1", deviceType: "Contact Sensor", hubDeviceId: HUB_ID, battery: 60, moveDetected: false, openState: "close", brightness: "dim" },
  L1L1L1L1L1L1: { deviceId: "L1L1L1L1L1L1", deviceType: "Ceiling Light", hubDeviceId: "000000000000", power: "on", brightness: 40, colorTemperature: 3000, onlineStatus: "online" },
  K1K1K1K1K1K1: { deviceId: "K1K1K1K1K1K1", deviceType: "Curtain3", hubDeviceId: HUB_ID, calibrate: true, group: false, moving: false, battery: 80, slidePosition: "100" },
  G1G1G1G1G1G1: { deviceId: "G1G1G1G1G1G1", deviceType: "Plug Mini (JP)", voltage: 100.6, weight: 18.2, electricityOfDay: 312, electricCurrent: 190.5 },
  G2G2G2G2G2G2: { deviceId: "G2G2G2G2G2G2", deviceType: "Plug Mini (JP)", voltage: 100.4, weight: 0, electricityOfDay: 0, electricCurrent: 0 },
};
