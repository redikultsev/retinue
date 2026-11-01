import { describe, expect, it } from "vitest";
import { addDays, at, dayOf, dayWords, duration, firstFree, hm, hours, iso, type Plan } from "@/lib/plan";
import { dayItems, load } from "@/components/plan/day-tab";

const TZ = "Europe/Berlin";
const NOW = Date.parse("2026-11-04T08:00:00Z") / 1000; // a Wednesday, 09:00 in Berlin

function plan(over: Partial<Plan> = {}): Plan {
  return {
    now: NOW,
    tz: TZ,
    tasks: { rows: [], rev: null, space: "home", goals: [{ value: "work", label: "Work" }], missing: false },
    calendar: { events: [], at: NOW, failed: 0, stale: false },
    asks: [],
    reminders: [],
    ...over,
  };
}

describe("the owner's day in his zone", () => {
  it("reads a moment's day and wall time where he is, across midnight and a change of offset", () => {
    expect(dayOf(Date.parse("2026-11-04T23:30:00Z") / 1000, TZ)).toBe("2026-11-05");
    expect(hm(Date.parse("2026-11-04T16:30:00Z") / 1000, TZ)).toBe("17:30");
    expect(iso(at("2026-11-04", 17 * 60 + 30, TZ))).toBe("2026-11-04T16:30:00Z");
    expect(iso(at("2027-03-29", 18 * 60, TZ))).toBe("2027-03-29T16:00:00Z"); // summer time: +2
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
  });

  it("says a day and a length as the tabs do", () => {
    expect(dayWords("2026-11-04", "2026-11-04")).toBe("Today");
    expect(dayWords("2026-11-05", "2026-11-04")).toBe("Tomorrow");
    expect(dayWords("2026-11-07", "2026-11-04")).toBe("Sat 7 Nov");
    expect([duration(45), duration(90), duration(120)]).toEqual(["45 min", "1 h 30", "2 h"]);
    expect(hours("2026-11-04")).toEqual([17 * 60, 23 * 60]);
    expect(hours("2026-11-07")).toEqual([10 * 60, 23 * 60]);
  });

  it("finds the first free quarter hour of an evening, clear of meetings and slots", () => {
    const meeting: [number, number] = [at("2026-11-04", 17 * 60, TZ), at("2026-11-04", 18 * 60, TZ)];
    expect(hm(firstFree("2026-11-04", 30, [meeting], NOW, TZ), TZ)).toBe("18:00");
    expect(hm(firstFree("2026-11-04", 30, [], at("2026-11-04", 19 * 60 + 5, TZ), TZ), TZ)).toBe("19:15");
  });
});

describe("a day's items", () => {
  it("puts meetings, slots and reminders in order with the free stretches of its hours between", () => {
    const p = plan({
      calendar: { events: [{ start: at("2026-11-04", 18 * 60, TZ), end: at("2026-11-04", 19 * 60, TZ), title: "Lesson", all_day: false, busy: true }], at: NOW, failed: 0, stale: false },
      tasks: {
        rows: [{ id: "T1", fields: { title: "Write", state: "open", minutes: 60, slot: iso(at("2026-11-04", 20 * 60, TZ)) }, version: 1, created: NOW, updated: NOW }],
        rev: null, space: "home", goals: [], missing: false,
      },
      reminders: [{ id: "reminder:1", title: "Call", at: at("2026-11-04", 12 * 60, TZ), when: "2026-11-04", repeat: null }],
    });
    const items = dayItems(p, "2026-11-04").map((i) => `${i.kind} ${hm(i.start, TZ)}`);
    expect(items).toEqual(["reminder 12:00", "free 17:00", "event 18:00", "free 19:00", "task 20:00", "free 21:00"]);
    expect(load(p, "2026-11-04")).toEqual({ planned: 60, free: 240 });
  });
});
