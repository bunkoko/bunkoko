// 機器の役割（docs/phase1-design.md の 4.2）。
// 基本は機器の種類と名前から自動で決め、設定（deviceRoles）があればそちらを優先する。

import type { DeviceRole } from "./settings";

export type RoleMap = Record<Exclude<DeviceRole, "ignore">, string[]>;

const emptyRoles = (): RoleMap => ({
  co2: [],
  bedPresence: [],
  window: [],
  door: [],
  ceilingLight: [],
  curtain: [],
  purifier: [],
  tv: [],
  hub: [],
});

export function inferRole(deviceType: string | null, deviceName: string | null): DeviceRole | null {
  const type = deviceType ?? "";
  const name = (deviceName ?? "").toLowerCase();
  if (type === "MeterPro(CO2)") return "co2";
  if (type === "Presence Sensor") return "bedPresence";
  if (type === "Hub 3") return "hub";
  if (/^Ceiling Light/.test(type)) return "ceilingLight";
  if (/^Curtain/.test(type)) return "curtain";
  if (type === "Contact Sensor") {
    if (/窓|まど|window/.test(name)) return "window";
    if (/ドア|扉|玄関|door/.test(name)) return "door";
    return null;
  }
  if (/^Plug Mini/.test(type)) {
    if (/加湿|空気清浄|清浄機|purifier|humidifier/.test(name)) return "purifier";
    if (/テレビ|tv/.test(name)) return "tv";
    return null;
  }
  return null;
}

export async function loadRoles(
  db: D1Database,
  userId: string,
  overrides: Record<string, DeviceRole>,
): Promise<RoleMap> {
  const { results } = await db
    .prepare(`SELECT device_id, device_type, device_name FROM devices WHERE user_id = ?1 AND active = 1`)
    .bind(userId)
    .all<{ device_id: string; device_type: string | null; device_name: string | null }>();
  const roles = emptyRoles();
  for (const device of results) {
    const role = overrides[device.device_id] ?? inferRole(device.device_type, device.device_name);
    if (role && role !== "ignore") roles[role].push(device.device_id);
  }
  return roles;
}
