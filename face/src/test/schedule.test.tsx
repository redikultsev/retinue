// The owner's schedule (lib/schedule.ts): each verb goes the way it must — a task's fields and his own blocks a write of
// the editor's, his blocks always `locked`, at the version shown; taking off, laying and holding a press of the plan —
// and the grid's controls say verbs, read here with no FullCalendar drawn.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BlockMenu, dropped, PlanBar, stretched } from "@/components/plan/grid-tab";
import { ScheduleContext } from "@/components/plan/shared";
import type { Step } from "@/lib/edit";
import type { Block, Placed, Plan, Press, Task } from "@/lib/plan";
import { type Schedule, schedule } from "@/lib/schedule";

const NOW = Date.parse("2026-11-04T07:00:00Z") / 1000;
const TASK: Task = { id: "01KTASK0000000000000000001", fields: { title: "Write the report", state: "open", minutes: 45 }, version: 3, created: NOW, updated: NOW };
const BLOCK: Block = { id: "01KBLOCK000000000000000001", fields: { task: TASK.id, start: "2026-11-04T15:00:00Z", minutes: 45, state: "planned" }, version: 2 };
const PLACED: Placed = { block: BLOCK, task: TASK, start: Date.parse(BLOCK.fields.start) / 1000, end: Date.parse(BLOCK.fields.start) / 1000 + 45 * 60 };

function fake() {
  const writes: [string, Step[]][] = [];
  const presses: Press[] = [];
  const s = schedule(
    async (collection, steps) => {
      writes.push([collection, steps]);
      return true;
    },
    async (body) => {
      presses.push(body);
    },
  );
  return { s, writes, presses, ops: () => writes.map(([c, steps]) => [c, steps.map((x) => x.op)]) };
}

/** A schedule that only records its verbs. */
function verbs(): Schedule & Record<keyof Schedule, ReturnType<typeof vi.fn>> {
  const keys: (keyof Schedule)[] = ["add", "set", "put", "move", "resize", "remove", "plan", "place", "undo", "busy", "now", "free", "off", "hours", "activities"];
  return Object.fromEntries(keys.map((k) => [k, vi.fn().mockResolvedValue(true)])) as never;
}

afterEach(() => vi.restoreAllMocks());

describe("the schedule's verbs", () => {
  it("puts, moves and stretches his own blocks as writes of `blocks`, each locked, at the version shown", async () => {
    const { s, ops, presses } = fake();
    await s.put(TASK, Date.parse("2026-11-05T17:30:00Z") / 1000, 45, "Planned.");
    await s.move(BLOCK, Date.parse("2026-11-05T18:00:00Z") / 1000, "Moved.");
    await s.resize(BLOCK, 90, "Longer.");
    expect(ops()).toEqual([
      ["blocks", [{ op: "add", fields: { task: TASK.id, start: "2026-11-05T17:30:00Z", minutes: 45, state: "planned", locked: true } }]],
      ["blocks", [{ op: "set", id: BLOCK.id, version: 2, fields: { start: "2026-11-05T18:00:00Z", locked: true } }]],
      ["blocks", [{ op: "set", id: BLOCK.id, version: 2, fields: { minutes: 90, locked: true } }]],
    ]);
    expect(presses).toEqual([]);
  });

  it("takes a block off with a press, never a write: the Router keeps the plan's account", async () => {
    const { s, writes, presses } = fake();
    await s.remove(BLOCK, "Taken off.");
    expect(presses).toEqual([{ action: "remove", block: BLOCK.id }]);
    expect(writes).toEqual([]);
  });

  it("writes a task's fields and a new one, open, to `tasks`", async () => {
    const { s, ops } = fake();
    await s.set(TASK, { state: "done", due: null }, "Done.");
    await s.add({ title: "Call Acme" }, "Added.");
    expect(ops()).toEqual([
      ["tasks", [{ op: "set", id: TASK.id, version: 3, fields: { state: "done", due: null } }]],
      ["tasks", [{ op: "add", fields: { state: "open", title: "Call Acme" } }]],
    ]);
  });

  it("presses the plan for the rest, each body as the platform takes it", async () => {
    const { s, presses } = fake();
    await s.plan("2026-11-04", 1, "");
    await s.plan("2026-11-04", 7, "");
    await s.place("2026-11-05", TASK, "");
    await s.place("2026-11-05", { t: "a".repeat(32) }, "");
    await s.place("2026-11-05", { ref: "reminder:7" }, "");
    await s.undo("p0123456789", "");
    await s.busy(60, "");
    await s.now("Lunch", "");
    await s.free("");
    await s.off("");
    await s.hours({ zone: "Europe/Berlin", opens: "09:00", closes: "23:00" }, "");
    await s.activities([{ title: "Walk", minutes: 30 }], "");
    expect(presses).toEqual([
      { action: "plan", day: "2026-11-04" },
      { action: "plan", day: "2026-11-04", days: 7 },
      { action: "place", day: "2026-11-05", task: TASK.id },
      { action: "place", day: "2026-11-05", t: "a".repeat(32) },
      { action: "place", day: "2026-11-05", ref: "reminder:7" },
      { action: "undo", plan: "p0123456789" },
      { action: "busy", minutes: 60 },
      { action: "now", activity: "Lunch" },
      { action: "free" },
      { action: "off" },
      { action: "hours", zone: "Europe/Berlin", opens: "09:00", closes: "23:00" },
      { action: "activities", activities: [{ title: "Walk", minutes: 30 }] },
    ]);
  });

  it("says a press that did not go through, and writes nothing where the views are read only", async () => {
    const s = schedule(null, () => Promise.reject(new Error("No connection to the server.")));
    expect(await s.off("Not working today…")).toBe(false);
    expect(await s.move(BLOCK, NOW, "Moved.")).toBe(false);
  });
});

