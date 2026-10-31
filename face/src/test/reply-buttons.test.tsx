import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReplyButtons } from "@/components/reply-buttons";
import { Transcript } from "@/components/transcript/transcript";
import { live } from "@/lib/live";
import { lastWords } from "@/lib/store";
import { hideReplyLines, openPing, withoutLine } from "@/lib/replies";
import type { Item, Summary } from "@/lib/types";

const ping = (over: Partial<Summary> = {}): Summary => ({
  session: "s1", seq: 7, head: "Result · notes · session s1", text: "Found three options.", at: 5, turn: 0,
  kind: "decision", took_s: 600, replies: ["Take the first", "Show more"], answered: null,
  line: "Replies: «Take the first» · «Show more»", ...over,
});

const LAST = "Found three options.\n\nReplies: «Take the first» · «Show more»";

afterEach(() => {
  vi.restoreAllMocks();
  live.stop();
});

describe("a ping's replies", () => {
  it("sends the pressed reply by the seq and its number, and shows the ping as the platform answers", async () => {
    const dispatch = vi.spyOn(live, "dispatch");
    const closed = ping({ replies: [], answered: "Show more" });
    const reply = vi.fn().mockResolvedValue({ say: "Accepted.", summary: closed });
    render(<ReplyButtons summary={ping()} live={live} reply={reply} />);
    const group = screen.getByRole("group", { name: "Replies" });
    expect(within(group).getAllByRole("button").map((b) => b.textContent)).toEqual(["Take the first", "Show more"]);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Show more" })));
    expect(reply).toHaveBeenCalledWith("s1", 7, 1);
    await waitFor(() =>
      expect(dispatch).toHaveBeenCalledWith({ type: "event", id: null, event: { kind: "summary", data: closed } }),
    );
  });

  it("shows the reply that went in instead of buttons, and nothing for a ping without replies", () => {
    const { rerender, container } = render(<ReplyButtons summary={ping({ replies: [], answered: "Take the first" })} />);
    expect(screen.getByTestId("answered").textContent).toBe("→ Take the first");
    rerender(<ReplyButtons summary={ping({ replies: [], kind: "done" })} />);
    expect(container.textContent).toBe("");
  });

  it("hides the replies line in the turn's last message, and the summary that repeats it shows only its header", () => {
    const items: Item[] = [{ session: "s1", turn: 0, n: 0, kind: "message", data: { role: "assistant", text: LAST } }];
    render(<Transcript items={items} approvals={[]} running={false} summaries={[ping()]} />);
    expect(screen.queryByText(/Replies:/)).toBeNull();
    expect(screen.getAllByText("Found three options.")).toHaveLength(1);
    expect(within(screen.getByTestId("summary")).getByRole("button", { name: "Take the first" })).toBeTruthy();
  });

  it("takes off only the exact line the platform parsed, at the end of the turn's last message", () => {
    const line = "Replies: «Take the first» · «Show more»";
    expect(withoutLine(`${LAST}\n  `, line)).toBe("Found three options.");
    expect(withoutLine(`${line}\nand more text after`, line)).toBe(`${line}\nand more text after`);
    expect(withoutLine(LAST, null)).toBe(LAST);
    const msg = (n: number, text: string, turn = 0): Item =>
      ({ session: "s1", turn, n, kind: "message", data: { role: "assistant", text } }) as Item;
    const items = [msg(0, LAST), msg(1, LAST), msg(0, LAST, 1)];
    const shown = hideReplyLines(items, [ping()]);
    expect(shown.map((it) => (it.kind === "message" ? it.data.text : ""))).toEqual([LAST, "Found three options.", LAST]);
    expect(hideReplyLines(items, [ping({ line: null })])).toBe(items);
    expect(lastWords([msg(0, LAST)], "", [ping()])).toBe("Found three options.");
    expect(lastWords([msg(0, LAST)], "", [])).toBe(LAST);
    expect(openPing([ping(), ping({ seq: 8, replies: [] })])).toBeNull();
    expect(openPing([ping({ seq: 6, replies: [] }), ping()])?.seq).toBe(7);
  });
});
