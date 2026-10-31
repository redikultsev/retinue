import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Transcript } from "@/components/transcript/transcript";
import { hideSphereLines, withoutLine } from "@/lib/sphere-line";
import { lastWords } from "@/lib/store";
import type { Item, Summary } from "@/lib/types";

const LINE = "Sphere: travel";
const LAST = `Found three options.\n\n${LINE}`;
const ping = (over: Partial<Summary> = {}): Summary => ({
  session: "s1", seq: 7, head: "Result · notes · session s1", text: "Found three options.", at: 5, turn: 0,
  kind: "done", took_s: 600, line: LINE, ...over,
});
const msg = (n: number, text: string, turn = 0): Item =>
  ({ session: "s1", turn, n, kind: "message", data: { role: "assistant", text } }) as Item;

describe("a ping's sphere line", () => {
  it("is off the turn's last message, and a summary that repeats it shows only its header", () => {
    render(<Transcript items={[msg(0, LAST)]} approvals={[]} running={false} summaries={[ping()]} />);
    expect(screen.queryByText(/Sphere:/)).toBeNull();
    expect(screen.getAllByText("Found three options.")).toHaveLength(1);
  });

  it("is taken off exactly as the platform parsed it, at the end of the turn's last message only", () => {
    expect(withoutLine(`${LAST}\n  `, LINE)).toBe("Found three options.");
    expect(withoutLine(`${LINE}\nand more text after`, LINE)).toBe(`${LINE}\nand more text after`);
    expect(withoutLine(LAST, null)).toBe(LAST);
    const items = [msg(0, LAST), msg(1, LAST), msg(0, LAST, 1)];
    const shown = hideSphereLines(items, [ping()]);
    expect(shown.map((it) => (it.kind === "message" ? it.data.text : ""))).toEqual([LAST, "Found three options.", LAST]);
    expect(hideSphereLines(items, [ping({ line: null })])).toBe(items);
    expect(lastWords([msg(0, LAST)], "", [ping()])).toBe("Found three options.");
    expect(lastWords([msg(0, LAST)], "", [])).toBe(LAST);
  });
});
