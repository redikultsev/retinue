// A Space's views in words and in place: the strip, the queues and the boards in their order; a board too short for
// columns; the column a phone opens on; days and thresholds as words; a ref by its record's words, never its id.
import { describe, expect, it } from "vitest";
import {
  type BoardView,
  boardTotal,
  cell,
  daysWords,
  emptyWords,
  layout,
  newer,
  openColumn,
  previousWords,
  progress,
  rowWords,
  type SpaceViews,
  ulidTime,
  type ViewRow,
  valueWords,
  windowWords,
} from "./collections";

const NOW = new Date("2026-09-16T12:00:00Z");
const ID = "01J8ZQ4Y7Z5G0000000000000A";
const row = (over: Partial<ViewRow> = {}): ViewRow => ({ id: ID, version: 1, fields: {}, refs: {}, src: null, days: null, rotten: false, ...over });

const board = (columns: Array<[string | null, number, boolean?, ViewRow[]?]>): BoardView => ({
  name: "pipeline", kind: "board", title: "Pipeline", group: "stage", rot: 7, fields: ["company"], dropped: 0,
  columns: columns.map(([value, count, folded = false, rows = []]) => ({ value, count, folded, rows })),
});

describe("layout", () => {
  it("puts every collection's numbers in one strip, then the queues, then the boards — a broken view in its kind's place", () => {
    const answer: SpaceViews = {
      space: "work", tz: "Europe/Berlin", today: "2026-09-16", at: 0,
      collections: [
        { name: "deals", rev: ID, views: [
          { name: "pipeline", kind: "board", title: "Pipeline", error: "field not found: 'stage'" },
          { name: "moved", kind: "stat", title: "Moved", value: 2, goal: null, previous: 1, window: "week", first: "2026-09-14", last: "2026-09-20" },
        ] },
        { name: "followups", rev: null, views: [
          { name: "made", kind: "stat", title: "Made", value: 0, goal: 7, previous: 0, window: "week", first: "2026-09-14", last: "2026-09-20" },
          { name: "my_move", kind: "queue", title: "My move", rot: 2, fields: [], count: 0, rows: [], dropped: 0 },
        ] },
      ],
    };
    const { stats, queues, boards } = layout(answer);
    expect(stats.map((p) => `${p.collection.name}/${p.view.name}`)).toEqual(["deals/moved", "followups/made"]);
    expect(queues.map((p) => p.view.name)).toEqual(["my_move"]);
    expect(boards.map((p) => p.view.error)).toEqual(["field not found: 'stage'"]);
    expect(layout(null)).toEqual({ stats: [], queues: [], boards: [] });
  });
});

describe("a board", () => {
  it("counts every record, the folded ones too: fewer than three make a list", () => {
    expect(boardTotal(board([["lead", 1], ["won", 0], ["lost", 1, true]]))).toBe(2);
    expect(boardTotal(board([["lead", 2], ["lost", 1, true]]))).toBe(3);
  });

  it("opens on a phone at the first open column with a stuck card, else the first with cards, never a folded one", () => {
    const stuck = row({ rotten: true });
    expect(openColumn(board([["lead", 0], ["qualified", 1, false, [row()]], ["proposal", 1, false, [stuck]], ["lost", 1, true, [stuck]]]))).toBe(2);
    expect(openColumn(board([["lead", 0], ["qualified", 2, false, [row(), row()]], ["lost", 3, true]]))).toBe(1);
    expect(openColumn(board([["lost", 3, true], ["lead", 0]]))).toBe(1);
  });

  it("says a value as the owner reads it, and the unknown one as «Unknown»", () => {
    expect(valueWords("pack_ready")).toBe("Pack ready");
    expect(valueWords(null)).toBe("Unknown");
  });
});

describe("days and thresholds", () => {
  it("says the days, and past the threshold the word as well as the colour", () => {
    expect(daysWords(row({ days: 9, rotten: true }), "board")).toEqual({ text: "9 days", late: "stuck" });
    expect(daysWords(row({ days: 3, rotten: true }), "queue")).toEqual({ text: "3 days", late: "overdue" });
    expect(daysWords(row({ days: 1 }), "queue")).toEqual({ text: "1 day", late: null });
    expect(daysWords(row({ days: 0 }), "queue").text).toBe("today");
    expect(daysWords(row({ days: -2 }), "queue").text).toBe("in 2 days");
    expect(daysWords(row({ days: null }), "board").text).toBe("");
  });
});

describe("a row in words", () => {
  it("says a ref by its record's words and never its id, a day short, a yes by the field's name", () => {
    const r = row({
      fields: { deal: ID, other: "01J8ZQ4Y7Z5G0000000000000B", date: "2026-09-14", amount: 54000, urgent: true, quiet: false, channel: "mail" },
      refs: { deal: "Acme" },
    });
    expect(cell(r, "deal", NOW)).toBe("Acme");
    expect(cell(r, "other", NOW)).toBeNull();
    expect(rowWords(r, ["deal", "other", "channel", "date", "amount", "urgent", "quiet", "missing"], NOW)).toEqual({
      title: "Acme", rest: ["mail", "14.09", "54,000", "Urgent"],
    });
    expect(rowWords(row(), ["company"], NOW)).toEqual({ title: "Untitled", rest: [] });
  });
});

describe("a number", () => {
  it("is measured against its goal, and met at the goal", () => {
    expect(progress({ value: 3, goal: 10 })).toEqual({ share: 0.3, met: false });
    expect(progress({ value: 12, goal: 10 })).toEqual({ share: 1, met: true });
    expect(progress({ value: 2, goal: null })).toBeNull();
  });

  it("says its window and the period before", () => {
    const stat = { window: "week" as const, first: "2026-09-14", last: "2026-09-20", previous: 2 };
    expect(windowWords(stat, NOW)).toBe("this week, 14.09–20.09");
    expect(previousWords(stat)).toBe("last week: 2");
    expect(windowWords({ window: null, first: null, last: null })).toBe("all time");
    expect(previousWords({ window: null, previous: null })).toBeNull();
  });
});

describe("rev", () => {
  it("is a ULID: its time is in its first ten letters, and a newer one is greater as a string", () => {
    expect(ulidTime("01KDVDNA00TSV4RRFFQ69G5FAV")).toBe(Date.UTC(2026, 0, 1));
    expect(ulidTime("not a ulid")).toBeNull();
    expect(newer("01ARZ3NDEKTSV4RRFFQ69G5FAW", "01ARZ3NDEKTSV4RRFFQ69G5FAV")).toBe(true);
    expect(newer("01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FAV")).toBe(false);
    expect(newer("01ARZ3NDEKTSV4RRFFQ69G5FAV", null)).toBe(true);
  });

  it("dates an empty view's quiet, or says nothing was ever written", () => {
    expect(emptyWords({ name: "deals", rev: "01KDVDNA00TSV4RRFFQ69G5FAV", views: [] }, new Date("2026-09-01"))).toBe(
      "Nothing here — no change since 01.01.",
    );
    expect(emptyWords({ name: "deals", rev: null, views: [] })).toBe("No data yet: nothing was written here.");
  });
});
