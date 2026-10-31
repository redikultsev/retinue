import { describe, expect, it } from "vitest";
import { diffCounts, lineDiff } from "./linediff";

describe("lineDiff", () => {
  it("marks removed and added lines and keeps the common ones", () => {
    const rows = lineDiff("a\nb\nc", "a\nB\nc");
    expect(rows).toEqual([
      { type: " ", text: "a" },
      { type: "-", text: "b" },
      { type: "+", text: "B" },
      { type: " ", text: "c" },
    ]);
    expect(diffCounts(rows)).toEqual({ added: 1, removed: 1 });
  });

  it("folds long unchanged runs", () => {
    const same = Array.from({ length: 20 }, (_, i) => `line ${i}`);
    const rows = lineDiff([...same, "old"].join("\n"), [...same, "new"].join("\n"), 2);
    expect(rows[0]).toEqual({ type: "…", text: "18 unchanged" });
    expect(rows.slice(1).map((r) => r.type)).toEqual([" ", " ", "-", "+"]);
  });

  it("a new file is all additions", () => {
    expect(lineDiff("", "x\ny").map((r) => r.type)).toEqual(["+", "+"]);
  });
});
