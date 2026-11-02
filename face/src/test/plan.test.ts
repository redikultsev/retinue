import { describe, expect, it } from "vitest";
import { addDays, at, dayOf, dayWords, duration, hm, iso, type Plan, planDay, windowDay, windowOf } from "@/lib/plan";
import { dayItems, load } from "@/components/plan/day-tab";

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

describe("a window's items", () => {
  it("puts meetings, blocks, holds and reminders in order with the free stretches between", () => {
    const p = plan({
      calendar: { events: [{ start: at("2026-11-04", 18 * 60, ZONE), end: at("2026-11-04", 19 * 60, ZONE), title: "Lesson", all_day: false, busy: true }], at: NOW, failed: 0, stale: false },
      tasks: {
        rows: [{ id: "T1", fields: { title: "Write", state: "open", minutes: 60 }, version: 1, created: NOW, updated: NOW }],
        rev: null,
        space: "home",
        goals: [],
        missing: false,
        blocks: [{ id: "B1", fields: { task: "T1", start: iso(at("2026-11-04", 20 * 60, ZONE)), minutes: 60, state: "planned" }, version: 1 }],
      },
      reminders: [{ id: "reminder:1", title: "Call", at: at("2026-11-04", 12 * 60, ZONE), when: "2026-11-04", repeat: null }],
      plan: { at: NOW, plans: {}, holds: [{ from: at("2026-11-04", 22 * 60, ZONE), to: at("2026-11-04", 23 * 60, ZONE), label: "Busy" }], said: {}, window: W },
    });
    const items = dayItems(p, "2026-11-04").map((i) => `${i.kind} ${hm(i.start, ZONE)}`);
    expect(items).toEqual(["free 11:00", "reminder 12:00", "event 18:00", "free 19:00", "block 20:00", "free 21:00", "hold 22:00", "free 23:00"]);
    expect(load(p, "2026-11-04")).toEqual({ planned: 60, free: 7 * 60 + 60 + 60 + 120 });
  });

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
    expect(dayItems(p, "2026-11-04").filter((i) => i.kind === "block").map((i) => hm(i.start, ZONE))).toEqual(["10:00"]);
  });
});