describe("the grid's controls", () => {
  const plan: Plan = {
    now: NOW,
    tz: "Europe/Berlin",
    tasks: { rows: [TASK], rev: null, space: "home", goals: [], missing: false, blocks: [BLOCK] },
    calendar: { events: [], at: NOW, failed: 0, stale: false },
    reminders: [],
    plan: { at: NOW, plans: {}, recent: [{ id: "p0123456789", by: "owner", at: NOW, undone: false, failed: false }], holds: [], said: {}, window: { zone: "Europe/Berlin", opens: "09:00", closes: "23:00" } },
  };

  it("a drag moves the block and a pull stretches it", async () => {
    const s = verbs();
    await dropped(s, PLACED, Date.parse("2026-11-05T17:00:00Z") / 1000, "Europe/Berlin", "2026-11-04");
    await stretched(s, PLACED, 90);
    expect(s.move).toHaveBeenCalledWith(BLOCK, Date.parse("2026-11-05T17:00:00Z") / 1000, "Moved to Tomorrow 18:00.");
    expect(s.resize).toHaveBeenCalledWith(BLOCK, 90, "Write the report: 1 h 30.");
  });

  it("a block's menu sets its length or takes it off", async () => {
    const s = verbs();
    render(
      <ScheduleContext value={s}>
        <BlockMenu menu={{ p: PLACED, x: 0, y: 0 }} close={() => {}} tz="Europe/Berlin" />
      </ScheduleContext>,
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "1h" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Take off the plan" }));
    await waitFor(() => expect(s.resize).toHaveBeenCalledWith(BLOCK, 60, "Write the report: 1 h."));
    expect(s.remove).toHaveBeenCalledWith(BLOCK, "Taken off the plan.");
  });

  it("the bar plans the day and the week, takes the plan back and stops the day", () => {
    const s = verbs();
    render(
      <ScheduleContext value={s}>
        <PlanBar plan={plan} first="2026-11-04" today="2026-11-04" unplanned={0} nav={{ title: "", go: () => {} }} />
      </ScheduleContext>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Plan my day" }));
    fireEvent.click(screen.getByRole("button", { name: "Plan my week" }));
    fireEvent.click(screen.getByRole("button", { name: "Undo plan" }));
    fireEvent.click(screen.getByRole("button", { name: "Now" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Not working today" }));
    expect(s.plan.mock.calls.map((c) => c.slice(0, 2))).toEqual([["2026-11-04", 1], ["2026-11-04", 7]]);
    expect(s.undo).toHaveBeenCalledWith("p0123456789", "Taking the plan back…");
    expect(s.off).toHaveBeenCalledTimes(1);
  });
});
