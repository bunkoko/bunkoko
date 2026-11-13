// 利用者の設定（タイムゾーン・目標の就寝と起床・通知・目安の値・機器の役割）

import { isValidHhmm, isValidTimeZone } from "./time";
import { Problems, isRecord, optionalNumber, type Json } from "./validate";

export const DEVICE_ROLES = [
  "co2",
  "bedPresence",
  "window",
  "door",
  "ceilingLight",
  "curtain",
  "purifier",
  "tv",
  "hub",
  "ignore",
] as const;
export type DeviceRole = (typeof DEVICE_ROLES)[number];

export interface UserSettings {
  timeZone: string;
  /** 目標の就寝時刻 "HH:MM" */
  targetBedtime: string;
  /** 目標の起床時刻 "HH:MM" */
  targetWake: string;
  notifications: {
    bedtime: boolean;
    /** 目標の就寝時刻の何分前に、寝る前の確認を送るか */
    bedtimeLeadMinutes: number;
    morningReport: boolean;
  };
  /** 目安の値。室温は季節で変わるので、決めるまでは null（使わない） */
  comfort: {
    co2Max: number;
    humidityMin: number;
    humidityMax: number;
    temperatureMin: number | null;
    temperatureMax: number | null;
  };
  /** 機器の役割を自動の判定から変えたいときだけ書く（deviceId → 役割） */
  deviceRoles: Record<string, DeviceRole>;
}

export function defaultSettings(timeZone: string): UserSettings {
  return {
    timeZone,
    targetBedtime: "23:30",
    targetWake: "07:00",
    notifications: { bedtime: true, bedtimeLeadMinutes: 60, morningReport: true },
    comfort: { co2Max: 1000, humidityMin: 40, humidityMax: 60, temperatureMin: null, temperatureMax: null },
    deviceRoles: {},
  };
}

export async function getSettings(db: D1Database, userId: string, defaultTimeZone: string): Promise<UserSettings> {
  const raw = await db
    .prepare(`SELECT settings_json FROM user_settings WHERE user_id = ?1`)
    .bind(userId)
    .first<string>("settings_json");
  const defaults = defaultSettings(defaultTimeZone);
  if (!raw) return defaults;
  // 保存されている値が古い形でも動くように、足りない項目は既定値で埋める
  const stored = JSON.parse(raw) as Partial<UserSettings>;
  return {
    ...defaults,
    ...stored,
    notifications: { ...defaults.notifications, ...stored.notifications },
    comfort: { ...defaults.comfort, ...stored.comfort },
    deviceRoles: { ...stored.deviceRoles },
  };
}

/** 一部だけの変更を確かめて、今の設定に重ねる */
export function applySettingsPatch(current: UserSettings, patch: unknown, problems: Problems): UserSettings {
  if (!isRecord(patch)) {
    problems.add("settings", "オブジェクトにしてください");
    return current;
  }
  const next: UserSettings = structuredClone(current);

  if (patch.timeZone !== undefined) {
    if (typeof patch.timeZone === "string" && isValidTimeZone(patch.timeZone)) next.timeZone = patch.timeZone;
    else problems.add("timeZone", "IANA のタイムゾーン名（例: Asia/Tokyo）にしてください");
  }
  for (const key of ["targetBedtime", "targetWake"] as const) {
    if (patch[key] === undefined) continue;
    if (typeof patch[key] === "string" && isValidHhmm(patch[key] as string)) next[key] = patch[key] as string;
    else problems.add(key, '"HH:MM" の形にしてください');
  }

  if (patch.notifications !== undefined) {
    if (!isRecord(patch.notifications)) problems.add("notifications", "オブジェクトにしてください");
    else {
      const n = patch.notifications as Json;
      for (const key of ["bedtime", "morningReport"] as const) {
        if (n[key] === undefined) continue;
        if (typeof n[key] === "boolean") next.notifications[key] = n[key] as boolean;
        else problems.add(`notifications.${key}`, "true / false にしてください");
      }
      const lead = optionalNumber(problems, "notifications.bedtimeLeadMinutes", n.bedtimeLeadMinutes, 0, 240);
      if (lead !== null) next.notifications.bedtimeLeadMinutes = Math.round(lead);
    }
  }

  if (patch.comfort !== undefined) {
    if (!isRecord(patch.comfort)) problems.add("comfort", "オブジェクトにしてください");
    else {
      const c = patch.comfort as Json;
      const co2Max = optionalNumber(problems, "comfort.co2Max", c.co2Max, 400, 5000);
      if (co2Max !== null) next.comfort.co2Max = co2Max;
      const humidityMin = optionalNumber(problems, "comfort.humidityMin", c.humidityMin, 0, 100);
      if (humidityMin !== null) next.comfort.humidityMin = humidityMin;
      const humidityMax = optionalNumber(problems, "comfort.humidityMax", c.humidityMax, 0, 100);
      if (humidityMax !== null) next.comfort.humidityMax = humidityMax;
      for (const key of ["temperatureMin", "temperatureMax"] as const) {
        if (c[key] === undefined) continue;
        if (c[key] === null) next.comfort[key] = null;
        else {
          const value = optionalNumber(problems, `comfort.${key}`, c[key], 0, 40);
          if (value !== null) next.comfort[key] = value;
        }
      }
      if (next.comfort.humidityMin > next.comfort.humidityMax) {
        problems.add("comfort", "humidityMin は humidityMax 以下にしてください");
      }
      const { temperatureMin, temperatureMax } = next.comfort;
      if (temperatureMin !== null && temperatureMax !== null && temperatureMin > temperatureMax) {
        problems.add("comfort", "temperatureMin は temperatureMax 以下にしてください");
      }
    }
  }

  if (patch.deviceRoles !== undefined) {
    if (!isRecord(patch.deviceRoles)) problems.add("deviceRoles", "オブジェクトにしてください");
    else {
      for (const [deviceId, role] of Object.entries(patch.deviceRoles)) {
        if (role === null) {
          delete next.deviceRoles[deviceId];
        } else if (typeof role === "string" && (DEVICE_ROLES as readonly string[]).includes(role)) {
          next.deviceRoles[deviceId] = role as DeviceRole;
        } else {
          problems.add(`deviceRoles.${deviceId}`, `${DEVICE_ROLES.join(" / ")} のどれか（自動に戻すなら null）にしてください`);
        }
      }
    }
  }
  return next;
}

export async function saveSettings(db: D1Database, userId: string, settings: UserSettings, now: number): Promise<void> {
  await db
    .prepare(
      `INSERT INTO user_settings (user_id, settings_json, updated_at) VALUES (?1, ?2, ?3)
       ON CONFLICT (user_id) DO UPDATE SET settings_json = excluded.settings_json, updated_at = excluded.updated_at`,
    )
    .bind(userId, JSON.stringify(settings), now)
    .run();
}
