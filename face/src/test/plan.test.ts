import { describe, expect, it } from "vitest";
import { addDays, at, dayOf, dayWords, duration, hm, iso, type Plan, placed, planDay, windowDay, windowOf } from "@/lib/plan";

const TZ = "Europe/Berlin";
const ZONE = "Asia/Dubai"; // the plan's own zone, three hours ahead of the owner's
const W = { zone: ZONE, opens: "10:00", closes: "01:00" };
const NOW = Date.parse("2026-11-04T07:00:00Z") / 1000; // a Wednesday: 11:00 in the plan's zone, 08:00 in the owner's

function plan(over: Partial<Plan> = {}): Plan {
  return {
    now: NOW,
    tz: TZ,
    tasks: { rows: [], rev: null, space: "home", goals: [{ value: "work", label: "Work" }], missing: false, blocks: [] },
    calendar: { events: [], at: NOW, failed: 0, stale: false },
    asks: [],
    reminders: [],
    plan: { at: null, plans: {}, holds: [], said: {}, window: W },
    ...over,
  };
}

describe("the plan's window, in its own zone, shown in the owner's", () => {
  it("runs from its opening to past midnight in its zone, and a moment after midnight is the evening before", () => {
    const [open, close] = windowOf("2026-11-04", W);
    expect([hm(open, ZONE), hm(close, ZONE), hm(open, TZ), hm(close, TZ)]).toEqual(["10:00", "01:00", "07:00", "22:00"]);
    expect(windowDay(at("2026-11-05", 30, ZONE), W)).toBe("2026-11-04");
    expect(windowDay(at("2026-11-05", 90, ZONE), W)).toBe("2026-11-05");
    expect(planDay(at("2026-11-05", 20, ZONE), W)).toBe("2026-11-05"); // 00:20: forty minutes left — the next
    expect(planDay(at("2026-11-04", 23 * 60, ZONE), W)).toBe("2026-11-04");
  });

  it("says a day and a length as the tabs do, and keeps moments in UTC", () => {
    expect(dayWords("2026-11-05", "2026-11-04")).toBe("Tomorrow");
    expect(dayWords("2026-11-07", "2026-11-04")).toBe("Sat 7 Nov");
    expect([duration(45), duration(90), duration(120)]).toEqual(["45 min", "1 h 30", "2 h"]);
    expect(iso(at("2026-11-04", 17 * 60 + 30, ZONE))).toBe("2026-11-04T13:30:00Z");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(dayOf(Date.parse("2026-11-04T23:30:00Z") / 1000, TZ)).toBe("2026-11-05");
  });
});

describe("the blocks on the grid", () => {
  it("leaves out the blocks of a task closed since, but keeps its passed ones", () => {
    const rows = [{ id: "T1", fields: { title: "Done", state: "done" as const }, version: 2, created: NOW, updated: NOW }];
    const start = iso(at("2026-11-04", 20 * 60, ZONE));
    const p = plan({
      tasks: {
        rows,
        rev: null,
        space: "home",
        goals: [],
        missing: false,
        blocks: [
          { id: "B1", fields: { task: "T1", start, minutes: 30, state: "planned" }, version: 1 },
          { id: "B2", fields: { task: "T1", start: iso(at("2026-11-04", 10 * 60, ZONE)), minutes: 30, state: "passed" }, version: 2 },
        ],
      },
    });
    expect(placed(p).map((x) => hm(x.start, ZONE))).toEqual(["10:00"]);
  });
});
