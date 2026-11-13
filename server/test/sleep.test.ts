import { describe, expect, it } from "vitest";
import {
  buildSessions,
  chooseNight,
  napMs,
  pickWatchSamples,
  presenceInBedStart,
  type PresenceObservation,
  type SleepSample,
} from "../src/days/sleep";
import { MINUTE_MS, localTimeOn } from "../src/time";

const TZ = "Asia/Tokyo";
const at = (date: string, hhmm: string) => localTimeOn(date, hhmm, TZ);
const watch = (start: number, end: number, stage: string): SleepSample => ({ start, end, stage, sourceName: "Apple Watch", sourceProduct: "Watch7,1" });

describe("pickWatchSamples", () => {
  it("Apple Watch の記録だけを使う", () => {
    const iphone: SleepSample = { start: 0, end: 1, stage: "asleepUnspecified", sourceName: "iPhone", sourceProduct: "iPhone16,1" };
    const fromWatch = watch(0, 1, "asleepCore");
    expect(pickWatchSamples([iphone, fromWatch])).toEqual([fromWatch]);
    expect(pickWatchSamples([iphone])).toEqual([]);
  });

  it("どの記録にも機器の情報がなければ、すべてを使う", () => {
    const unknown: SleepSample = { start: 0, end: 1, stage: "asleepCore", sourceName: null, sourceProduct: null };
    expect(pickWatchSamples([unknown])).toEqual([unknown]);
  });
});

describe("buildSessions", () => {
  const d = "2026-11-21";
  const night = [
    watch(at("2026-11-20", "23:40"), at(d, "01:00"), "asleepCore"),
    watch(at(d, "01:00"), at(d, "02:00"), "asleepDeep"),
    watch(at(d, "02:00"), at(d, "02:05"), "awake"),
    watch(at(d, "02:05"), at(d, "03:00"), "asleepREM"),
    watch(at(d, "03:00"), at(d, "03:01"), "awake"),
    watch(at(d, "03:01"), at(d, "06:50"), "asleepCore"),
  ];

  it("切れ目が短ければ1つの睡眠にまとめ、時間を数える", () => {
    const [session] = buildSessions(night);
    expect(session.onset).toBe(at("2026-11-20", "23:40"));
    expect(session.wake).toBe(at(d, "06:50"));
    expect(session.asleepMs).toBe(424 * MINUTE_MS);
    expect(session.awakeMs).toBe(6 * MINUTE_MS);
    expect(session.stageMs.asleepDeep).toBe(60 * MINUTE_MS);
    // 2分以上の切れ目だけを中途覚醒として数える（1分の切れ目は数えない）
    expect(session.awakenings).toBe(1);
    expect(session.segments.filter((segment) => segment.stage === "awake")).toHaveLength(2);
  });

  it("60分より長く空いたら、別の睡眠にする", () => {
    const sessions = buildSessions([
      ...night,
      watch(at(d, "14:00"), at(d, "14:30"), "asleepCore"),
    ]);
    expect(sessions).toHaveLength(2);
  });
});

describe("chooseNight / napMs", () => {
  const sessions = buildSessions([
    // 前の日の昼寝
    watch(at("2026-11-20", "14:00"), at("2026-11-20", "14:30"), "asleepCore"),
    // 前の日の昼寝（短すぎるので数えない）
    watch(at("2026-11-20", "16:00"), at("2026-11-20", "16:10"), "asleepCore"),
    // その夜
    watch(at("2026-11-20", "23:40"), at("2026-11-21", "06:50"), "asleepCore"),
    // 朝の二度寝（別の睡眠だが、短いほうなので夜には選ばれない）
    watch(at("2026-11-21", "08:30"), at("2026-11-21", "09:00"), "asleepCore"),
  ]);

  it("前の日の18時〜その日の8時に始まった、いちばん長い睡眠を選ぶ", () => {
    expect(chooseNight(sessions, "2026-11-21", TZ)?.onset).toBe(at("2026-11-20", "23:40"));
    expect(chooseNight(sessions, "2026-11-22", TZ)).toBeNull();
  });

  it("前の日の8時〜18時の、15分以上の睡眠を昼寝として数える", () => {
    expect(napMs(sessions, "2026-11-21", TZ)).toBe(30 * MINUTE_MS);
  });
});

describe("presenceInBedStart", () => {
  const onset = at("2026-11-20", "23:40");
  /** from から to まで5分ごとに、present の値で観測を作る */
  const polls = (from: string, to: string, present: boolean): PresenceObservation[] => {
    const list: PresenceObservation[] = [];
    for (let t = at("2026-11-20", from); t <= at("2026-11-20", to); t += 5 * MINUTE_MS) list.push({ t, present });
    return list;
  };

  it("寝つく前からずっと「いる」の、最初の時刻を返す", () => {
    const observations = [...polls("21:00", "23:05", false), ...polls("23:10", "23:40", true)];
    expect(presenceInBedStart(observations, onset)).toBe(at("2026-11-20", "23:10"));
  });

  it("10分以内の「いない」は、寝返りなどとみなして続けて数える", () => {
    const observations = [
      ...polls("21:00", "22:40", false),
      ...polls("22:45", "23:00", true),
      { t: at("2026-11-20", "23:05"), present: false },
      ...polls("23:10", "23:40", true),
    ];
    expect(presenceInBedStart(observations, onset)).toBe(at("2026-11-20", "22:45"));
  });

  it("寝つく前の15分以内に「いる」がなければ、判定しない", () => {
    const observations = [...polls("22:00", "23:00", true), ...polls("23:05", "23:40", false)];
    expect(presenceInBedStart(observations, onset)).toBeNull();
  });

  it("3時間ずっと「いる」なら、3時間前からの最初の「いる」を使う", () => {
    const observations = polls("19:00", "23:40", true);
    expect(presenceInBedStart(observations, onset)).toBe(at("2026-11-20", "20:40"));
  });
});
